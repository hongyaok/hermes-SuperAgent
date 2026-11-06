"""Tests for the handoff endpoints: GET /chats/{id}/diff, /comment-preview, POST /comment.

The diff handlers shell out to real git, so the fixture builds a genuine
repository; the GitHub client is faked, because nothing here may reach the
network.
"""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load_api():
    path = ROOT / "dashboard" / "plugin_api.py"
    spec = importlib.util.spec_from_file_location("superagent_plugin_api_handoff", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["superagent_plugin_api_handoff"] = module
    spec.loader.exec_module(module)
    return module


def _git(repo, *args):
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


class FakeGhClient:
    """Records posts; raises GitHubError when ``fail`` is set."""

    instances = []
    fail = ""

    def __init__(self, token, repo, timeout=20):
        self.token = token
        self.repo = repo
        FakeGhClient.instances.append(self)

    def post_comment(self, number, body):
        if FakeGhClient.fail:
            raise _GITHUB.GitHubError(FakeGhClient.fail, 403)
        return {
            "id": 99,
            "html_url": f"https://github.com/{self.repo}/pull/{number}#issuecomment-99",
            "created_at": "2026-01-01T00:00:00Z",
            "body": body,
        }


_GITHUB = None


@pytest.fixture
def handoff_api(tmp_path, monkeypatch):
    global _GITHUB
    module = load_api()
    _GITHUB = module.sa_github
    db = tmp_path / "superagent.db"
    store = module.sa_chats.ChatStore(db)
    monkeypatch.setattr(module, "_db_path", lambda: db)
    home = tmp_path / "hermes"
    home.mkdir()
    monkeypatch.setattr(module.sa_settings, "_hermes_home", lambda: home)
    monkeypatch.setattr(module.sa_settings, "config_path", lambda: home / "config.yaml")
    for name in module.sa_settings.ENV_VARS.values():
        monkeypatch.delenv(name, raising=False)
    module.sa_settings.clear_cache()

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "config", "core.autocrlf", "false")
    (repo / "a.txt").write_text("one\ntwo\nthree\n", encoding="utf-8")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-q", "-m", "initial")

    FakeGhClient.instances = []
    FakeGhClient.fail = ""
    monkeypatch.setattr(module.sa_github, "GitHubClient", FakeGhClient)
    yield types.SimpleNamespace(api=module, store=store, repo=repo, home=home)
    module.sa_settings.clear_cache()


def _pr_session(store, repo, pr_number=7, with_repo=True):
    session = store.create_session(
        "Fix the parser crash",
        title="Fix the parser crash",
        repo=str(repo) if with_repo else "",
        source="github_pr",
        github_repo="owner/name",
        pr_number=pr_number,
        pr_url="https://github.com/owner/name/pull/7",
        status="idle",
    )
    store.add_message(session["id"], "user", "Fix the parser crash")
    store.add_message(session["id"], "assistant", "Patched the tokenizer; added a test.")
    return session["id"]


def _connect(api, token="ghp_token_value"):
    api.sa_settings.save_settings({"github_repo": "owner/name", "github_token": token})


# -- diff ------------------------------------------------------------------


def test_diff_endpoint_returns_status_stat_and_patch(handoff_api):
    api, store, repo = handoff_api.api, handoff_api.store, handoff_api.repo
    session_id = _pr_session(store, repo)
    (repo / "a.txt").write_text("one\nTWO\nthree\n", encoding="utf-8")
    (repo / "new.txt").write_text("new file\n", encoding="utf-8")

    body = api.chat_diff(session_id)

    assert body["ok"] is True
    assert body["session_id"] == session_id
    assert {"status": "modified", "path": "a.txt"} in body["files"]
    assert {"status": "untracked", "path": "new.txt"} in body["files"]
    assert "1 file changed" in body["stat"]
    assert "+TWO" in body["patch"]
    assert body["truncated"] is False


def test_diff_endpoint_is_404_for_an_unknown_session(handoff_api):
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as excinfo:
        handoff_api.api.chat_diff("S-9999")
    assert excinfo.value.status_code == 404


def test_diff_endpoint_is_400_without_a_repository(handoff_api):
    from fastapi import HTTPException

    session_id = _pr_session(handoff_api.store, handoff_api.repo, with_repo=False)
    with pytest.raises(HTTPException) as excinfo:
        handoff_api.api.chat_diff(session_id)
    assert excinfo.value.status_code == 400
    assert "repository" in excinfo.value.detail


def test_diff_endpoint_is_400_when_the_repository_is_gone(handoff_api, tmp_path):
    from fastapi import HTTPException

    missing = tmp_path / "gone"
    missing.mkdir()
    session_id = _pr_session(handoff_api.store, missing)
    shutil.rmtree(missing)

    with pytest.raises(HTTPException) as excinfo:
        handoff_api.api.chat_diff(session_id)
    assert excinfo.value.status_code == 400


# -- preview ---------------------------------------------------------------


def test_comment_preview_needs_no_network_and_no_token(handoff_api):
    api, store, repo = handoff_api.api, handoff_api.store, handoff_api.repo
    session_id = _pr_session(store, repo)
    (repo / "a.txt").write_text("one\nTWO\nthree\n", encoding="utf-8")

    body = api.chat_comment_preview(session_id)

    assert body["ok"] is True
    assert body["pr_number"] == 7
    assert body["github_repo"] == "owner/name"
    assert body["length"] == len(body["body"])
    assert body["body"].startswith(f"Superagent finished {session_id}")
    assert "Patched the tokenizer; added a test." in body["body"]
    assert "Files changed: 1 (+1/-1)" in body["body"]
    assert FakeGhClient.instances == []


def test_comment_preview_is_400_without_a_pull_request(handoff_api):
    from fastapi import HTTPException

    session = handoff_api.store.create_session("plain chat", repo=str(handoff_api.repo))
    with pytest.raises(HTTPException) as excinfo:
        handoff_api.api.chat_comment_preview(session["id"])
    assert excinfo.value.status_code == 400


# -- comment ---------------------------------------------------------------


def test_comment_posts_the_default_body_and_records_the_event(handoff_api):
    api, store, repo = handoff_api.api, handoff_api.store, handoff_api.repo
    session_id = _pr_session(store, repo)
    _connect(api)
    (repo / "a.txt").write_text("one\nTWO\nthree\n", encoding="utf-8")

    body = api.chat_comment(session_id, {})

    assert body["ok"] is True
    assert body["url"].endswith("#issuecomment-99")
    assert body["pr_number"] == 7
    assert "Superagent finished" in body["body_preview"]
    assert FakeGhClient.instances[-1].repo == "owner/name"
    assert FakeGhClient.instances[-1].token == "ghp_token_value"
    events = store.get_events(session_id)
    assert any(event["kind"] == "commented" and body["url"] in event["detail"] for event in events)


def test_comment_uses_an_explicit_body_verbatim(handoff_api):
    api, store, repo = handoff_api.api, handoff_api.store, handoff_api.repo
    session_id = _pr_session(store, repo)
    _connect(api)

    body = api.chat_comment(session_id, {"body": "Reviewed by hand. Nice work."})

    assert body["ok"] is True
    assert body["body_preview"] == "Reviewed by hand. Nice work."


def test_comment_is_400_without_a_pull_request(handoff_api):
    from fastapi import HTTPException

    api = handoff_api.api
    session = handoff_api.store.create_session("plain", repo=str(handoff_api.repo))
    _connect(api)
    with pytest.raises(HTTPException) as excinfo:
        api.chat_comment(session["id"], {})
    assert excinfo.value.status_code == 400
    assert "pull request" in excinfo.value.detail


def test_comment_is_400_without_a_token(handoff_api):
    from fastapi import HTTPException

    api = handoff_api.api
    session_id = _pr_session(handoff_api.store, handoff_api.repo)
    api.sa_settings.save_settings({"github_repo": "owner/name", "github_token": ""})
    with pytest.raises(HTTPException) as excinfo:
        api.chat_comment(session_id, {})
    assert excinfo.value.status_code == 400
    assert "token" in excinfo.value.detail


def test_comment_is_400_when_the_repository_is_gone(handoff_api, tmp_path):
    from fastapi import HTTPException

    api = handoff_api.api
    missing = tmp_path / "gone"
    missing.mkdir()
    session_id = _pr_session(handoff_api.store, missing)
    _connect(api)
    shutil.rmtree(missing)

    with pytest.raises(HTTPException) as excinfo:
        api.chat_comment(session_id, {})
    assert excinfo.value.status_code == 400


def test_comment_is_502_and_redacts_when_github_fails(handoff_api):
    from fastapi import HTTPException

    api = handoff_api.api
    session_id = _pr_session(handoff_api.store, handoff_api.repo)
    _connect(api, token="ghp_handoff_secret_123")
    FakeGhClient.fail = "Bad credentials ghp_handoff_secret_123"

    with pytest.raises(HTTPException) as excinfo:
        api.chat_comment(session_id, {})
    assert excinfo.value.status_code == 502
    assert "ghp_handoff_secret_123" not in excinfo.value.detail


def test_comment_rejects_an_oversized_body(handoff_api):
    from fastapi import HTTPException

    api = handoff_api.api
    session_id = _pr_session(handoff_api.store, handoff_api.repo)
    _connect(api)

    with pytest.raises(HTTPException) as excinfo:
        api.chat_comment(session_id, {"body": "x" * (api.COMMENT_BODY_LIMIT + 1)})
    assert excinfo.value.status_code == 400
