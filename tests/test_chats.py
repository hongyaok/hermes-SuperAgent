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


# -- session management (iteration 14) -------------------------------------
#
# Rename, archive and delete are housekeeping on the same rows: none of them
# may disturb the ticket schema, the transcript, or a repository on disk.


def _old_schema_without_archived(sa) -> str:
    """The pre-iteration-14 chat_sessions shape (no archived_at, no index)."""
    lines = [line for line in sa.sa_chats.SCHEMA.splitlines() if "archived_at" not in line]
    old = "\n".join(lines).replace("finished_at       REAL,", "finished_at       REAL")
    assert "archived_at" not in old
    return old


def test_an_old_database_gains_the_archived_column_in_place(tmp_path, sa):
    import sqlite3

    db = tmp_path / "superagent.db"
    conn = sqlite3.connect(db)
    conn.executescript(_old_schema_without_archived(sa))
    conn.execute(
        "INSERT INTO chat_sessions (id, title, prompt, status, created_at, updated_at)"
        " VALUES ('S-0007', 'older chat', 'older prompt', 'idle', 1.0, 1.0)"
    )
    conn.commit()
    conn.close()

    store = sa_chats.ChatStore(db)

    with sqlite3.connect(db) as check:
        columns = {row[1] for row in check.execute("PRAGMA table_info(chat_sessions)")}
    assert "archived_at" in columns
    # The pre-existing row is readable and simply not archived.
    old = store.get_session("S-0007", message_limit=0)
    assert old["archived_at"] is None
    assert store.list_sessions()["sessions"][0]["id"] == "S-0007"

    archived = store.set_archived("S-0007")
    assert archived["archived_at"] is not None
    # Opening the store again is idempotent: no duplicate-column error.
    sa_chats.ChatStore(db)
    assert sa_chats.ChatStore(db).get_session("S-0007", message_limit=0)["archived_at"]


def test_rename_session_stores_a_trimmed_title(chat):
    session = chat.create_session("fix the parser")
    renamed = chat.rename_session(session["id"], "  Parser crash  ")
    assert renamed["title"] == "Parser crash"
    assert chat.get_session(session["id"], message_limit=0)["title"] == "Parser crash"


def test_rename_session_collapses_whitespace_and_newlines(chat):
    session = chat.create_session("work")
    renamed = chat.rename_session(session["id"], "  One\ttwo\n\nthree  ")
    assert renamed["title"] == "One two three"


def test_rename_session_accepts_a_longer_title_than_a_derived_one(chat):
    session = chat.create_session("work")
    title = "t" * sa_chats.MAX_RENAME_CHARS
    assert chat.rename_session(session["id"], title)["title"] == title


def test_rename_session_rejects_empty_and_oversized_titles(chat):
    session = chat.create_session("work")
    for title in ("", "   ", "\n\t", "x" * (sa_chats.MAX_RENAME_CHARS + 1)):
        try:
            chat.rename_session(session["id"], title)
        except ValueError:
            pass
        else:  # pragma: no cover - every value above must raise
            raise AssertionError(f"title {title!r} should not be accepted")
    # A rejected rename leaves the old title alone.
    assert chat.get_session(session["id"], message_limit=0)["title"] == "work"


def test_rename_unknown_session_returns_none(chat):
    assert chat.rename_session("S-9999", "anything") is None


def test_archive_sets_the_stamp_and_unarchive_clears_it(chat):
    session = chat.create_session("file me away")
    assert session["archived_at"] is None

    archived = chat.set_archived(session["id"])
    assert archived["archived_at"] is not None
    assert chat.get_session(session["id"], message_limit=0)["archived_at"] == archived["archived_at"]

    restored = chat.set_archived(session["id"], archived=False)
    assert restored["archived_at"] is None


def test_archive_is_idempotent_and_does_not_reorder_the_rail(chat):
    session = chat.create_session("keep my place")
    chat.update_session(session["id"], status="review")
    before = chat.get_session(session["id"], message_limit=0)

    first = chat.set_archived(session["id"])
    second = chat.set_archived(session["id"])

    assert second["archived_at"] == first["archived_at"]  # the first moment wins
    assert second["updated_at"] == before["updated_at"]  # filing is not activity


def test_set_archived_unknown_session_returns_none(chat):
    assert chat.set_archived("S-9999") is None
    assert chat.set_archived("S-9999", archived=False) is None


def test_archived_at_is_writable_through_update_session(chat):
    session = chat.create_session("repair path")
    updated = chat.update_session(session["id"], archived_at=123.5)
    assert updated["archived_at"] == 123.5


def test_list_sessions_hides_archived_rows_by_default(chat):
    visible = chat.create_session("visible")
    filed = chat.create_session("filed")
    chat.set_archived(filed["id"])

    data = chat.list_sessions()
    assert [row["id"] for row in data["sessions"]] == [visible["id"]]
    assert data["archived_count"] == 1
    # Counts describe what the rail can actually show.
    assert data["counts"] == {"queued": 1}
    assert data["running"] == 0


def test_list_sessions_archived_only_and_all(chat):
    first = chat.create_session("visible")
    second = chat.create_session("filed")
    chat.set_archived(second["id"])

    only = chat.list_sessions(archived="only")
    assert [row["id"] for row in only["sessions"]] == [second["id"]]
    assert only["archived_count"] == 1
    assert only["counts"] == {"queued": 1}

    everything = chat.list_sessions(archived="all")
    assert {row["id"] for row in everything["sessions"]} == {first["id"], second["id"]}
    assert everything["archived_count"] == 1
    assert everything["counts"] == {"queued": 2}


def test_list_sessions_archived_count_survives_a_status_filter(chat):
    running = chat.create_session("running")
    chat.update_session(running["id"], status="running")
    filed = chat.create_session("filed")
    chat.set_archived(filed["id"])

    data = chat.list_sessions(statuses=["running"])
    assert [row["id"] for row in data["sessions"]] == [running["id"]]
    assert data["archived_count"] == 1


def test_list_sessions_rejects_an_unknown_archived_mode(chat):
    chat.create_session("one")
    try:
        chat.list_sessions(archived="sometimes")
    except ValueError as exc:
        assert "archived" in str(exc)
    else:  # pragma: no cover - the call above must raise
        raise AssertionError("an unknown archived mode should raise")


def test_delete_session_removes_the_row_messages_and_events(chat):
    session = chat.create_session("delete me")
    keep = chat.create_session("keep me")
    chat.add_message(session["id"], "user", "hello")
    chat.add_event(session["id"], "tool", "ls")

    assert chat.delete_session(session["id"]) is True
    assert chat.get_session(session["id"]) is None
    assert chat.get_messages(session["id"]) == []
    assert chat.get_events(session["id"]) == []
    assert [row["id"] for row in chat.list_sessions()["sessions"]] == [keep["id"]]
    # The neighbour's transcript is untouched.
    assert [m["content"] for m in chat.get_messages(keep["id"])] == []


def test_delete_unknown_session_returns_false(chat):
    assert chat.delete_session("S-9999") is False


def test_delete_keeps_the_github_dedupe_rows_and_files_on_disk(tmp_path, chat):
    marker = tmp_path / "worktree.txt"
    marker.write_text("still here", encoding="utf-8")
    session = chat.create_session("github work")
    chat.mark_seen("gh_comment:owner/repo:7")

    assert chat.delete_session(session["id"]) is True
    # A deleted session must not make the watcher replay its comment...
    assert chat.is_seen("gh_comment:owner/repo:7") is True
    # ...and a delete is never a file operation.
    assert marker.read_text(encoding="utf-8") == "still here"


# -- search ---------------------------------------------------------------


def test_search_reports_where_it_matched(chat):
    title_hit = chat.create_session("Fix the postgres migration")
    prompt_hit = chat.create_session(
        "Investigate the flaky test\n\nit mentions websocket frames"
    )
    message_hit = chat.create_session("Unrelated opening line")
    chat.add_message(message_hit["id"], "assistant", "the backoff jitter was missing")

    rows = chat.search_sessions("jitter")["sessions"]
    assert [row["id"] for row in rows] == [message_hit["id"]]
    assert rows[0]["match_field"] == "message"
    assert "backoff jitter" in rows[0]["match_snippet"]

    rows = chat.search_sessions("postgres")["sessions"]
    assert [(row["id"], row["match_field"]) for row in rows] == [
        (title_hit["id"], "title")
    ]

    rows = chat.search_sessions("websocket")["sessions"]
    assert [(row["id"], row["match_field"]) for row in rows] == [
        (prompt_hit["id"], "prompt")
    ]


def test_search_folds_case(chat):
    session = chat.create_session("Fix the Postgres migration")
    assert [row["id"] for row in chat.search_sessions("POSTGRES")["sessions"]] == [
        session["id"]
    ]


def test_search_escapes_like_wildcards(chat):
    percent = chat.create_session("100% coverage")
    chat.create_session("plain task")

    # A literal % must not behave as "match everything"...
    assert [row["id"] for row in chat.search_sessions("%")["sessions"]] == [percent["id"]]
    # ...and a literal _ must not match any single character.
    assert chat.search_sessions("_")["sessions"] == []


def test_search_obeys_archived_visibility(chat):
    visible = chat.create_session("keep me visible")
    filed = chat.create_session("file me away")
    chat.set_archived(filed["id"])

    assert chat.search_sessions("file")["sessions"] == []
    assert [row["id"] for row in chat.search_sessions("file", archived="only")["sessions"]] == [
        filed["id"]
    ]
    assert {row["id"] for row in chat.search_sessions("e", archived="all")["sessions"]} == {
        visible["id"], filed["id"]
    }


def test_search_limit_is_newest_first(chat):
    chat.create_session("alpha one")
    second = chat.create_session("alpha two")
    third = chat.create_session("alpha three")

    rows = chat.search_sessions("alpha", limit=2)["sessions"]
    assert [row["id"] for row in rows] == [third["id"], second["id"]]


def test_search_snippet_is_capped_and_marked(chat):
    session = chat.create_session("long one")
    chat.add_message(
        session["id"], "assistant", ("lorem " * 60) + "NEEDLE" + (" ipsum" * 60)
    )

    snippet = chat.search_sessions("needle")["sessions"][0]["match_snippet"]
    assert len(snippet) <= sa_chats.SEARCH_SNIPPET_CHARS
    assert snippet.startswith("…") and snippet.endswith("…")
    assert "NEEDLE" in snippet


def test_search_without_hits_is_empty(chat):
    chat.create_session("something")
    assert chat.search_sessions("nothing-like-this")["sessions"] == []


def test_blank_search_is_the_plain_list(chat):
    chat.create_session("one")
    chat.create_session("two")

    listed = chat.list_sessions()
    searched = chat.search_sessions("   ")
    assert [row["id"] for row in searched["sessions"]] == [
        row["id"] for row in listed["sessions"]
    ]
    assert all("match_field" not in row for row in searched["sessions"])
    assert searched["counts"] == listed["counts"]
