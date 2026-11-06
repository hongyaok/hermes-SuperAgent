import time

import pytest


def make_settings(sa, **overrides):
    base = dict(mode="observe", max_workers=2, max_runtime_seconds=60)
    base.update(overrides)
    return sa.sa_engine.Settings(**base)


def test_sessions_empty_db(sa, store, fake_executor):
    engine = sa.sa_engine.Engine(store, make_settings(sa), executor=fake_executor)
    out = engine.sessions()
    assert out["sessions"] == []
    assert out["counts"] == {}
    assert out["running"] == 0
    assert out["queued"] == 0


def test_sessions_after_ask(sa, store, fake_executor):
    engine = sa.sa_engine.Engine(store, make_settings(sa), executor=fake_executor)
    ticket = engine.ask("Fix the crash in parser.py", requester="alice")
    out = engine.sessions()
    assert len(out["sessions"]) == 1
    row = out["sessions"][0]
    assert row["id"] == ticket["id"]
    assert row["status"] == sa.sa_engine.OBSERVED
    assert row["source"] == "local"
    assert row["requester"] == "alice"
    assert "crash" in row["request"]
    assert row["reason"] is not None
    assert "created_at" in row and "updated_at" in row


def test_sessions_preview_truncation(sa, store, fake_executor):
    engine = sa.sa_engine.Engine(store, make_settings(sa), executor=fake_executor)
    long_request = "Fix this bug in the parser module " * 20
    ticket = engine.ask(long_request)
    out = engine.sessions()
    row = next(s for s in out["sessions"] if s["id"] == ticket["id"])
    assert len(row["request"]) <= 161
    assert row["request"].endswith("…")


def test_sessions_result_preview_truncation(sa, store, fake_executor):
    engine = sa.sa_engine.Engine(store, make_settings(sa), executor=fake_executor)
    ticket = engine.ask("Fix the parser crash")
    store.update(ticket["id"], result="R" * 500)
    out = engine.sessions()
    row = next(s for s in out["sessions"] if s["id"] == ticket["id"])
    assert len(row["result_preview"]) <= 201
    assert row["result_preview"].endswith("…")
    assert row["handle"] is None or isinstance(row["handle"], str)


def test_sessions_event_count(sa, store, fake_executor):
    engine = sa.sa_engine.Engine(store, make_settings(sa), executor=fake_executor)
    ticket = engine.ask("Fix the parser crash")
    store.add_event(ticket["id"], "note", "extra detail")
    out = engine.sessions()
    row = next(s for s in out["sessions"] if s["id"] == ticket["id"])
    # created + assessed + note = at least 3 events
    assert row["event_count"] >= 3


def test_sessions_elapsed_secs(sa, store, fake_executor):
    engine = sa.sa_engine.Engine(store, make_settings(sa), executor=fake_executor)
    ticket = engine.ask("Fix the parser crash")
    store.update(ticket["id"], started_at=time.time() - 42)
    out = engine.sessions()
    row = next(s for s in out["sessions"] if s["id"] == ticket["id"])
    assert row["started_at"] is not None
    assert row["elapsed_secs"] >= 40
    assert row["elapsed_secs"] < 3600


def test_sessions_counts_running_queued(sa, store, fake_executor):
    engine = sa.sa_engine.Engine(store, make_settings(sa, mode="auto"), executor=fake_executor)
    first = engine.ask("Fix the parser crash")
    second = engine.ask("Add a regression test for the parser")
    engine.tick()
    out = engine.sessions()
    assert out["counts"].get("running", 0) >= 1
    assert out["running"] == out["counts"].get("running", 0)
    assert out["queued"] == out["counts"].get("queued", 0)
    ids = {s["id"] for s in out["sessions"]}
    assert first["id"] in ids and second["id"] in ids


def test_sessions_status_filter(sa, store, fake_executor):
    engine = sa.sa_engine.Engine(store, make_settings(sa), executor=fake_executor)
    engine.ask("Fix the parser crash")
    out = engine.sessions(status="observed")
    assert all(s["status"] == "observed" for s in out["sessions"])
    empty = engine.sessions(status="running")
    assert empty["sessions"] == []


def test_get_ticket_detail_ok(sa, store, fake_executor):
    engine = sa.sa_engine.Engine(store, make_settings(sa), executor=fake_executor)
    ticket = engine.ask("Fix the parser crash")
    detail = engine.get_ticket_detail(ticket["id"])
    assert detail["id"] == ticket["id"]
    assert detail["request"] == "Fix the parser crash"
    assert isinstance(detail["events"], list)
    assert len(detail["events"]) >= 2


def test_get_ticket_detail_missing_raises(sa, store, fake_executor):
    engine = sa.sa_engine.Engine(store, make_settings(sa), executor=fake_executor)
    with pytest.raises(KeyError):
        engine.get_ticket_detail("T-9999")


def test_store_list_tickets_filter(sa, store):
    store.create_ticket(request="a", status="queued")
    store.create_ticket(request="b", status="observed")
    assert [t["request"] for t in store.list_tickets(status="observed")] == ["b"]
    both = store.list_tickets(status="queued,observed")
    assert len(both) == 2
    # legacy alias still works
    assert len(store.list(status="queued")) == 1


def test_store_get_events_and_count_by_status(sa, store):
    ticket = store.create_ticket(request="a", status="queued")
    store.add_event(ticket["id"], "launched", "worker sa-1")
    kinds = [e["kind"] for e in store.get_events(ticket["id"])]
    assert "created" in kinds and "launched" in kinds
    # legacy alias still works
    assert kinds == [e["kind"] for e in store.events(ticket["id"])]
    assert store.count_by_status().get("queued") == 1
