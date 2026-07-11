"""LangGraph / LangChain integration — the LangChain-side equivalent of
witwicky.integrations.strands.decision_ledger_hook. Same collapse of
callback registration + check-then-record orchestration into one factory
call; same one thing the caller still owns: build_transaction.

Unlike Strands (whose MCP tool results arrive wrapped in
structuredContent/content blocks, requiring extract_result_dict()), a plain
`@tool`-decorated LangChain tool's return value arrives at on_tool_end
unwrapped — a dict-returning tool yields a dict directly, no unwrapping
helper needed for that common case. MCP-backed LangChain tools (via
langchain-mcp-adapters or similar) may still need unwrapping — not handled
here.

Lazy dependency: langchain-core is NOT a dependency of the core package
(pyproject.toml's `dependencies = []` stays empty) — importing this module
is what pulls it in.

Usage::

    from witwicky import DecisionLedgerClient
    from witwicky.integrations.langgraph import decision_ledger_callback

    client = DecisionLedgerClient(url, deployment_id="...", agent_id="...")

    def build_transaction(result, tool_input, state):
        return {"counterparty": ..., "instrument": ..., "quantity": 1,
                "price": ..., "currency": ...}

    DecisionLedgerCallback = decision_ledger_callback(
        client, tool_name="book_carrier", mandate_ref="...", mandate_version="1",
        build_transaction=build_transaction,
    )
    # Attach via RunnableConfig so it covers every tool call in the graph:
    #   graph.invoke(input, config={"callbacks": [DecisionLedgerCallback()],
    #                                "metadata": {"actor_id": "...", "session_id": "..."}})
"""
import logging
from typing import Any, Callable, Optional
from uuid import UUID

from ..client import DecisionLedgerClient

logger = logging.getLogger("witwicky.integrations.langgraph")

TransactionBuilder = Callable[[dict, dict, dict], Optional[dict]]
ExternalAnchorsBuilder = Callable[[dict], Optional[dict]]


def decision_ledger_callback(
    client: Optional[DecisionLedgerClient],
    *,
    tool_name: str,
    mandate_ref: str,
    mandate_version: str,
    build_transaction: TransactionBuilder,
    build_external_anchors: Optional[ExternalAnchorsBuilder] = None,
    reversibility: str = "reversible_at_cost",
):
    """Returns a LangChain `BaseCallbackHandler` *class* (not an instance —
    matches the `callbacks=[SomeCallback(), ...]` convention) that emits a
    decision event on every completed `tool_name` call.

    Same parameter shape as `strands.decision_ledger_hook` — an existing
    build_transaction written against that integration works here unchanged.

    `client=None` disables the integration entirely (valid no-op; callers
    don't need an `if` around their `callbacks=[...]` list).

    build_transaction(result, tool_input, state) -> transaction dict, OR
    None to skip this call entirely (the tool returned an error payload
    rather than a real commitment).
      - result: the tool's return value. A dict-returning @tool's output
        arrives as-is (see module docstring).
      - tool_input: the structured input dict the tool was invoked with
        (LangChain's `inputs` kwarg, not the stringified `input_str`).
      - state: the `metadata` dict passed via config={"metadata": {...}}
        at invoke time — the direct equivalent of Strands' invocation_state.
        By convention this SDK reads `actor_id`/`session_id` from it.
    build_external_anchors(result) -> optional external_anchors dict
    """
    from langchain_core.callbacks.base import BaseCallbackHandler

    class DecisionLedgerCallback(BaseCallbackHandler):
        def __init__(self) -> None:
            super().__init__()
            # run_id -> (tool_input, state), stashed by on_tool_start for
            # on_tool_end/on_tool_error to pick up — those only get run_id.
            self._pending: dict[UUID, tuple] = {}

        def on_tool_start(
            self,
            serialized: dict,
            input_str: str,
            *,
            run_id: UUID,
            parent_run_id: Optional[UUID] = None,
            tags: Optional[list] = None,
            metadata: Optional[dict] = None,
            inputs: Optional[dict] = None,
            **kwargs: Any,
        ) -> None:
            if client is None or not serialized or serialized.get("name") != tool_name:
                return
            self._pending[run_id] = (inputs or {}, metadata or {})

        def on_tool_end(self, output: Any, *, run_id: UUID, parent_run_id: Optional[UUID] = None, **kwargs: Any) -> None:
            self._handle(run_id, output, exception=None)

        def on_tool_error(self, error: BaseException, *, run_id: UUID, parent_run_id: Optional[UUID] = None, **kwargs: Any) -> None:
            self._handle(run_id, {}, exception=error)

        def _handle(self, run_id: UUID, output: Any, *, exception: Optional[BaseException]) -> None:
            pending = self._pending.pop(run_id, None)
            if client is None or pending is None:
                return  # not our tool (on_tool_start never stashed it), or no-op mode
            tool_input, state = pending
            actor_id = state.get("actor_id", "unknown")
            episode_id = state.get("session_id", "unknown")

            result_dict = output if isinstance(output, dict) and exception is None else {}
            transaction = build_transaction(result_dict, tool_input, state)
            if transaction is None:
                # The tool call didn't represent a real decision (e.g. it
                # returned an error). Skip both check and record — don't
                # write a junk decision event to the ledger.
                return

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
                    "status": "failed" if exception else "executed",
                    "reversibility": reversibility,
                },
                external_anchors=external_anchors,
                unchecked=check_result.unchecked,
            )

    return DecisionLedgerCallback
