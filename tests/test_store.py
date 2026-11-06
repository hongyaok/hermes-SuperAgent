def test_ticket_ids_are_sequential(store):
    first = store.create_ticket(request="fix a", status="queued")
    second = store.create_ticket(request="fix b", status="queued")
    assert first["id"] == "T-0001"
    assert second["id"] == "T-0002"


def test_update_and_fetch(store):
    ticket = store.create_ticket(request="fix a", status="queued")
    updated = store.update(ticket["id"], status="running", result="done")
    assert updated["status"] == "running"
    assert updated["result"] == "done"
    assert store.get(ticket["id"])["status"] == "running"


def test_list_filters_by_status(store):
    store.create_ticket(request="a", status="queued")
    store.create_ticket(request="b", status="observed")
    observed = store.list(status="observed")
    assert [ticket["request"] for ticket in observed] == ["b"]


def test_state_round_trips_json(store):
    store.set_state("github_since", "2026-11-06T00:00:00Z")
    assert store.get_state("github_since") == "2026-11-06T00:00:00Z"


def test_seen_dedupes(store):
    assert not store.is_seen("github:1")
    store.mark_seen("github:1")
    assert store.is_seen("github:1")


def test_events_are_recorded(store):
    ticket = store.create_ticket(request="a", status="queued")
    store.add_event(ticket["id"], "launched", "worker sa-1")
    kinds = [event["kind"] for event in store.events(ticket["id"])]
    assert "created" in kinds
    assert "launched" in kinds
