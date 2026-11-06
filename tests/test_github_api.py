"""Tests for the GitHub dashboard endpoints (dashboard/plugin_api.py).

The API module is loaded standalone, exactly as the gateway loads it. The
GitHub client is replaced with a canned fake and the watcher with a recorder,
so no request leaves the machine; settings are written to a throwaway
HERMES_HOME.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TOKEN = "ghp_api_secret_123456"
REPO = "owner/name"


def load_api():
    path = ROOT / "dashboard" / "plugin_api.py"
    spec = importlib.util.spec_from_file_location("superagent_plugin_api_github", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["superagent_plugin_api_github"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def api(tmp_path, monkeypatch):
    module = load_api()
    db = tmp_path / "superagent.db"
    home = tmp_path / "hermes"
    home.mkdir()
    monkeypatch.setattr(module, "_db_path", lambda: db)
    monkeypatch.setattr(module.sa_settings, "_hermes_home", lambda: home)
    monkeypatch.setattr(module.sa_settings, "config_path", lambda: home / "config.yaml")
    for name in module.sa_settings.ENV_VARS.values():
        monkeypatch.delenv(name, raising=False)
    module.sa_settings.clear_cache()
    yield module
    module.sa_settings.clear_cache()


def pr(number=7, title="Add parser tests", author="alice"):
    return {
        "number": number,
        "title": title,
        "body": "Closes #3",
        "author": author,
        "state": "open",
        "head_ref": "feature/parser",
        "head_sha": "a" * 40,
        "base_ref": "main",
        "html_url": f"https://github.com/{REPO}/pull/{number}",
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-02T00:00:00Z",
        "draft": False,
        "repo": REPO,
    }


class FakeWatcher:
    def __init__(self, status=None, poll_result=None, error=""):
        self._status = status or {
            "configured": True,
            "repo": REPO,
            "login": "agent",
            "token_set": True,
            "open_prs": [pr()],
            "last_poll": None,
            "last_error": "",
            "watching": False,
            "watch_mode": "observe",
            "poll_seconds": 90,
            "sessions_created": 0,
        }
        self.poll_result = poll_result or {
            "configured": True,
            "polled": 2,
            "created": ["S-0001"],
            "errors": [],
        }
        self.error = error
        self.started = 0
        self.stopped = 0
        self.polls = 0

    def status(self):
        return dict(self._status)

    def start_background(self):
        self.started += 1
        return True

    def stop_background(self):
        self.stopped += 1

    def poll_once(self):
        self.polls += 1
        if self.error:
            raise RuntimeError(self.error)
        return dict(self.poll_result)


def install_watcher(api, monkeypatch, **kwargs):
    """Register a fake watcher for this db (and answer _get_watcher with it)."""
    watcher = FakeWatcher(**kwargs)
    monkeypatch.setitem(api._WATCHERS, str(api._db_path()), watcher)
    monkeypatch.setattr(api, "_get_watcher", lambda: watcher)
    return watcher


def install_client(api, monkeypatch, *, prs=None, fail="", login="agent", info=None):
    """Patch GitHubClient with a canned class and return it."""

    class FakeClient:
        instances = []

        def __init__(self, token, repo, timeout=20):
            self.token = token
            self.repo = repo
            FakeClient.instances.append(self)

        def me(self):
            if fail:
                raise api.sa_github.GitHubError(fail, 401)
            return {"login": login, "id": 1}

        def repo_info(self):
            if fail:
                raise api.sa_github.GitHubError(fail, 401)
            return info or {
                "full_name": self.repo,
                "default_branch": "main",
                "private": False,
                "open_issues": 3,
            }

        def list_open_prs(self, limit=10):
            if fail:
                raise api.sa_github.GitHubError(fail, 401)
            return [dict(item) for item in (prs or [])]

        def list_comments(self, number):
            return []

        def get_pr(self, number):
            for item in prs or []:
                if item["number"] == number:
                    return dict(item)
            raise api.sa_github.GitHubError(f"pull request #{number} was not found", 404)

    monkeypatch.setattr(api.sa_github, "GitHubClient", FakeClient)
    return FakeClient


class FakeRunner:
    def __init__(self, store=None, fail=""):
        self.store = store
        self.fail = fail
        self.launches = []

    def launch_turn(
        self, session_id, prompt, *, resume_session_id=None, model=None, provider=None, repo=None
    ):
        if self.fail:
            raise RuntimeError(self.fail)
        self.launches.append(
            {
                "session_id": session_id,
                "prompt": prompt,
                "model": model,
                "provider": provider,
                "repo": repo,
            }
        )
        if self.store is not None:
            self.store.update_session(session_id, status="running", pid=4321)
        return {"session_id": session_id}


def assert_no_token(payload, token=TOKEN):
    assert token not in json.dumps(payload, default=str)


# -- status --------------------------------------------------------------


def test_status_reports_the_watcher_and_starts_it(api, monkeypatch):
    watcher = install_watcher(api, monkeypatch)
    body = api.github_status()
    assert body["configured"] is True
    assert body["repo"] == REPO
    assert body["open_prs"][0]["number"] == 7
    assert watcher.started == 1
    assert_no_token(body)


# -- test ----------------------------------------------------------------


def test_test_endpoint_reports_a_masked_repository(api, monkeypatch):
    api.sa_settings.save_settings({"github_token": TOKEN})
    install_client(api, monkeypatch, prs=[pr(), pr(8)])
    body = api.github_test({"repo": REPO})
    assert body == {
        "ok": True,
        "login": "agent",
        "full_name": REPO,
        "default_branch": "main",
        "private": False,
        "open_pr_count": 2,
    }
    assert_no_token(body)


def test_test_endpoint_requires_a_token(api, monkeypatch):
    from fastapi import HTTPException

    install_client(api, monkeypatch)
    with pytest.raises(HTTPException) as excinfo:
        api.github_test({"repo": REPO})
    assert excinfo.value.status_code == 400
    assert "token" in excinfo.value.detail


def test_test_endpoint_validates_the_repo_format(api, monkeypatch):
    from fastapi import HTTPException

    api.sa_settings.save_settings({"github_token": TOKEN})
    with pytest.raises(HTTPException) as excinfo:
        api.github_test({"repo": "owner-only"})
    assert excinfo.value.status_code == 400
    assert "owner/name" in excinfo.value.detail


def test_test_endpoint_maps_auth_failure_to_400(api, monkeypatch):
    from fastapi import HTTPException

    api.sa_settings.save_settings({"github_token": TOKEN})
    install_client(api, monkeypatch, fail="GitHub 401: Bad credentials")
    with pytest.raises(HTTPException) as excinfo:
        api.github_test({"repo": REPO})
    assert excinfo.value.status_code == 400
    assert "Bad credentials" in excinfo.value.detail
    assert TOKEN not in excinfo.value.detail


# -- connect / disconnect ------------------------------------------------


def test_connect_persists_the_repo_and_token(api, monkeypatch):
    install_client(api, monkeypatch)
    watcher = install_watcher(api, monkeypatch)

    body = api.github_connect({"repo": REPO, "token": TOKEN})

    data = json.loads(api.sa_settings.settings_path().read_text(encoding="utf-8"))
    assert data["github_repo"] == REPO
    assert data["github_token"] == TOKEN
    assert data["github_agent_login"] == "agent"
    assert body["ok"] is True
    assert body["configured"] is True
    assert body["repo"] == REPO
    assert watcher.started == 1
    assert_no_token(body)


def test_connect_can_reuse_the_saved_token(api, monkeypatch):
    api.sa_settings.save_settings({"github_token": TOKEN})
    FakeClient = install_client(api, monkeypatch)
    install_watcher(api, monkeypatch)

    api.github_connect({"repo": REPO})

    assert FakeClient.instances[-1].token == TOKEN
    data = json.loads(api.sa_settings.settings_path().read_text(encoding="utf-8"))
    assert data["github_token"] == TOKEN


def test_connect_rejects_a_bad_repo_without_writing_settings(api, monkeypatch):
    from fastapi import HTTPException

    install_client(api, monkeypatch)
    with pytest.raises(HTTPException) as excinfo:
        api.github_connect({"repo": "nope", "token": TOKEN})
    assert excinfo.value.status_code == 400
    assert not api.sa_settings.settings_path().exists()


def test_connect_auth_failure_does_not_save_anything(api, monkeypatch):
    from fastapi import HTTPException

    install_client(api, monkeypatch, fail="GitHub 403: Resource not accessible")
    with pytest.raises(HTTPException) as excinfo:
        api.github_connect({"repo": REPO, "token": TOKEN})
    assert excinfo.value.status_code == 400
    assert not api.sa_settings.settings_path().exists()


def test_connect_resets_the_old_baseline(api, monkeypatch):
    install_client(api, monkeypatch)
    install_watcher(api, monkeypatch)
    store = api._chat_store()
    store.mark_seen(f"gh_baseline:{REPO}")
    store.mark_seen(f"gh_comment:{REPO}:issue:1")

    api.github_connect({"repo": REPO, "token": TOKEN})

    assert not store.is_seen(f"gh_baseline:{REPO}")
    assert not store.is_seen(f"gh_comment:{REPO}:issue:1")


def test_disconnect_clears_settings_and_stops_polling(api, monkeypatch):
    api.sa_settings.save_settings({"github_repo": REPO, "github_token": TOKEN})
    watcher = install_watcher(api, monkeypatch)

    body = api.github_disconnect()

    assert body == {"ok": True}
    settings = api.sa_settings.load_settings()
    assert settings["github_repo"] == ""
    assert settings["github_token"] == ""
    assert watcher.stopped == 1
    assert_no_token(body)


# -- poll / prs ----------------------------------------------------------


def test_poll_returns_the_watcher_summary(api, monkeypatch):
    watcher = install_watcher(
        api,
        monkeypatch,
        poll_result={"configured": True, "polled": 3, "created": ["S-0002"], "errors": []},
    )
    body = api.github_poll()
    assert body == {
        "configured": True,
        "polled": 3,
        "created": ["S-0002"],
        "errors": [],
    }
    assert watcher.polls == 1


def test_poll_reports_a_crash_as_json(api, monkeypatch):
    api.sa_settings.save_settings({"github_token": TOKEN})
    install_watcher(api, monkeypatch, error=f"boom {TOKEN}")
    body = api.github_poll()
    assert body["configured"] is False
    assert body["created"] == []
    assert body["errors"]
    assert_no_token(body)


def test_prs_lists_open_pull_requests(api, monkeypatch):
    install_watcher(api, monkeypatch)
    body = api.github_prs()
    assert body["configured"] is True
    assert [item["number"] for item in body["prs"]] == [7]


# -- work ----------------------------------------------------------------


def test_work_creates_a_session_and_launches_it(api, monkeypatch):
    api.sa_settings.save_settings({"github_repo": REPO, "github_token": TOKEN})
    install_client(api, monkeypatch, prs=[pr()])
    runner = FakeRunner(store=api.sa_chats.ChatStore(api._db_path()))
    monkeypatch.setattr(api, "_get_runner", lambda: runner)

    body = api.github_pr_work(7, {"request": "add a regression test"})

    assert body["ok"] is True
    assert body["launched"] is True
    session = body["session"]
    assert session["source"] == "github_pr"
    assert session["github_repo"] == REPO
    assert session["pr_number"] == 7
    assert session["status"] == "running"
    assert "add a regression test" in session["prompt"]
    assert "Add parser tests" in session["prompt"]
    assert runner.launches[0]["repo"] == ""
    assert_no_token(body)


def test_work_404s_for_an_unknown_pr(api, monkeypatch):
    from fastapi import HTTPException

    api.sa_settings.save_settings({"github_repo": REPO, "github_token": TOKEN})
    install_client(api, monkeypatch, prs=[])
    with pytest.raises(HTTPException) as excinfo:
        api.github_pr_work(99)
    assert excinfo.value.status_code == 404


def test_work_400s_when_github_is_not_connected(api, monkeypatch):
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as excinfo:
        api.github_pr_work(7)
    assert excinfo.value.status_code == 400
    assert "not connected" in excinfo.value.detail


def test_work_rejects_an_invalid_number(api):
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as excinfo:
        api.github_pr_work(0)
    assert excinfo.value.status_code == 400


def test_work_uses_the_pr_checkout_when_a_clone_is_configured(api, monkeypatch, tmp_path):
    clone = tmp_path / "clone"
    clone.mkdir()
    checkout = tmp_path / "workspaces" / "pr-7"
    api.sa_settings.save_settings(
        {"github_repo": REPO, "github_token": TOKEN, "repo_path": str(clone)}
    )
    install_client(api, monkeypatch, prs=[pr()])
    runner = FakeRunner()
    monkeypatch.setattr(api, "_get_runner", lambda: runner)
    monkeypatch.setattr(
        api.sa_github,
        "prepare_pr_checkout",
        lambda *args, **kwargs: {"path": str(checkout), "reused": False, "branch_ref": "x"},
    )

    body = api.github_pr_work(7)

    assert body["checkout"] == str(checkout)
    assert runner.launches[0]["repo"] == str(checkout)
    assert str(checkout) in runner.launches[0]["prompt"]


def test_work_survives_a_checkout_failure(api, monkeypatch, tmp_path):
    clone = tmp_path / "clone"
    clone.mkdir()
    api.sa_settings.save_settings(
        {"github_repo": REPO, "github_token": TOKEN, "repo_path": str(clone)}
    )
    install_client(api, monkeypatch, prs=[pr()])
    runner = FakeRunner()
    monkeypatch.setattr(api, "_get_runner", lambda: runner)

    def broken(*args, **kwargs):
        raise api.sa_github.GitHubError("could not fetch pull request #7: nope")

    monkeypatch.setattr(api.sa_github, "prepare_pr_checkout", broken)
    body = api.github_pr_work(7)

    assert body["ok"] is True
    assert body["checkout"] == ""
    assert "nope" in body["warning"]
    assert runner.launches[0]["repo"] == str(clone)


def test_work_reports_a_launch_failure(api, monkeypatch):
    api.sa_settings.save_settings({"github_repo": REPO, "github_token": TOKEN})
    install_client(api, monkeypatch, prs=[pr()])
    runner = FakeRunner(fail="hermes executable not found")
    monkeypatch.setattr(api, "_get_runner", lambda: runner)

    body = api.github_pr_work(7)

    assert body["ok"] is False
    assert body["session"]["status"] == "failed"
    assert "hermes" in body["error"]


# -- settings integration ------------------------------------------------


def test_runner_picks_up_plugin_settings(api, monkeypatch, tmp_path):
    import yaml

    repo = tmp_path / "repo"
    repo.mkdir()
    config = tmp_path / "hermes" / "config.yaml"
    config.write_text(
        yaml.safe_dump(
            {
                "plugins": {
                    "entries": {
                        "superagent": {
                            "settings": {
                                "repo_path": str(repo),
                                "default_model": "config-model",
                                "default_provider": "config-provider",
                                "max_workers": 3,
                                "worker_yolo": False,
                                "chat_max_turns": 7,
                            }
                        }
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    api.sa_settings.clear_cache()

    captured = {}

    class CapturingRunner:
        def __init__(self, store, **kwargs):
            captured.update(kwargs)
            captured["store"] = store

    monkeypatch.setattr(api.sa_runner, "Runner", CapturingRunner)
    api._RUNNERS.clear()
    api._get_runner()

    assert captured["repo"] == str(repo)
    assert captured["model"] == "config-model"
    assert captured["provider"] == "config-provider"
    assert captured["max_workers"] == 3
    assert captured["yolo"] is False
    assert captured["max_turns"] == 7


def test_models_endpoint_uses_plugin_settings(api, tmp_path):
    import yaml

    config = tmp_path / "hermes" / "config.yaml"
    config.write_text(
        yaml.safe_dump(
            {
                "plugins": {
                    "entries": {
                        "superagent": {
                            "settings": {
                                "default_model": "config-model",
                                "default_provider": "config-provider",
                                "model_presets": ["m-one", "m-two"],
                            }
                        }
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    api.sa_settings.clear_cache()
    body = api.list_models()
    assert body == {
        "default_model": "config-model",
        "default_provider": "config-provider",
        "models": ["m-one", "m-two"],
    }
