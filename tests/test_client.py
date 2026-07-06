import socket
import time

import pytest
from fake_collector import FakeCollector

from decision_ledger import DecisionLedgerClient

TXN = {"counterparty": "carrier-1", "instrument": "lane-1", "quantity": 1, "price": 100, "currency": "EUR"}


@pytest.fixture
def collector():
    fc = FakeCollector()
    yield fc
    fc.shutdown()


def _unused_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_check_fails_open_when_collector_is_unreachable(tmp_path):
    # FR-4.3: nothing is listening on this port at all.
    client = DecisionLedgerClient(
        f"http://127.0.0.1:{_unused_port()}", deployment_id="dp", agent_id="a",
        check_timeout=0.2, outbox_dir=str(tmp_path),
    )
    try:
        result = client.check(mandate_ref="m", mandate_version="1", transaction=TXN)
        assert result.decision == "allow"
        assert result.unchecked is True
    finally:
        client.close()


def test_check_returns_the_real_verdict_when_reachable(tmp_path, collector):
    client = DecisionLedgerClient(collector.base_url, deployment_id="dp", agent_id="a", outbox_dir=str(tmp_path))
    try:
        result = client.check(mandate_ref="m", mandate_version="1", transaction=TXN)
        assert result.decision == "allow"
        assert result.unchecked is False
        assert collector.received[0][0] == "/check"
    finally:
        client.close()


def test_record_decision_returns_immediately_and_delivers_in_background(tmp_path, collector):
    client = DecisionLedgerClient(
        collector.base_url, deployment_id="dp", agent_id="a",
        outbox_dir=str(tmp_path), poll_interval=0.1,
    )
    try:
        event_id = client.record_decision(
            principal="Acme BV", mandate_ref="m", mandate_version="1", episode_id="ep-1",
            transaction=TXN, execution={"status": "executed", "reversibility": "reversible_at_cost"},
        )
        # Delivered asynchronously -- wait briefly rather than assuming it's instant.
        for _ in range(50):
            if client.pending_count() == 0:
                break
            time.sleep(0.05)
        assert client.pending_count() == 0
        assert any(path == "/events" and body["event_id"] == event_id for path, body in collector.received)
    finally:
        client.close()


def test_record_decision_survives_collector_downtime_then_delivers(tmp_path, collector):
    collector.up = False
    client = DecisionLedgerClient(
        collector.base_url, deployment_id="dp", agent_id="a",
        outbox_dir=str(tmp_path), poll_interval=0.1,
    )
    try:
        event_id = client.record_decision(
            principal="Acme BV", mandate_ref="m", mandate_version="1", episode_id="ep-1",
            transaction=TXN, execution={"status": "executed", "reversibility": "reversible_at_cost"},
        )
        time.sleep(0.3)
        # Still down: must NOT be lost -- still sitting durably in the outbox.
        assert client.pending_count() == 1

        collector.up = True
        for _ in range(50):
            if client.pending_count() == 0:
                break
            time.sleep(0.1)
        assert client.pending_count() == 0
        assert any(path == "/events" and body["event_id"] == event_id for path, body in collector.received)
    finally:
        client.close()


def test_outbox_durability_across_a_process_restart(tmp_path, collector):
    collector.up = False
    outbox_dir = str(tmp_path)
    client1 = DecisionLedgerClient(collector.base_url, deployment_id="dp", agent_id="a", outbox_dir=outbox_dir)
    event_id = client1.record_decision(
        principal="Acme BV", mandate_ref="m", mandate_version="1", episode_id="ep-1",
        transaction=TXN, execution={"status": "executed", "reversibility": "reversible_at_cost"},
    )
    client1.close()  # simulates the process exiting -- worker thread stops, event stays on disk

    # A brand-new client instance (new "process"), same outbox_dir, must pick up the leftover event.
    collector.up = True
    client2 = DecisionLedgerClient(
        collector.base_url, deployment_id="dp", agent_id="a", outbox_dir=outbox_dir, poll_interval=0.1,
    )
    try:
        for _ in range(50):
            if client2.pending_count() == 0:
                break
            time.sleep(0.05)
        assert client2.pending_count() == 0
        assert any(path == "/events" and body["event_id"] == event_id for path, body in collector.received)
    finally:
        client2.close()
