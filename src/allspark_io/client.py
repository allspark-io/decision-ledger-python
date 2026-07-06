"""DecisionLedgerClient — the FR-1.3(a) native Python SDK.

Design principles (AGENTS.md rule 7 applies to this SDK too, not just the
collector): this library must never own the availability of the customer's
revenue path. Every call that could block on the network either has a short
timeout and fails open (check()), or returns instantly and durably buffers
the real work for a background thread (record_decision(), record_outcome()).
"""
from __future__ import annotations

import atexit
import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from . import outbox as _outbox
from ._http import HttpError, post_json

logger = logging.getLogger("decision_ledger")

SCHEMA_VERSION = "1-0-0"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class CheckResult:
    """Result of a pre-execution check (FR-4.2). `unchecked=True` means the
    collector was unreachable and this is a synthetic fail-open result
    (FR-4.3) — pass it straight through to record_decision(unchecked=...)."""
    decision: str  # "allow" | "warn" | "block-advice"
    matched_rules: list = field(default_factory=list)
    unchecked: bool = False


class DecisionLedgerClient:
    def __init__(
        self,
        base_url: str,
        *,
        deployment_id: str,
        agent_id: str,
        check_timeout: float = 0.15,  # FR-4.4: p99 <= 150ms in-region
        send_timeout: float = 5.0,
        outbox_dir: str | None = None,
        poll_interval: float = 0.5,
        max_retry_interval: float = 300.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.deployment_id = deployment_id
        self.agent_id = agent_id
        self.check_timeout = check_timeout
        self.send_timeout = send_timeout
        self.max_retry_interval = max_retry_interval

        self._outbox = _outbox.Outbox(outbox_dir or f".decision_ledger_outbox/{deployment_id}")
        self._poll_interval = poll_interval
        self._stop = threading.Event()
        self._worker = threading.Thread(target=self._run, name="decision-ledger-outbox", daemon=True)
        self._worker.start()
        self._atexit_registered = False
        self._register_atexit()

    # ------------------------------------------------------------------
    # FR-4.2 pre-execution check — synchronous, short-timeout, fail-open.
    # ------------------------------------------------------------------
    def check(
        self,
        *,
        mandate_ref: str,
        mandate_version: str,
        transaction: dict,
        reversibility: str = "reversible_at_cost",
    ) -> CheckResult:
        payload = {
            "deployment_id": self.deployment_id,
            "agent_id": self.agent_id,
            "mandate_ref": mandate_ref,
            "mandate_version": mandate_version,
            "transaction": transaction,
            "reversibility": reversibility,
        }
        try:
            result = post_json(f"{self.base_url}/check", payload, timeout=self.check_timeout)
            return CheckResult(decision=result.get("decision", "allow"), matched_rules=result.get("matched_rules", []))
        except HttpError:
            # FR-4.3: advisory only. The collector being unreachable must
            # never stop the customer's agent from proceeding.
            logger.warning("decision-ledger check unreachable, proceeding unchecked (fail-open)", exc_info=True)
            return CheckResult(decision="allow", matched_rules=[], unchecked=True)

    # ------------------------------------------------------------------
    # Durable, buffered event submission.
    # ------------------------------------------------------------------
    def record_decision(
        self,
        *,
        principal: str,
        mandate_ref: str,
        mandate_version: str,
        episode_id: str,
        transaction: dict,
        execution: dict,
        reasoning: dict | None = None,
        parent_decision_id: str | None = None,
        external_anchors: dict | None = None,
        attestation: dict | None = None,
        unchecked: bool = False,
        event_id: str | None = None,
        occurred_at: str | None = None,
    ) -> str:
        """Enqueues a decision event and returns immediately — never blocks
        on the network. Returns the event_id (generated if not supplied) so
        the caller can reference it later, e.g. from record_outcome()."""
        event_id = event_id or str(uuid.uuid4())
        execution = dict(execution)
        execution.setdefault("unchecked", unchecked)
        event = {
            "event_id": event_id,
            "event_type": "decision",
            "schema_version": SCHEMA_VERSION,
            "occurred_at": occurred_at or _now_iso(),
            "deployment_id": self.deployment_id,
            "agent_id": self.agent_id,
            "principal": principal,
            "mandate_ref": mandate_ref,
            "mandate_version": mandate_version,
            "episode_id": episode_id,
            "transaction": transaction,
            "reasoning": reasoning or {},
            "execution": execution,
        }
        if parent_decision_id is not None:
            event["parent_decision_id"] = parent_decision_id
        if external_anchors is not None:
            event["external_anchors"] = external_anchors
        if attestation is not None:
            event["attestation"] = attestation
        self._enqueue(event_id, event)
        return event_id

    def record_outcome(
        self,
        *,
        decision_event_id: str,
        outcome_type: str,
        amount: float | None = None,
        currency: str | None = None,
        details: str | None = None,
        event_id: str | None = None,
        occurred_at: str | None = None,
    ) -> str:
        event_id = event_id or str(uuid.uuid4())
        event = {
            "event_id": event_id,
            "event_type": "outcome",
            "schema_version": SCHEMA_VERSION,
            "occurred_at": occurred_at or _now_iso(),
            "deployment_id": self.deployment_id,
            "decision_event_id": decision_event_id,
            "outcome_type": outcome_type,
        }
        if amount is not None:
            event["amount"] = amount
        if currency is not None:
            event["currency"] = currency
        if details is not None:
            event["details"] = details
        self._enqueue(event_id, event)
        return event_id

    def pending_count(self) -> int:
        """Number of events not yet acknowledged by the collector — for
        health checks/metrics, e.g. alert if this grows unbounded."""
        return len(self._outbox)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def close(self, timeout: float = 5.0) -> None:
        """Stops the background worker. Does NOT discard unsent events —
        they stay in the outbox on disk and will be picked up by the next
        DecisionLedgerClient constructed against the same outbox_dir (e.g.
        after a process restart)."""
        self._stop.set()
        self._worker.join(timeout=timeout)

    def __enter__(self) -> "DecisionLedgerClient":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def _register_atexit(self) -> None:
        if not self._atexit_registered:
            atexit.register(self.close, timeout=2.0)
            self._atexit_registered = True

    # ------------------------------------------------------------------
    # Internal: durable buffer + background delivery.
    # ------------------------------------------------------------------
    def _enqueue(self, event_id: str, event: dict) -> None:
        # Write-ahead to local disk BEFORE any network attempt (mirrors the
        # collector's own NFR-2 write-ahead-to-S3-before-ack discipline) —
        # this call returning is the durability guarantee, not a successful
        # send.
        self._outbox.put(event_id, event)

    def _run(self) -> None:
        # "Durable system, high accuracy in tracking" means this thread must
        # never die — a crashed worker silently stops all future delivery
        # until the process restarts, which is worse than any single
        # delivery failure. _flush_once() already catches HttpError per
        # event; this outer catch is defense-in-depth against anything
        # else (a corrupt outbox entry, an unexpected library exception)
        # taking down the whole loop.
        while not self._stop.is_set():
            try:
                self._flush_once()
            except Exception:  # noqa: BLE001
                logger.exception("decision-ledger outbox worker hit an unexpected error, continuing")
            self._stop.wait(self._poll_interval)
        # Best-effort final pass on shutdown — not required for correctness
        # (the outbox survives on disk regardless) but shortens the time
        # until data lands if the process is exiting cleanly.
        try:
            self._flush_once()
        except Exception:  # noqa: BLE001
            logger.exception("decision-ledger outbox worker hit an unexpected error during final flush")

    def _flush_once(self) -> None:
        now = _outbox.now()
        for event_id in self._outbox.pending():
            record = self._outbox.get(event_id)
            if record is None:
                continue  # removed concurrently, or a corrupt/unreadable entry
            if record["next_attempt_at"] > now:
                continue  # still in backoff — don't head-of-line block the rest
            try:
                post_json(f"{self.base_url}/events", record["event"], timeout=self.send_timeout)
                self._outbox.remove(event_id)
            except HttpError:
                attempts = record["attempts"] + 1
                delay = _outbox.backoff_seconds(attempts, cap=self.max_retry_interval)
                logger.warning(
                    "failed to deliver decision-ledger event %s (attempt %d, retrying in %.0fs)",
                    event_id, attempts, delay, exc_info=True,
                )
                self._outbox.mark_attempt(event_id, next_attempt_at=now + delay)
