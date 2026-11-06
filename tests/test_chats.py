"""Tests for the chat-session store (sa_chats.py)."""

from __future__ import annotations

from sa_plugin import sa_chats


def test_session_ids_are_sequential(chat):
    first = chat.create_session("fix the parser")
    second = chat.create_session("add a test")
    assert first["id"] == "S-0001"
    assert second["id"] == "S-0002"


def test_title_comes_from_the_first_prompt_line(chat):
    session = chat.create_session("Fix the parser crash\n\nMore detail here")
    assert session["title"] == "Fix the parser crash"


def test_title_is_clipped_to_80_chars(chat):
    session = chat.create_session("x" * 200)
    assert len(session["title"]) == 80
    assert session["title"].endswith("…")


def test_explicit_title_wins(chat):
    session = chat.create_session("Fix the parser crash", title="  Parser  ")
    assert session["title"] == "Parser"


def test_create_records_fields_and_a_created_event(chat):
    session = chat.create_session(
        "ship it",
        model="deepseek-v4.1-flash",
        provider="opencode-go",
        repo="C:/repo",
        source="desktop",
    )
    assert session["status"] == "queued"
    assert session["model"] == "deepseek-v4.1-flash"
    assert session["provider"] == "opencode-go"
    assert session["repo"] == "C:/repo"
    assert session["prompt"] == "ship it"
    assert session["pid"] is None
    assert session["created_at"] <= session["updated_at"]
    kinds = [event["kind"] for event in session["events"]]
    assert "created" in kinds


def test_update_session_changes_whitelisted_fields(chat):
    session = chat.create_session("work")
    updated = chat.update_session(
        session["id"], status="running", pid=1234, tokens=42, hermes_session_id="local-abc"
    )
    assert updated["status"] == "running"
    assert updated["pid"] == 1234
    assert updated["tokens"] == 42
    assert updated["hermes_session_id"] == "local-abc"


def test_update_session_rejects_unknown_fields(chat):
    session = chat.create_session("work")
    try:
        chat.update_session(session["id"], nope="x")
    except ValueError as exc:
        assert "nope" in str(exc)
    else:  # pragma: no cover - the call above must raise
        raise AssertionError("unknown column should raise")


def test_update_unknown_session_returns_none(chat):
    assert chat.update_session("S-9999", status="review") is None


def test_get_unknown_session_returns_none(chat):
    assert chat.get_session("S-9999") is None


def test_messages_are_truncated_and_ordered(chat):
    session = chat.create_session("work")
    chat.add_message(session["id"], "user", "hello")
    chat.add_message(session["id"], "assistant", "y" * 25000)
    messages = chat.get_messages(session["id"])
    assert [m["role"] for m in messages] == ["user", "assistant"]
    assert len(messages[1]["content"]) == sa_chats.MAX_MESSAGE_CHARS


def test_message_limit_keeps_the_newest_in_order(chat):
    session = chat.create_session("work")
    for index in range(5):
        chat.add_message(session["id"], "user", f"m{index}")
    messages = chat.get_messages(session["id"], limit=2)
    assert [m["content"] for m in messages] == ["m3", "m4"]


def test_event_detail_is_truncated_and_sets_last_event(chat):
    session = chat.create_session("work")
    chat.add_event(session["id"], "tool", "z" * 5000)
    events = chat.get_events(session["id"])
    tool = next(event for event in events if event["kind"] == "tool")
    assert len(tool["detail"]) == sa_chats.MAX_EVENT_CHARS
    refreshed = chat.get_session(session["id"], message_limit=0)
    assert refreshed["last_event"].startswith("tool: zzz")


def test_get_session_returns_messages_and_events_chronologically(chat):
    session = chat.create_session("work")
    chat.add_message(session["id"], "user", "one")
    chat.add_event(session["id"], "queued", "waiting")
    chat.add_message(session["id"], "assistant", "two")
    detail = chat.get_session(session["id"])
    assert [m["content"] for m in detail["messages"]] == ["one", "two"]
    kinds = [event["kind"] for event in detail["events"]]
    assert kinds == ["created", "queued"]


def test_list_sessions_shape_and_order(chat):
    first = chat.create_session("first")
    second = chat.create_session("second")
    chat.add_message(second["id"], "user", "hi")
    chat.update_session(second["id"], status="running")

    data = chat.list_sessions()
    assert data["running"] == 1
    assert data["counts"]["running"] == 1
    assert data["counts"]["queued"] == 1
    assert [row["id"] for row in data["sessions"]] == [second["id"], first["id"]]
    assert data["sessions"][0]["message_count"] == 1


def test_live_rows_carry_the_last_three_events_newest_first(chat):
    first = chat.create_session("live work")
    second = chat.create_session("more live work")
    for index in range(4):
        chat.add_event(first["id"], "tool", f"a{index}")
        chat.add_event(second["id"], "tool", f"b{index}")

    rows = {row["id"]: row for row in chat.list_sessions()["sessions"]}
    a = rows[first["id"]]["recent_events"]
    b = rows[second["id"]]["recent_events"]
    # Three each, from its own session, newest first — one query, two buckets.
    assert [event["detail"] for event in a] == ["a3", "a2", "a1"]
    assert [event["detail"] for event in b] == ["b3", "b2", "b1"]
    assert all(event["kind"] == "tool" for event in a)
    assert all("created_at" in event for event in a)
    assert a[0]["created_at"] >= a[-1]["created_at"]


def test_recent_event_detail_is_clipped_to_160_chars(chat):
    session = chat.create_session("long detail")
    chat.add_event(session["id"], "tool", "d" * 900)

    event = chat.list_sessions()["sessions"][0]["recent_events"][0]
    assert len(event["detail"]) == sa_chats.RECENT_EVENT_CHARS


def test_only_live_sessions_get_recent_events(chat):
    chat.create_session("still queued")
    done = chat.create_session("finished")
    chat.update_session(done["id"], status="idle")

    rows = {row["id"]: row for row in chat.list_sessions()["sessions"]}
    assert "recent_events" in rows["S-0001"]
    assert "recent_events" not in rows["S-0002"]
    assert all("created_at" in event for event in rows["S-0001"]["recent_events"])


def test_elapsed_seconds_is_only_reported_for_live_turns(chat):
    running = chat.create_session("running")
    now = sa_chats.time.time()
    chat.update_session(running["id"], status="running", started_at=now - 30)
    queued = chat.create_session("queued")
    idle = chat.create_session("idle")
    chat.update_session(idle["id"], status="idle", started_at=now - 90, finished_at=now)

    rows = {row["id"]: row for row in chat.list_sessions()["sessions"]}
    assert 25 <= rows[running["id"]]["elapsed_seconds"] <= 40
    assert rows[queued["id"]]["elapsed_seconds"] == 0
    assert rows[idle["id"]]["elapsed_seconds"] is None


def test_queued_rows_ignore_a_previous_turns_start(chat):
    """A resumed turn is queued while `started_at` still holds the old turn."""
    session = chat.create_session("resume me")
    chat.update_session(session["id"], status="running", started_at=sa_chats.time.time() - 600)
    chat.update_session(session["id"], status="queued")

    row = chat.list_sessions()["sessions"][0]
    assert row["elapsed_seconds"] == 0


def test_list_sessions_status_filter_keeps_board_counts(chat):
    running = chat.create_session("running")
    chat.update_session(running["id"], status="running")
    queued = chat.create_session("queued")
    done = chat.create_session("done")
    chat.update_session(done["id"], status="review")

    data = chat.list_sessions(statuses=["running", "queued"])
    assert [row["id"] for row in data["sessions"]] == [queued["id"], running["id"]]
    assert data["counts"]["review"] == 1  # counts describe the whole board
    assert data["running"] == 1


def test_list_sessions_ignores_an_empty_status_filter(chat):
    chat.create_session("one")
    chat.create_session("two")
    assert len(chat.list_sessions(statuses=[])["sessions"]) == 2
    assert len(chat.list_sessions(statuses=["  "])["sessions"]) == 2


def test_chat_tables_live_in_the_ticket_database(tmp_path, sa):
    store = sa.sa_store.Store(tmp_path / "superagent.db")
    store.create_ticket(request="a ticket", status="queued")
    chat = sa_chats.ChatStore(tmp_path / "superagent.db")
    chat.create_session("a chat")
    assert len(store.list()) == 1  # ticket tables untouched
    assert len(chat.list_sessions()["sessions"]) == 1
