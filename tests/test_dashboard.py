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
    # Same hermetic HERMES_HOME as settings_api: a developer's real
    # settings.json must not leak default_model into these assertions.
    home = tmp_path / "hermes"
    home.mkdir()
    monkeypatch.setattr(module.sa_settings, "_hermes_home", lambda: home)
    monkeypatch.setattr(module.sa_settings, "config_path", lambda: home / "config.yaml")
    for name in module.sa_settings.ENV_VARS.values():
        monkeypatch.delenv(name, raising=False)
    module.sa_settings.clear_cache()
    yield module, runner
    module.sa_settings.clear_cache()


def test_models_endpoint_reports_defaults(chat_api, monkeypatch):
    api, _ = chat_api
    monkeypatch.setenv("SUPERAGENT_DEFAULT_MODEL", "deepseek-v4.1-flash")
    monkeypatch.setenv("SUPERAGENT_DEFAULT_PROVIDER", "opencode-go")
    monkeypatch.delenv("SUPERAGENT_MODEL_PRESETS", raising=False)
    body = api.list_models()
    assert body["default_model"] == "deepseek-v4.1-flash"
    assert body["default_provider"] == "opencode-go"
    # No presets configured -> no chips; the flat list carries the default only.
    assert body["presets"] == []
    assert body["models"] == ["deepseek-v4.1-flash"]


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
    assert module.list_chats() == {
        "db_exists": False, "sessions": [], "counts": {}, "running": 0,
        "archived_count": 0,
    }


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


# -- live grid enrichment (v0.2, iteration 3) ------------------------------


def test_list_chats_enriches_live_rows(chat_api):
    api, _ = chat_api
    api.create_chat({"prompt": "live work"})  # FakeRunner leaves it running

    row = api.list_chats()["sessions"][0]
    assert isinstance(row["recent_events"], list) and row["recent_events"]
    assert set(row["recent_events"][0]) == {"kind", "detail", "created_at"}
    assert row["recent_events"][0]["kind"] == "launched"  # newest first
    assert isinstance(row["elapsed_seconds"], int)


def test_list_chats_keeps_historical_rows_slim(chat_api):
    api, _ = chat_api
    created = api.create_chat({"prompt": "finished"})["session"]
    store = api.sa_chats.ChatStore(api._db_path())
    store.update_session(created["id"], status="idle", finished_at=api.time.time(), tokens=1200)

    row = api.list_chats()["sessions"][0]
    assert "recent_events" not in row
    assert row["elapsed_seconds"] is None
    assert row["tokens"] == 1200


def test_list_chats_filters_by_status(chat_api):
    api, _ = chat_api
    live = api.create_chat({"prompt": "live"})["session"]
    done = api.create_chat({"prompt": "done"})["session"]
    store = api.sa_chats.ChatStore(api._db_path())
    store.update_session(done["id"], status="idle")

    body = api.list_chats(status="running")
    assert [row["id"] for row in body["sessions"]] == [live["id"]]
    assert body["counts"]["idle"] == 1  # counts stay board-wide
    assert len(api.list_chats()["sessions"]) == 2  # empty filter = default


def test_set_chat_model_applies_to_the_next_turn(chat_api):
    api, runner = chat_api
    created = api.create_chat({"prompt": "first", "model": "old-model"})["session"]
    store = api.sa_chats.ChatStore(api._db_path())
    store.update_session(created["id"], status="idle", hermes_session_id="local-77")

    body = api.set_chat_model(
        created["id"], {"model": "qwen/qwen3.8-27b", "provider": "opencode-go"}
    )
    assert body["ok"] is True
    assert body["session"]["model"] == "qwen/qwen3.8-27b"
    assert body["session"]["provider"] == "opencode-go"

    detail = api.get_chat(created["id"])["session"]
    assert detail["events"][-1]["kind"] == "model"
    assert "qwen/qwen3.8-27b" in detail["events"][-1]["detail"]

    api.post_chat_message(created["id"], {"content": "carry on"})
    assert runner.launches[-1]["model"] == "qwen/qwen3.8-27b"
    assert runner.launches[-1]["provider"] == "opencode-go"


def test_set_chat_model_rejects_an_empty_choice(chat_api):
    api, _ = chat_api
    created = api.create_chat({"prompt": "first"})["session"]
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as excinfo:
        api.set_chat_model(created["id"], {"model": "   ", "provider": ""})
    assert excinfo.value.status_code == 400


def test_set_chat_model_conflicts_while_a_turn_runs(chat_api):
    api, _ = chat_api
    created = api.create_chat({"prompt": "live"})["session"]
    assert created["status"] == "running"
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as excinfo:
        api.set_chat_model(created["id"], {"model": "qwen/qwen3.8-27b"})
    assert excinfo.value.status_code == 409


def test_set_chat_model_unknown_session_is_404(chat_api):
    api, _ = chat_api
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as excinfo:
        api.set_chat_model("S-9999", {"model": "qwen/qwen3.8-27b"})
    assert excinfo.value.status_code == 404


def test_set_chat_model_accepts_a_provider_only_change(chat_api):
    api, _ = chat_api
    created = api.create_chat({"prompt": "first", "model": "old-model"})["session"]
    store = api.sa_chats.ChatStore(api._db_path())
    store.update_session(created["id"], status="review")

    body = api.set_chat_model(created["id"], {"provider": "opencode-go"})
    assert body["ok"] is True
    assert body["session"]["model"] == ""  # clearing the pin is explicit
    assert body["session"]["provider"] == "opencode-go"


# -- settings + model presets (v0.4) ---------------------------------------


@pytest.fixture
def settings_api(tmp_path, monkeypatch):
    """A chat API whose settings live in a throwaway Hermes home."""
    module = load_api()
    db = tmp_path / "superagent.db"
    runner = FakeRunner(store=module.sa_chats.ChatStore(db))
    monkeypatch.setattr(module, "_db_path", lambda: db)
    monkeypatch.setattr(module, "_get_runner", lambda: runner)
    home = tmp_path / "hermes"
    home.mkdir()
    monkeypatch.setattr(module.sa_settings, "_hermes_home", lambda: home)
    monkeypatch.setattr(module.sa_settings, "config_path", lambda: home / "config.yaml")
    for name in module.sa_settings.ENV_VARS.values():
        monkeypatch.delenv(name, raising=False)
    module.sa_settings.clear_cache()
    yield module, runner
    module.sa_settings.clear_cache()


def test_get_settings_returns_the_public_surface(settings_api):
    api, _ = settings_api
    body = api.get_settings()
    for key in (
        "mode",
        "repo_path",
        "default_model",
        "default_provider",
        "model_presets",
        "max_workers",
        "worker_yolo",
        "chat_max_turns",
        "github_repo",
        "github_agent_login",
        "github_allowed_users",
        "github_watch_mode",
        "github_watch_new_prs",
        "github_poll_seconds",
        "github_token_set",
    ):
        assert key in body
    assert body["mode"] == "observe"
    assert body["max_workers"] == 2
    assert body["github_token_set"] is False
    assert "github_token" not in body


def test_get_settings_never_returns_the_token(settings_api):
    api, _ = settings_api
    api.sa_settings.save_settings({"github_token": "ghp_secret", "github_repo": "owner/name"})
    body = api.get_settings()
    assert body["github_token_set"] is True
    assert body["github_repo"] == "owner/name"
    assert "ghp_secret" not in api.json.dumps(body)


def test_post_settings_saves_every_editable_key(settings_api, tmp_path):
    api, _ = settings_api
    repo = tmp_path / "clone"
    repo.mkdir()
    saved = api.update_settings(
        {
            "mode": "auto",
            "repo_path": str(repo),
            "default_model": "qwen/qwen3.8-27b",
            "default_provider": "opencode-go",
            "model_presets": [
                "fast=opencode-go:qwen/qwen3.8-27b",
                {"name": "strong", "model": "z-ai/glm-5.3-flash", "provider": "opencode-go"},
            ],
            "max_workers": 4,
            "worker_yolo": False,
            "chat_max_turns": 30,
        }
    )
    assert saved["ok"] is True
    settings = saved["settings"]
    assert settings["mode"] == "auto"
    assert settings["repo_path"] == str(repo.resolve())
    assert settings["default_model"] == "qwen/qwen3.8-27b"
    assert settings["default_provider"] == "opencode-go"
    assert settings["model_presets"] == [
        {"name": "fast", "model": "qwen/qwen3.8-27b", "provider": "opencode-go"},
        {"name": "strong", "model": "z-ai/glm-5.3-flash", "provider": "opencode-go"},
    ]
    assert settings["max_workers"] == 4
    assert settings["worker_yolo"] is False
    assert settings["chat_max_turns"] == 30

    # A second GET reads the same file back.
    assert api.get_settings()["model_presets"] == settings["model_presets"]


def test_post_settings_accepts_a_partial_patch(settings_api):
    api, _ = settings_api
    api.update_settings({"mode": "auto", "max_workers": 3})
    api.update_settings({"chat_max_turns": 12})
    body = api.get_settings()
    assert body["mode"] == "auto"
    assert body["max_workers"] == 3
    assert body["chat_max_turns"] == 12


def test_post_settings_resets_the_runner_singleton(settings_api):
    api, _ = settings_api
    key = str(api._db_path())
    api._RUNNERS[key] = object()
    api.update_settings({"max_workers": 5})
    assert key not in api._RUNNERS


@pytest.mark.parametrize(
    "patch",
    [
        {"mode": "later"},
        {"default_model": "x" * 201},
        {"default_provider": 12},
        {"model_presets": "fast=one"},
        {"model_presets": ["fast=one", "FAST=two"]},
        {"model_presets": [{"name": "empty"}]},
        {"model_presets": [f"m{index}" for index in range(13)]},
        {"max_workers": 0},
        {"max_workers": 9},
        {"max_workers": True},
        {"worker_yolo": "maybe"},
        {"chat_max_turns": 0},
        {"chat_max_turns": 501},
        {"unknown_setting": 1},
        {"repo_path": 42},
    ],
)
def test_post_settings_rejects_invalid_values(settings_api, patch):
    api, _ = settings_api
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as excinfo:
        api.update_settings(patch)
    assert excinfo.value.status_code == 400
    # Nothing was written.
    assert api.get_settings()["mode"] == "observe"


def test_post_settings_rejects_a_missing_repo_path(settings_api, tmp_path):
    api, _ = settings_api
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as excinfo:
        api.update_settings({"repo_path": str(tmp_path / "nope")})
    assert excinfo.value.status_code == 400
    assert "does not exist" in excinfo.value.detail


def test_models_reports_named_presets_and_the_flat_list(settings_api):
    api, _ = settings_api
    api.update_settings(
        {
            "default_model": "one",
            "default_provider": "prov",
            "model_presets": ["fast=prov:one", {"name": "strong", "model": "two"}],
        }
    )
    body = api.list_models()
    assert body["default_model"] == "one"
    assert body["default_provider"] == "prov"
    assert body["models"] == ["one", "two"]
    assert body["presets"] == [
        {"name": "fast", "model": "one", "provider": "prov"},
        {"name": "strong", "model": "two", "provider": ""},
    ]


def test_models_without_presets_advertises_nothing(settings_api):
    api, _ = settings_api
    body = api.list_models()
    assert body["presets"] == []
    assert body["models"] == []
    assert body["default_model"] == ""


def test_models_appends_the_default_model_to_the_flat_list(settings_api):
    api, _ = settings_api
    api.update_settings({"default_model": "solo", "default_provider": "prov"})
    body = api.list_models()
    assert body["presets"] == []
    assert body["models"] == ["solo"]


def test_models_keeps_preset_order_then_the_default(settings_api):
    api, _ = settings_api
    api.update_settings(
        {
            "default_model": "zzz-default",
            "model_presets": ["fast=prov:one", {"name": "strong", "model": "two"}],
        }
    )
    body = api.list_models()
    assert body["models"] == ["one", "two", "zzz-default"]


def test_create_chat_resolves_a_named_preset(settings_api):
    api, runner = settings_api
    api.update_settings({"model_presets": ["fast=opencode-go:qwen/qwen3.8-27b"]})

    body = api.create_chat({"prompt": "Use the fast preset", "preset": "fast"})
    assert body["ok"] is True
    session = body["session"]
    assert session["model"] == "qwen/qwen3.8-27b"
    assert session["provider"] == "opencode-go"
    assert runner.launches[-1]["model"] == "qwen/qwen3.8-27b"
    assert runner.launches[-1]["provider"] == "opencode-go"

    detail = api.get_chat(session["id"])["session"]
    kinds = [event["kind"] for event in detail["events"]]
    assert kinds == ["created", "preset", "launched"]
    assert "fast" in detail["events"][1]["detail"]


def test_create_chat_explicit_model_beats_the_preset(settings_api):
    api, runner = settings_api
    api.update_settings({"model_presets": ["fast=opencode-go:qwen/qwen3.8-27b"]})

    body = api.create_chat(
        {
            "prompt": "override",
            "preset": "fast",
            "model": "explicit-model",
            "provider": "explicit-provider",
        }
    )
    assert body["session"]["model"] == "explicit-model"
    assert body["session"]["provider"] == "explicit-provider"
    assert runner.launches[-1]["model"] == "explicit-model"


def test_create_chat_unknown_preset_lists_the_available_names(settings_api):
    api, _ = settings_api
    api.update_settings({"model_presets": ["fast=one", "strong=two"]})
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as excinfo:
        api.create_chat({"prompt": "work", "preset": "missing"})
    assert excinfo.value.status_code == 400
    assert "missing" in excinfo.value.detail
    assert "fast" in excinfo.value.detail and "strong" in excinfo.value.detail


def test_create_chat_unknown_preset_without_configuration_says_none(settings_api):
    api, _ = settings_api
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as excinfo:
        api.create_chat({"prompt": "work", "preset": "missing"})
    assert excinfo.value.status_code == 400
    assert "none configured" in excinfo.value.detail


def test_create_chat_without_a_preset_uses_the_saved_defaults(settings_api):
    api, _ = settings_api
    api.update_settings({"default_model": "saved-model", "default_provider": "saved-provider"})

    body = api.create_chat({"prompt": "defaults"})
    assert body["session"]["model"] == "saved-model"
    assert body["session"]["provider"] == "saved-provider"


# -- session management (iteration 14) -------------------------------------
#
# Rename / archive / delete are the housekeeping the rail's ⋯ menu calls. Each
# one is validated here: the status codes are the UI's contract, and a delete
# must never reach beyond the two chat tables.


def _idle(api, session_id, status="idle"):
    """Put a session into a finished state so housekeeping is allowed."""
    store = api.sa_chats.ChatStore(api._db_path())
    store.update_session(session_id, status=status)
    return store


def test_rename_chat_updates_the_title(chat_api):
    api, _ = chat_api
    created = api.create_chat({"prompt": "first"})["session"]
    _idle(api, created["id"])

    body = api.rename_chat(created["id"], {"title": "  Parser crash  "})
    assert body["ok"] is True
    assert body["session"]["title"] == "Parser crash"
    assert api.get_chat(created["id"])["session"]["title"] == "Parser crash"


def test_rename_chat_rejects_an_empty_title(chat_api):
    api, _ = chat_api
    created = api.create_chat({"prompt": "first"})["session"]
    from fastapi import HTTPException

    for title in ("", "   ", None):
        with pytest.raises(HTTPException) as excinfo:
            api.rename_chat(created["id"], {"title": title})
        assert excinfo.value.status_code == 400
    with pytest.raises(HTTPException) as missing:
        api.rename_chat(created["id"], {})
    assert missing.value.status_code == 400


def test_rename_chat_rejects_a_too_long_title(chat_api):
    api, _ = chat_api
    created = api.create_chat({"prompt": "first"})["session"]
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as excinfo:
        api.rename_chat(created["id"], {"title": "x" * (api.sa_chats.MAX_RENAME_CHARS + 1)})
    assert excinfo.value.status_code == 400
    assert "too long" in excinfo.value.detail


def test_rename_chat_rejects_a_non_string_title(chat_api):
    api, _ = chat_api
    created = api.create_chat({"prompt": "first"})["session"]
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as excinfo:
        api.rename_chat(created["id"], {"title": 42})
    assert excinfo.value.status_code == 400


def test_rename_unknown_chat_is_404(chat_api):
    api, _ = chat_api
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as excinfo:
        api.rename_chat("S-9999", {"title": "anything"})
    assert excinfo.value.status_code == 404


def test_rename_chat_is_allowed_while_a_turn_runs(chat_api):
    """A title is not a turn input: renaming a live chat is harmless."""
    api, _ = chat_api
    created = api.create_chat({"prompt": "live"})["session"]
    assert created["status"] == "running"
    body = api.rename_chat(created["id"], {"title": "renamed mid-turn"})
    assert body["session"]["title"] == "renamed mid-turn"
    assert body["session"]["status"] == "running"


def test_archive_and_unarchive_a_chat(chat_api):
    api, _ = chat_api
    created = api.create_chat({"prompt": "file me"})["session"]
    _idle(api, created["id"])

    archived = api.archive_chat(created["id"], {})
    assert archived["ok"] is True
    assert archived["session"]["archived_at"] is not None

    listing = api.list_chats()
    assert created["id"] not in [row["id"] for row in listing["sessions"]]
    assert listing["archived_count"] == 1

    restored = api.archive_chat(created["id"], {"archived": False})
    assert restored["session"]["archived_at"] is None
    assert [row["id"] for row in api.list_chats()["sessions"]] == [created["id"]]


def test_archive_accepts_an_explicit_true(chat_api):
    api, _ = chat_api
    created = api.create_chat({"prompt": "file me"})["session"]
    _idle(api, created["id"])
    body = api.archive_chat(created["id"], {"archived": True})
    assert body["session"]["archived_at"] is not None


def test_archive_conflicts_while_a_turn_runs(chat_api):
    api, _ = chat_api
    created = api.create_chat({"prompt": "live"})["session"]
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as excinfo:
        api.archive_chat(created["id"], {})
    assert excinfo.value.status_code == 409
    assert "wait for the turn to finish" in excinfo.value.detail


def test_archive_unknown_chat_is_404(chat_api):
    api, _ = chat_api
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as excinfo:
        api.archive_chat("S-9999", {})
    assert excinfo.value.status_code == 404


def test_archive_rejects_a_non_boolean_flag(chat_api):
    api, _ = chat_api
    created = api.create_chat({"prompt": "file me"})["session"]
    _idle(api, created["id"])
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as excinfo:
        api.archive_chat(created["id"], {"archived": "sometimes"})
    assert excinfo.value.status_code == 400


def test_delete_chat_removes_the_session_and_its_transcript(chat_api):
    api, _ = chat_api
    created = api.create_chat({"prompt": "delete me"})["session"]
    store = _idle(api, created["id"])
    store.add_message(created["id"], "user", "hello")
    store.add_event(created["id"], "tool", "ls")
    other = api.create_chat({"prompt": "keep me"})["session"]

    body = api.delete_chat(created["id"])
    assert body == {"ok": True, "id": created["id"]}
    assert api.sa_chats.ChatStore(api._db_path()).get_session(created["id"]) is None
    assert [row["id"] for row in api.list_chats()["sessions"]] == [other["id"]]

    from fastapi import HTTPException

    with pytest.raises(HTTPException) as excinfo:
        api.get_chat(created["id"])
    assert excinfo.value.status_code == 404


def test_delete_chat_keeps_the_github_dedupe_rows(chat_api):
    api, _ = chat_api
    created = api.create_chat({"prompt": "github work"})["session"]
    store = _idle(api, created["id"])
    store.mark_seen("gh_head:owner/repo:1:abc")

    api.delete_chat(created["id"])
    assert api.sa_chats.ChatStore(api._db_path()).is_seen("gh_head:owner/repo:1:abc")


def test_delete_conflicts_while_a_turn_runs(chat_api):
    api, _ = chat_api
    created = api.create_chat({"prompt": "live"})["session"]
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as excinfo:
        api.delete_chat(created["id"])
    assert excinfo.value.status_code == 409
    assert "stop it before deleting" in excinfo.value.detail


def test_delete_unknown_chat_is_404(chat_api):
    api, _ = chat_api
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as excinfo:
        api.delete_chat("S-9999")
    assert excinfo.value.status_code == 404


def test_list_chats_archived_filter(chat_api):
    api, _ = chat_api
    visible = api.create_chat({"prompt": "visible"})["session"]
    filed = api.create_chat({"prompt": "filed"})["session"]
    _idle(api, visible["id"])
    _idle(api, filed["id"])
    api.archive_chat(filed["id"], {})

    hidden = api.list_chats()
    assert [row["id"] for row in hidden["sessions"]] == [visible["id"]]
    assert hidden["archived_count"] == 1
    assert hidden["counts"] == {"idle": 1}

    only = api.list_chats(archived="only")
    assert [row["id"] for row in only["sessions"]] == [filed["id"]]
    assert only["counts"] == {"idle": 1}

    everything = api.list_chats(archived="all")
    assert {row["id"] for row in everything["sessions"]} == {visible["id"], filed["id"]}
    assert everything["counts"] == {"idle": 2}


def test_list_chats_rejects_an_unknown_archived_mode(chat_api):
    api, _ = chat_api
    api.create_chat({"prompt": "one"})
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as excinfo:
        api.list_chats(archived="sometimes")
    assert excinfo.value.status_code == 400
    assert "archived" in excinfo.value.detail


def test_list_chats_treats_an_empty_archived_mode_as_hide(chat_api):
    api, _ = chat_api
    created = api.create_chat({"prompt": "one"})["session"]
    _idle(api, created["id"])
    api.archive_chat(created["id"], {})
    assert api.list_chats(archived="")["sessions"] == []


def test_health_reports_the_api_shape(api):
    body = api.health()
    assert body["api_version"] == 2
    assert body["features"] == [
        "chats", "settings", "github", "diff", "session_management",
    ]
    assert isinstance(body["pid"], int) and body["pid"] > 0
    assert isinstance(body["started_at"], float)
    assert body["started_at"] <= api.time.time()
    assert body["archived_count"] == 0


def test_health_counts_archived_sessions(chat_api):
    api, _ = chat_api
    created = api.create_chat({"prompt": "file me"})["session"]
    _idle(api, created["id"])
    api.archive_chat(created["id"], {})
    assert api.health()["archived_count"] == 1


def test_health_on_a_database_without_chat_tables(tmp_path, monkeypatch):
    """An old ticket-only database must not make /health fail."""
    import sqlite3

    module = load_api()
    db = tmp_path / "superagent.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE tickets (id TEXT PRIMARY KEY)")
    conn.commit()
    conn.close()
    monkeypatch.setattr(module, "_db_path", lambda: db)
    body = module.health()
    assert body["ok"] is True
    assert body["db_exists"] is True
    assert body["archived_count"] == 0



# -- chat search -----------------------------------------------------------


def test_list_chats_search_filters_and_explains_the_hit(chat_api):
    api, _ = chat_api
    store = api.sa_chats.ChatStore(api._db_path())
    hit = store.create_session("Fix the parser crash")
    store.add_message(
        hit["id"], "assistant", "the stack overflow came from recursive descent"
    )
    store.create_session("Add a regression test")

    body = api.list_chats(q="  stack overflow  ")
    assert [row["id"] for row in body["sessions"]] == [hit["id"]]
    row = body["sessions"][0]
    assert row["match_field"] == "message"
    assert "stack overflow" in row["match_snippet"]
    # Counts still describe the board, not the hit list.
    assert sum(body["counts"].values()) == 2


def test_list_chats_empty_query_keeps_the_plain_list(chat_api):
    api, _ = chat_api
    store = api.sa_chats.ChatStore(api._db_path())
    store.create_session("Fix the parser crash")

    row = api.list_chats(q="")["sessions"][0]
    assert "match_field" not in row


def test_list_chats_rejects_an_overlong_query(chat_api):
    api, _ = chat_api
    with pytest.raises(api.HTTPException) as excinfo:
        api.list_chats(q="x" * (api.sa_chats.SEARCH_QUERY_CHARS + 1))
    assert excinfo.value.status_code == 400
