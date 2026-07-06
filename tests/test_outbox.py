from decision_ledger.outbox import Outbox, backoff_seconds


def test_put_then_get_round_trips(tmp_path):
    ob = Outbox(tmp_path)
    ob.put("e1", {"hello": "world"})
    record = ob.get("e1")
    assert record["event"] == {"hello": "world"}
    assert record["attempts"] == 0
    assert record["next_attempt_at"] == 0.0


def test_remove_deletes_the_file(tmp_path):
    ob = Outbox(tmp_path)
    ob.put("e1", {"x": 1})
    ob.remove("e1")
    assert ob.get("e1") is None
    assert len(ob) == 0


def test_pending_lists_every_unremoved_event(tmp_path):
    ob = Outbox(tmp_path)
    ob.put("e1", {})
    ob.put("e2", {})
    assert set(ob.pending()) == {"e1", "e2"}
    ob.remove("e1")
    assert ob.pending() == ["e2"]


def test_mark_attempt_increments_and_persists(tmp_path):
    ob = Outbox(tmp_path)
    ob.put("e1", {})
    ob.mark_attempt("e1", next_attempt_at=123.0)
    record = ob.get("e1")
    assert record["attempts"] == 1
    assert record["next_attempt_at"] == 123.0


def test_outbox_survives_across_instances_same_directory(tmp_path):
    # Simulates a process restart: a new Outbox pointed at the same
    # directory must see everything the previous instance left behind.
    Outbox(tmp_path).put("e1", {"data": 42})
    reopened = Outbox(tmp_path)
    assert reopened.get("e1")["event"] == {"data": 42}


def test_backoff_seconds_grows_exponentially_then_caps():
    assert backoff_seconds(0, base=1.0, cap=300.0) == 1.0
    assert backoff_seconds(1, base=1.0, cap=300.0) == 2.0
    assert backoff_seconds(2, base=1.0, cap=300.0) == 4.0
    assert backoff_seconds(20, base=1.0, cap=300.0) == 300.0  # capped, never abandoned
