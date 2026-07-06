"""Strands Agents integration — collapses the boilerplate every Strands-based
decision-ledger integration was rewriting by hand (hook registration, MCP
result unwrapping, check-then-record orchestration) into one factory call.
The only thing a caller still owns is mapping a specific tool's result into
a `transaction` dict — that mapping is inherently business-specific and
can't be abstracted away.

Lazy dependency: strands-agents is NOT a dependency of the core package
(pyproject.toml's `dependencies = []` stays empty) — importing this module
is what pulls it in, so installing allspark-decision-ledger alone never
forces a Strands install.

Usage::

    from allspark_io import DecisionLedgerClient
    from allspark_io.integrations.strands import decision_ledger_hook

    client = DecisionLedgerClient(url, deployment_id="...", agent_id="...")

    def build_transaction(result, tool_input, state):
        return {"counterparty": ..., "instrument": ..., "quantity": 1,
                "price": ..., "currency": ...}

    DecisionLedgerHooks = decision_ledger_hook(
        client, tool_name="checkout", mandate_ref="...", mandate_version="1",
        build_transaction=build_transaction,
    )
    # then: Agent(hooks=[DecisionLedgerHooks(), ...])
"""
import json
import logging
from typing import Any, Callable, Optional

from ..client import DecisionLedgerClient

logger = logging.getLogger("allspark_io.integrations.strands")

TransactionBuilder = Callable[[dict, dict, dict], dict]
ExternalAnchorsBuilder = Callable[[dict], Optional[dict]]


def extract_result_dict(result: Any) -> dict:
    """An MCP tool's return value doesn't arrive as a plain dict: Strands'
    MCPToolResult carries it either as `structuredContent` (a dict, only
    present if the server-side tool has a return type annotation FastMCP
    can build a schema from) or as JSON text inside a `content` block list
    (the always-present "unstructured" form) — prefer the former, fall
    back to parsing the latter."""
    if not isinstance(result, dict):
        return {}
    structured = result.get("structuredContent")
    if isinstance(structured, dict):
        return structured
    for block in result.get("content") or []:
        text = block.get("text") if isinstance(block, dict) else None
        if not text:
            continue
        try:
            parsed = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(parsed, dict):
            return parsed
    return {}


def decision_ledger_hook(
    client: Optional[DecisionLedgerClient],
    *,
    tool_name: str,
    mandate_ref: str,
    mandate_version: str,
    build_transaction: TransactionBuilder,
    build_external_anchors: Optional[ExternalAnchorsBuilder] = None,
    reversibility: str = "reversible_at_cost",
):
    """Returns a Strands `HookProvider` *class* (not an instance — matches
    the `hooks=[SomeHook(), ...]` convention callers already use) that emits
    a decision event on every completed `tool_name` call.

    `client=None` disables the integration entirely (e.g. decision-ledger
    URL unset) while still returning a valid, harmless no-op hook — callers
    don't need an `if` around their `hooks=[...]` list.

    build_transaction(result, tool_input, state) -> transaction dict
    build_external_anchors(result) -> optional external_anchors dict
    """
    from strands.hooks import AfterToolCallEvent, HookProvider, HookRegistry

    class DecisionLedgerHook(HookProvider):
        def register_hooks(self, registry: HookRegistry, **kwargs) -> None:
            registry.add_callback(AfterToolCallEvent, self._on_after_tool_call)

        def _on_after_tool_call(self, event: AfterToolCallEvent) -> None:
            if client is None or event.tool_use.get("name") != tool_name:
                return

            state = event.invocation_state or {}
            actor_id = state.get("actor_id", "unknown")
            episode_id = state.get("session_id", "unknown")
            tool_input = event.tool_use.get("input", {}) or {}

            result_dict = extract_result_dict(event.result) if not event.exception else {}
            transaction = build_transaction(result_dict, tool_input, state)

            check_result = client.check(mandate_ref=mandate_ref, mandate_version=mandate_version, transaction=transaction)
            if check_result.decision != "allow" and not check_result.unchecked:
                logger.info("decision-ledger check for %s returned %r on %r", actor_id, check_result.decision, transaction)

            external_anchors = build_external_anchors(result_dict) if build_external_anchors else None

            client.record_decision(
                principal=actor_id,
                mandate_ref=mandate_ref,
                mandate_version=mandate_version,
                episode_id=episode_id,
                transaction=transaction,
                execution={
                    "status": "failed" if event.exception else "executed",
                    "reversibility": reversibility,
                },
                external_anchors=external_anchors,
                unchecked=check_result.unchecked,
            )

    return DecisionLedgerHook
