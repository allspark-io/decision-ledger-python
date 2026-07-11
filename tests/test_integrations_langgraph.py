import time
import uuid

import pytest
from fake_collector import FakeCollector

from witwicky import DecisionLedgerClient
from witwicky.integrations.langgraph import decision_ledger_callback


def _wait_until_delivered(client, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if client.pending_count() == 0:
            return
        time.sleep(0.05)
    raise AssertionError("event was not delivered in time")


def test_callback_is_noop_when_client_is_none():
    Callback = decision_ledger_callback(
        None, tool_name="checkout", mandate_ref="m", mandate_version="1",
        build_transaction=lambda result, tool_input, state: {},
    )
    cb = Callback()
    run_id = uuid.uuid4()
    cb.on_tool_start({"name": "checkout"}, "{}", run_id=run_id, metadata={}, inputs={})
    cb.on_tool_end({}, run_id=run_id)  # must not raise


def test_callback_ignores_non_matching_tool_calls(tmp_path):
    fc = FakeCollector()
    try:
        client = DecisionLedgerClient(fc.base_url, deployment_id="dp", agent_id="a", outbox_dir=str(tmp_path))
        called = []
        Callback = decision_ledger_callback(
            client, tool_name="checkout", mandate_ref="m", mandate_version="1",
            build_transaction=lambda result, tool_input, state: called.append(1) or {},
        )
        cb = Callback()
        run_id = uuid.uuid4()
        cb.on_tool_start({"name": "search_products"}, "{}", run_id=run_id, metadata={}, inputs={})
        cb.on_tool_end({}, run_id=run_id)
        assert called == []
        assert client.pending_count() == 0
    finally:
        fc.shutdown()


def test_callback_skips_when_build_transaction_returns_none(tmp_path):
    fc = FakeCollector()
    try:
        client = DecisionLedgerClient(fc.base_url, deployment_id="dp", agent_id="a", outbox_dir=str(tmp_path))
        Callback = decision_ledger_callback(
            client, tool_name="book_carrier", mandate_ref="m", mandate_version="1",
            build_transaction=lambda result, tool_input, state: None if result.get("error") else {
                "counterparty": "c", "instrument": "i", "quantity": 1, "price": 1, "currency": "USD",
            },
        )
        cb = Callback()
        run_id = uuid.uuid4()
        cb.on_tool_start({"name": "book_carrier"}, "{}", run_id=run_id, metadata={}, inputs={})
        cb.on_tool_end({"error": "Quote not found"}, run_id=run_id)
        # Nothing checked, nothing recorded — not even a /check call.
        assert client.pending_count() == 0
        assert fc.received == []
    finally:
        fc.shutdown()


def test_callback_emits_decision_with_transaction_from_callback(tmp_path):
    fc = FakeCollector()
    try:
        client = DecisionLedgerClient(fc.base_url, deployment_id="dp", agent_id="a", outbox_dir=str(tmp_path))

        def build_transaction(result, tool_input, state):
            return {
                "counterparty": result.get("carrier"),
                "instrument": f"cart:{tool_input.get('cart_id')}",
                "quantity": 1,
                "price": result.get("price"),
                "currency": "EUR",
            }

        Callback = decision_ledger_callback(
            client, tool_name="checkout", mandate_ref="m", mandate_version="1",
            build_transaction=build_transaction,
        )
        cb = Callback()
        run_id = uuid.uuid4()
        cb.on_tool_start(
            {"name": "checkout"}, "{}", run_id=run_id,
            metadata={"actor_id": "user-1", "session_id": "sess-1"},
            inputs={"cart_id": "c1"},
        )
        cb.on_tool_end({"carrier": "Acme", "price": 42.0}, run_id=run_id)

        _wait_until_delivered(client)
        client.close()

        paths = [p for p, _ in fc.received]
        assert "/check" in paths
        assert "/events" in paths
        decision_body = next(b for p, b in fc.received if p == "/events")
        assert decision_body["transaction"] == {
            "counterparty": "Acme", "instrument": "cart:c1", "quantity": 1, "price": 42.0, "currency": "EUR",
        }
        assert decision_body["principal"] == "user-1"
        assert decision_body["episode_id"] == "sess-1"
        assert decision_body["execution"]["unchecked"] is False
        assert decision_body["execution"]["status"] == "executed"
    finally:
        fc.shutdown()


def test_callback_includes_external_anchors_when_provided(tmp_path):
    fc = FakeCollector()
    try:
        client = DecisionLedgerClient(fc.base_url, deployment_id="dp", agent_id="a", outbox_dir=str(tmp_path))
        Callback = decision_ledger_callback(
            client, tool_name="book_carrier", mandate_ref="m", mandate_version="1",
            build_transaction=lambda result, tool_input, state: {
                "counterparty": "c", "instrument": "i", "quantity": 1, "price": 1, "currency": "USD",
            },
            build_external_anchors=lambda result: {"booking_ref": result.get("booking_ref")},
        )
        cb = Callback()
        run_id = uuid.uuid4()
        cb.on_tool_start({"name": "book_carrier"}, "{}", run_id=run_id, metadata={}, inputs={})
        cb.on_tool_end({"booking_ref": "BK-1"}, run_id=run_id)

        _wait_until_delivered(client)
        client.close()

        decision_body = next(b for p, b in fc.received if p == "/events")
        assert decision_body["external_anchors"] == {"booking_ref": "BK-1"}
    finally:
        fc.shutdown()


def test_callback_marks_unchecked_when_collector_unreachable(tmp_path):
    fc = FakeCollector()
    fc.up = False
    try:
        client = DecisionLedgerClient(fc.base_url, deployment_id="dp", agent_id="a", outbox_dir=str(tmp_path), check_timeout=0.2)
        Callback = decision_ledger_callback(
            client, tool_name="checkout", mandate_ref="m", mandate_version="1",
            build_transaction=lambda result, tool_input, state: {
                "counterparty": "c", "instrument": "i", "quantity": 1, "price": 1, "currency": "USD",
            },
        )
        cb = Callback()
        run_id = uuid.uuid4()
        cb.on_tool_start({"name": "checkout"}, "{}", run_id=run_id, metadata={}, inputs={})
        cb.on_tool_end({}, run_id=run_id)

        fc.up = True
        _wait_until_delivered(client)
        client.close()

        decision_body = next(b for p, b in fc.received if p == "/events")
        assert decision_body["execution"]["unchecked"] is True
    finally:
        fc.shutdown()


def test_callback_records_failed_status_on_tool_error(tmp_path):
    # The on_tool_error path — LangChain fires this INSTEAD OF on_tool_end
    # on failure (unlike Strands' single callback that inspects
    # event.exception internally); the adapter must still record a
    # decision event with execution.status == "failed".
    fc = FakeCollector()
    try:
        client = DecisionLedgerClient(fc.base_url, deployment_id="dp", agent_id="a", outbox_dir=str(tmp_path))
        Callback = decision_ledger_callback(
            client, tool_name="book_carrier", mandate_ref="m", mandate_version="1",
            build_transaction=lambda result, tool_input, state: {
                "counterparty": "c", "instrument": "i", "quantity": 1, "price": 1, "currency": "USD",
            },
        )
        cb = Callback()
        run_id = uuid.uuid4()
        cb.on_tool_start({"name": "book_carrier"}, "{}", run_id=run_id, metadata={}, inputs={})
        cb.on_tool_error(RuntimeError("carrier API down"), run_id=run_id)

        _wait_until_delivered(client)
        client.close()

        decision_body = next(b for p, b in fc.received if p == "/events")
        assert decision_body["execution"]["status"] == "failed"
    finally:
        fc.shutdown()


def _build_booking_transaction(result: dict, tool_input: dict, state: dict):
    # Verbatim copy of agent-spark-2/main.py's _build_booking_transaction —
    # the actual "same build_transaction works across both frameworks"
    # proof, not a LangGraph-specific rewrite.
    if result.get("status") != "booked":
        return None
    origin = result.get("origin") or "unknown"
    destination = result.get("destination") or "unknown"
    return {
        "counterparty": result.get("carrier"),
        "instrument": f"lane:{origin}-{destination}",
        "quantity": 1,
        "price": float(result.get("price") or 0),
        "currency": result.get("currency") or "USD",
    }


def test_build_transaction_reused_across_frameworks_is_schema_shaped(tmp_path):
    """Proves the reused build_transaction produces a transaction dict
    satisfying decision-event's `transaction` field constraints — same
    function, same shape, no schema changes required for LangGraph."""
    fc = FakeCollector()
    try:
        client = DecisionLedgerClient(fc.base_url, deployment_id="dp", agent_id="a", outbox_dir=str(tmp_path))
        Callback = decision_ledger_callback(
            client, tool_name="book_carrier", mandate_ref="freight-mandate", mandate_version="1",
            build_transaction=_build_booking_transaction,
        )
        cb = Callback()
        run_id = uuid.uuid4()
        cb.on_tool_start(
            {"name": "book_carrier"}, "{}", run_id=run_id,
            metadata={"actor_id": "agent-spark-2", "session_id": "ep-1"},
            inputs={"origin": "Rotterdam", "destination": "Marrakech"},
        )
        cb.on_tool_end(
            {"status": "booked", "carrier": "Maersk", "origin": "Rotterdam",
             "destination": "Marrakech", "price": 1200.5, "currency": "EUR"},
            run_id=run_id,
        )

        _wait_until_delivered(client)
        client.close()

        event = next(b for p, b in fc.received if p == "/events")

        # Hand-asserted against decision-event 1-0-0's actual required
        # fields/constraints (always runs, no extra dependency) — see
        # decision-ledger/core/src/witwickyio_ledger_core/schemas/decision-event/1-0-0.schema.json
        for field in ("event_id", "event_type", "schema_version", "occurred_at",
                      "deployment_id", "agent_id", "principal", "mandate_ref",
                      "mandate_version", "episode_id", "transaction", "reasoning", "execution"):
            assert field in event
        txn = event["transaction"]
        for field in ("counterparty", "instrument", "quantity", "price", "currency"):
            assert field in txn
        assert isinstance(txn["quantity"], (int, float))
        assert isinstance(txn["price"], (int, float))
        assert len(txn["currency"]) == 3 and txn["currency"].isupper()
        assert event["execution"]["status"] in ("executed", "blocked", "failed", "reversed")
        assert event["execution"]["reversibility"] in ("freely_reversible", "reversible_at_cost", "irreversible")

        # Stronger, opportunistic proof: real jsonschema validation against
        # decision-ledger's actual schema, only if witwickyio_ledger_core
        # happens to be installed (private CodeArtifact index — not a hard
        # dependency of this repo, see plan notes on why).
        witwickyio_ledger_core = pytest.importorskip("witwickyio_ledger_core")
        validator = witwickyio_ledger_core.load_validator("decision")
        validator.validate(event)
    finally:
        fc.shutdown()
