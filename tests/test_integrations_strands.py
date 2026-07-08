import time

import pytest
from fake_collector import FakeCollector

from allspark_io import DecisionLedgerClient
from allspark_io.integrations.strands import decision_ledger_hook, extract_result_dict


class _FakeEvent:
    """Duck-typed stand-in for Strands' AfterToolCallEvent — the hook only
    ever reads tool_use/result/invocation_state/exception, so a real Agent/
    AgentTool isn't needed to exercise it."""

    def __init__(self, tool_use, result, invocation_state=None, exception=None):
        self.tool_use = tool_use
        self.result = result
        self.invocation_state = invocation_state
        self.exception = exception


def _wait_until_delivered(client, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if client.pending_count() == 0:
            return
        time.sleep(0.05)
    raise AssertionError("event was not delivered in time")


def test_extract_result_dict_prefers_structured_content():
    result = {"structuredContent": {"status": "booked", "price": 10}}
    assert extract_result_dict(result) == result["structuredContent"]


def test_extract_result_dict_falls_back_to_json_in_text_content_block():
    import json
    payload = {"status": "booked", "price": 5}
    result = {"content": [{"text": json.dumps(payload)}]}
    assert extract_result_dict(result) == payload


def test_extract_result_dict_returns_empty_dict_when_neither_is_present():
    assert extract_result_dict({}) == {}
    assert extract_result_dict(None) == {}


def test_hook_is_noop_when_client_is_none():
    Hook = decision_ledger_hook(
        None, tool_name="checkout", mandate_ref="m", mandate_version="1",
        build_transaction=lambda result, tool_input, state: {},
    )
    hook = Hook()
    event = _FakeEvent(tool_use={"name": "checkout", "input": {}}, result={}, invocation_state={})
    hook._on_after_tool_call(event)  # must not raise


def test_hook_skips_when_build_transaction_returns_none(tmp_path):
    # A failed tool call (error payload) must not write a decision event:
    # build_transaction returns None, and the hook records nothing.
    fc = FakeCollector()
    try:
        client = DecisionLedgerClient(fc.base_url, deployment_id="dp", agent_id="a", outbox_dir=str(tmp_path))
        Hook = decision_ledger_hook(
            client, tool_name="book_carrier", mandate_ref="m", mandate_version="1",
            build_transaction=lambda result, tool_input, state: None if result.get("error") else {
                "counterparty": "c", "instrument": "i", "quantity": 1, "price": 1, "currency": "USD",
            },
        )
        Hook()._on_after_tool_call(_FakeEvent(
            tool_use={"name": "book_carrier", "input": {}},
            result={"structuredContent": {"error": "Quote not found"}},
            invocation_state={},
        ))
        # Nothing checked, nothing recorded — not even a /check call.
        assert client.pending_count() == 0
        assert fc.received == []
    finally:
        fc.shutdown()


def test_hook_ignores_non_matching_tool_calls(tmp_path):
    fc = FakeCollector()
    try:
        client = DecisionLedgerClient(fc.base_url, deployment_id="dp", agent_id="a", outbox_dir=str(tmp_path))
        called = []
        Hook = decision_ledger_hook(
            client, tool_name="checkout", mandate_ref="m", mandate_version="1",
            build_transaction=lambda result, tool_input, state: called.append(1) or {},
        )
        Hook()._on_after_tool_call(_FakeEvent(tool_use={"name": "search_products", "input": {}}, result={}, invocation_state={}))
        assert called == []
        assert client.pending_count() == 0
    finally:
        fc.shutdown()


def test_hook_emits_decision_with_transaction_from_callback(tmp_path):
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

        Hook = decision_ledger_hook(
            client, tool_name="checkout", mandate_ref="m", mandate_version="1",
            build_transaction=build_transaction,
        )
        event = _FakeEvent(
            tool_use={"name": "checkout", "input": {"cart_id": "c1"}},
            result={"structuredContent": {"carrier": "Acme", "price": 42.0}},
            invocation_state={"actor_id": "user-1", "session_id": "sess-1"},
        )
        Hook()._on_after_tool_call(event)

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
    finally:
        fc.shutdown()


def test_hook_includes_external_anchors_when_provided(tmp_path):
    fc = FakeCollector()
    try:
        client = DecisionLedgerClient(fc.base_url, deployment_id="dp", agent_id="a", outbox_dir=str(tmp_path))
        Hook = decision_ledger_hook(
            client, tool_name="book_carrier", mandate_ref="m", mandate_version="1",
            build_transaction=lambda result, tool_input, state: {
                "counterparty": "c", "instrument": "i", "quantity": 1, "price": 1, "currency": "USD",
            },
            build_external_anchors=lambda result: {"booking_ref": result.get("booking_ref")},
        )
        event = _FakeEvent(
            tool_use={"name": "book_carrier", "input": {}},
            result={"structuredContent": {"booking_ref": "BK-1"}},
            invocation_state={},
        )
        Hook()._on_after_tool_call(event)

        _wait_until_delivered(client)
        client.close()

        decision_body = next(b for p, b in fc.received if p == "/events")
        assert decision_body["external_anchors"] == {"booking_ref": "BK-1"}
    finally:
        fc.shutdown()


def test_hook_marks_unchecked_when_collector_unreachable(tmp_path):
    fc = FakeCollector()
    fc.up = False
    try:
        client = DecisionLedgerClient(fc.base_url, deployment_id="dp", agent_id="a", outbox_dir=str(tmp_path), check_timeout=0.2)
        Hook = decision_ledger_hook(
            client, tool_name="checkout", mandate_ref="m", mandate_version="1",
            build_transaction=lambda result, tool_input, state: {
                "counterparty": "c", "instrument": "i", "quantity": 1, "price": 1, "currency": "USD",
            },
        )
        Hook()._on_after_tool_call(_FakeEvent(tool_use={"name": "checkout", "input": {}}, result={}, invocation_state={}))

        fc.up = True
        _wait_until_delivered(client)
        client.close()

        decision_body = next(b for p, b in fc.received if p == "/events")
        assert decision_body["execution"]["unchecked"] is True
    finally:
        fc.shutdown()
