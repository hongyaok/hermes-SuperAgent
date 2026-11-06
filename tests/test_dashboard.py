"""Tests for the read-only dashboard backend (dashboard/plugin_api.py).

The backend is imported by the gateway on its own, so it is loaded here the
same way — standalone, with the DB path patched to a throwaway database.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load_api():
    path = ROOT / "dashboard" / "plugin_api.py"
    spec = importlib.util.spec_from_file_location("superagent_plugin_api", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["superagent_plugin_api"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def api(tmp_path, monkeypatch, sa):
    module = load_api()
    db = tmp_path / "superagent.db"
    store = sa.sa_store.Store(db)
    store.create_ticket(source="local", request="Fix the parser crash",
                        status="observed", verdict="accept", reason="explicit")
    store.create_ticket(source="local", request="Add a regression test",
                        status="queued", verdict="accept", reason="explicit")
    monkeypatch.setattr(module, "_db_path", lambda: db)
    return module


def test_health_reports_version_and_db(api):
    body = api.health()
    assert body["ok"] is True
    assert body["db_exists"] is True
    assert body["version"]


def test_tickets_lists_attention_and_recent(api):
    body = api.list_tickets()
    assert body["db_exists"] is True
    assert body["counts"]["observed"] == 1
    assert body["counts"]["queued"] == 1
    assert len(body["attention"]) == 1
    assert body["attention"][0]["status"] == "observed"
    assert len(body["tickets"]) == 2


def test_ticket_detail_includes_events(api):
    first = api.list_tickets()["tickets"][0]
    detail = api.get_ticket(first["id"])
    assert detail["id"] == first["id"]
    assert detail["events"]


def test_unknown_ticket_is_404(api):
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as excinfo:
        api.get_ticket("T-9999")
    assert excinfo.value.status_code == 404


def test_missing_db_returns_empty_not_an_error(tmp_path, monkeypatch):
    module = load_api()
    monkeypatch.setattr(module, "_db_path", lambda: tmp_path / "nope.db")
    assert module.health()["db_exists"] is False
    body = module.list_tickets()
    assert body == {"db_exists": False, "counts": {}, "attention": [], "tickets": []}


# -- chat sessions (v0.2) -------------------------------------------------


class FakeRunner:
    """Records launches; mirrors the store writes the real Runner performs."""

    def __init__(self, store=None, fail=None):
        self.store = store
        self.fail = fail
        self.launches = []
        self.stopped = []

    def launch_turn(
        self, session_id, prompt, *, resume_session_id=None, model=None, provider=None, repo=None
    ):
        if self.fail:
            raise RuntimeError(self.fail)
        self.launches.append(
            {
                "session_id": session_id,
                "prompt": prompt,
                "resume_session_id": resume_session_id,
                "model": model,
                "provider": provider,
                "repo": repo,
            }
        )
        if self.store is not None:
            self.store.update_session(session_id, status="running", pid=4321)
        return {"session_id": session_id}

    def stop(self, session_id):
        self.stopped.append(session_id)
        if self.store is not None:
            self.store.update_session(
                session_id, status="cancelled", error="cancelled by user", pid=None
            )
        return True


@pytest.fixture
def chat_api(tmp_path, monkeypatch):
    module = load_api()
    db = tmp_path / "superagent.db"
    runner = FakeRunner(store=module.sa_chats.ChatStore(db))
    monkeypatch.setattr(module, "_db_path", lambda: db)
    monkeypatch.setattr(module, "_get_runner", lambda: runner)
    return module, runner


def test_models_endpoint_reports_defaults(chat_api, monkeypatch):
    api, _ = chat_api
    monkeypatch.setenv("SUPERAGENT_DEFAULT_MODEL", "deepseek-v4.1-flash")
    monkeypatch.setenv("SUPERAGENT_DEFAULT_PROVIDER", "opencode-go")
    monkeypatch.delenv("SUPERAGENT_MODEL_PRESETS", raising=False)
    body = api.list_models()
    assert body["default_model"] == "deepseek-v4.1-flash"
    assert body["default_provider"] == "opencode-go"
    assert api.MODEL_PRESETS[0] in body["models"]


def test_models_presets_can_come_from_env(chat_api, monkeypatch):
    api, _ = chat_api
    monkeypatch.setenv("SUPERAGENT_MODEL_PRESETS", "m-one, m-two")
    assert api.list_models()["models"] == ["m-one", "m-two"]


def test_create_chat_launches_a_worker(chat_api):
    api, runner = chat_api
    body = api.create_chat({"prompt": "Fix the parser crash"})
    assert body["ok"] is True
    assert body["launched"] is True
    session = body["session"]
    assert session["id"] == "S-0001"
    assert session["title"] == "Fix the parser crash"
    assert session["status"] == "running"  # FakeRunner marks it running
    assert runner.launches[0]["session_id"] == "S-0001"
    assert runner.launches[0]["prompt"] == "Fix the parser crash"

    listing = api.list_chats()
    assert listing["db_exists"] is True
    assert [row["id"] for row in listing["sessions"]] == ["S-0001"]
    assert listing["counts"]["running"] == 1
    assert listing["running"] == 1

    detail = api.get_chat("S-0001")["session"]
    assert detail["prompt"] == "Fix the parser crash"
    assert detail["messages"] == []
    assert [event["kind"] for event in detail["events"]] == ["created", "launched"]


def test_create_chat_rejects_bad_prompts(chat_api):
    api, _ = chat_api
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as empty:
        api.create_chat({"prompt": "   "})
    assert empty.value.status_code == 400
    with pytest.raises(HTTPException) as long_prompt:
        api.create_chat({"prompt": "x" * (api.MAX_PROMPT_CHARS + 1)})
    assert long_prompt.value.status_code == 400


def test_create_chat_reports_a_missing_binary(chat_api):
    api, runner = chat_api
    runner.fail = "hermes executable not found; install hermes or set SUPERAGENT_HERMES_BIN"
    body = api.create_chat({"prompt": "Fix the parser crash"})
    assert body["ok"] is False
    assert body["launched"] is False
    assert "hermes" in body["error"]
    assert body["session"]["status"] == "failed"
    assert "hermes" in api.get_chat(body["session"]["id"])["session"]["error"]


def test_unknown_chat_is_404(chat_api):
    api, _ = chat_api
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as excinfo:
        api.get_chat("S-9999")
    assert excinfo.value.status_code == 404


def test_post_message_resumes_the_hermes_session(chat_api):
    api, runner = chat_api
    created = api.create_chat({"prompt": "first"})["session"]
    store = api.sa_chats.ChatStore(api._db_path())
    store.update_session(created["id"], status="idle", hermes_session_id="local-77")

    body = api.post_chat_message(created["id"], {"content": "and now?"})
    assert body["ok"] is True
    assert body["resumed"] is True
    assert runner.launches[-1]["resume_session_id"] == "local-77"
    assert runner.launches[-1]["prompt"] == "and now?"
    assert [m["role"] for m in api.get_chat(created["id"])["session"]["messages"]] == ["user"]


def test_post_message_conflicts_while_a_turn_runs(chat_api):
    api, _ = chat_api
    created = api.create_chat({"prompt": "first"})["session"]
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as excinfo:
        api.post_chat_message(created["id"], {"content": "too soon"})
    assert excinfo.value.status_code == 409


def test_post_message_starts_a_new_chat_when_nothing_to_resume(chat_api):
    api, runner = chat_api
    created = api.create_chat({"prompt": "first"})["session"]
    store = api.sa_chats.ChatStore(api._db_path())
    store.update_session(created["id"], status="failed", error="hermes crashed")

    body = api.post_chat_message(created["id"], {"content": "try again"})
    assert body["ok"] is True
    assert body["resumed"] is False
    assert body["previous_session_id"] == created["id"]
    assert body["session"]["id"] != created["id"]
    assert runner.launches[-1]["session_id"] == body["session"]["id"]
    assert runner.launches[-1]["resume_session_id"] is None
    assert [m["content"] for m in body["session"]["messages"]] == ["try again"]


def test_stop_cancels_a_running_chat(chat_api):
    api, runner = chat_api
    created = api.create_chat({"prompt": "long job"})["session"]
    body = api.stop_chat(created["id"])
    assert body["ok"] is True
    assert body["stopped"] is True
    assert runner.stopped == [created["id"]]
    assert body["session"]["status"] == "cancelled"


def test_stop_conflicts_when_nothing_is_running(chat_api):
    api, _ = chat_api
    created = api.create_chat({"prompt": "done already"})["session"]
    store = api.sa_chats.ChatStore(api._db_path())
    store.update_session(created["id"], status="review")
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as excinfo:
        api.stop_chat(created["id"])
    assert excinfo.value.status_code == 409


def test_chats_list_on_a_missing_db(tmp_path, monkeypatch):
    module = load_api()
    monkeypatch.setattr(module, "_db_path", lambda: tmp_path / "nope.db")
    assert module.list_chats() == {"db_exists": False, "sessions": [], "counts": {}, "running": 0}


def test_a_chat_only_database_keeps_ticket_views_working(tmp_path, monkeypatch):
    """The first write on a fresh install may be a chat: /tickets must still answer."""
    module = load_api()
    db = tmp_path / "superagent.db"
    monkeypatch.setattr(module, "_db_path", lambda: db)
    monkeypatch.setattr(module, "_get_runner", lambda: FakeRunner())

    module.create_chat({"prompt": "first ever request"})

    assert module.list_sessions()["sessions"] == []
    assert module.list_tickets()["tickets"] == []


def test_ticket_sessions_endpoint_is_unchanged(api):
    """The v0.2 chat endpoints must not disturb the ticket sessions grid."""
    body = api.list_sessions()
    assert body["db_exists"] is True
    assert {row["id"] for row in body["sessions"]} == {"T-0001", "T-0002"}

