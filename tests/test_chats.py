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


def test_chat_tables_live_in_the_ticket_database(tmp_path, sa):
    store = sa.sa_store.Store(tmp_path / "superagent.db")
    store.create_ticket(request="a ticket", status="queued")
    chat = sa_chats.ChatStore(tmp_path / "superagent.db")
    chat.create_session("a chat")
    assert len(store.list()) == 1  # ticket tables untouched
    assert len(chat.list_sessions()["sessions"]) == 1
