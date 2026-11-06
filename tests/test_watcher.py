"""Tests for the GitHub polling watcher (sa_watcher.py).

No network and no worker: a fake client serves canned PRs/comments and a fake
runner records launches. The store is the real SQLite ChatStore, because the
dedupe keys and session rows are what these tests are about.
"""

from __future__ import annotations

import json

import pytest

from sa_plugin import sa_chats, sa_github, sa_watcher

REPO = "owner/name"
TOKEN = "ghp_secret_watcher"


def pr(number=7, sha="a" * 40, title="Add parser tests", author="alice"):
    return {
        "number": number,
        "title": title,
        "body": "Closes #3",
        "author": author,
        "state": "open",
        "head_ref": "feature/parser",
        "head_sha": sha,
        "base_ref": "main",
        "html_url": f"https://github.com/{REPO}/pull/{number}",
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-02T00:00:00Z",
        "draft": False,
        "repo": REPO,
    }


def comment(cid=1, body="@agent please fix the parser", author="alice", kind="issue"):
    return {
        "id": cid,
        "kind": kind,
        "author": author,
        "body": body,
        "html_url": f"https://github.com/{REPO}/pull/7#comment-{cid}",
        "path": "",
        "line": None,
        "created_at": "2026-01-03T00:00:00Z",
    }


class FakeClient:
    def __init__(self, prs=None, comments=None, login="agent", fail="", fail_comments=""):
        self.prs = list(prs or [])
        self.comments = {key: list(value) for key, value in (comments or {}).items()}
        self.login = login
        self.fail = fail
        self.fail_comments = fail_comments
        self.calls = []

    def list_open_prs(self, limit=10):
        self.calls.append(("prs", limit))
        if self.fail:
            raise sa_github.GitHubError(self.fail, 401)
        return [dict(item) for item in self.prs]

    def list_comments(self, number):
        self.calls.append(("comments", number))
        if self.fail_comments:
            raise sa_github.GitHubError(self.fail_comments, 500)
        return [dict(item) for item in self.comments.get(number, [])]

    def me(self):
        return {"login": self.login, "id": 1}


class FakeRunner:
    def __init__(self):
        self.launches = []

    def launch_turn(
        self, session_id, prompt, *, resume_session_id=None, model=None, provider=None, repo=None
    ):
        self.launches.append(
            {
                "session_id": session_id,
                "prompt": prompt,
                "model": model,
                "provider": provider,
                "repo": repo,
            }
        )
        return {"session_id": session_id}


def make_settings(tmp_path, **overrides):
    settings = {
        "github_repo": REPO,
        "github_token": TOKEN,
        "github_agent_login": "agent",
        "github_allowed_users": ["alice"],
        "github_watch_mode": "observe",
        "github_watch_new_prs": False,
        "github_pr_limit": 10,
        "github_poll_seconds": 90,
        "repo_path": "",
        "default_model": "test-model",
        "default_provider": "test-provider",
        "workspace_root": str(tmp_path / "workspaces"),
    }
    settings.update(overrides)
    return settings


def make_watcher(chat, settings, client):
    runner = FakeRunner()
    watcher = sa_watcher.GitHubWatcher(
        chat, runner, lambda: settings, client_factory=lambda token, repo: client
    )
    return watcher, runner


def test_first_poll_is_a_baseline_and_creates_nothing(chat, tmp_path):
    settings = make_settings(tmp_path)
    client = FakeClient(prs=[pr()], comments={7: [comment()]})
    watcher, runner = make_watcher(chat, settings, client)

    summary = watcher.poll_once()

    assert summary == {"configured": True, "polled": 1, "created": [], "errors": []}
    assert chat.list_sessions()["sessions"] == []
    assert runner.launches == []
    assert chat.is_seen("gh_baseline:owner/name")
    assert chat.is_seen("gh_comment:owner/name:issue:1")
    # A second poll of the same old comment still creates nothing.
    assert watcher.poll_once()["created"] == []


def test_baseline_marks_pr_heads_seen(chat, tmp_path):
    settings = make_settings(tmp_path, github_watch_new_prs=True)
    client = FakeClient(prs=[pr(sha="a" * 40)], comments={7: []})
    watcher, _ = make_watcher(chat, settings, client)

    watcher.poll_once()
    assert chat.is_seen(f"gh_head:owner/name:7:{'a' * 40}")
    assert watcher.poll_once()["created"] == []


def test_a_new_trigger_comment_creates_an_observed_session(chat, tmp_path):
    settings = make_settings(tmp_path)
    client = FakeClient(prs=[pr()], comments={7: []})
    watcher, runner = make_watcher(chat, settings, client)
    watcher.poll_once()  # baseline

    client.comments[7].append(comment(cid=2, body="@agent please add a regression test"))
    summary = watcher.poll_once()

    assert len(summary["created"]) == 1
    session = chat.get_session(summary["created"][0])
    assert session["status"] == sa_chats.OBSERVED
    assert session["source"] == "github_pr"
    assert session["github_repo"] == REPO
    assert session["pr_number"] == 7
    assert session["pr_url"] == f"https://github.com/{REPO}/pull/7"
    assert session["model"] == "test-model"
    assert session["provider"] == "test-provider"
    assert "Add parser tests" in session["title"]
    assert "please add a regression test" in session["prompt"]
    assert "github" in [event["kind"] for event in session["events"]]
    assert runner.launches == []  # observe mode never launches


def test_a_comment_without_a_trigger_is_marked_seen_but_ignored(chat, tmp_path):
    settings = make_settings(tmp_path)
    client = FakeClient(prs=[pr()], comments={7: []})
    watcher, _ = make_watcher(chat, settings, client)
    watcher.poll_once()

    client.comments[7].append(comment(cid=3, body="thanks, looks good"))
    assert watcher.poll_once()["created"] == []
    assert chat.is_seen("gh_comment:owner/name:issue:3")


def test_allowed_users_gate_blocks_other_humans(chat, tmp_path):
    settings = make_settings(tmp_path)
    client = FakeClient(prs=[pr()], comments={7: []})
    watcher, _ = make_watcher(chat, settings, client)
    watcher.poll_once()

    client.comments[7].append(comment(cid=4, author="bob", body="@agent fix it"))
    assert watcher.poll_once()["created"] == []
    assert chat.is_seen("gh_comment:owner/name:issue:4")


def test_an_empty_allow_list_means_nobody(chat, tmp_path):
    settings = make_settings(tmp_path, github_allowed_users=[])
    client = FakeClient(prs=[pr()], comments={7: []})
    watcher, _ = make_watcher(chat, settings, client)
    watcher.poll_once()

    client.comments[7].append(comment(cid=5, body="@agent fix it"))
    assert watcher.poll_once()["created"] == []


def test_bot_and_self_comments_are_skipped(chat, tmp_path):
    settings = make_settings(tmp_path)
    client = FakeClient(prs=[pr()], comments={7: []})
    watcher, _ = make_watcher(chat, settings, client)
    watcher.poll_once()

    client.comments[7] += [
        comment(cid=6, author="dependabot[bot]", body="@agent bump it"),
        comment(cid=7, author="agent", body="@agent fix it"),
    ]
    assert watcher.poll_once()["created"] == []


def test_the_same_comment_never_creates_two_sessions(chat, tmp_path):
    settings = make_settings(tmp_path)
    client = FakeClient(prs=[pr()], comments={7: []})
    watcher, _ = make_watcher(chat, settings, client)
    watcher.poll_once()

    client.comments[7].append(comment(cid=8))
    first = watcher.poll_once()
    second = watcher.poll_once()
    assert len(first["created"]) == 1
    assert second["created"] == []
    assert len(chat.list_sessions()["sessions"]) == 1


def test_auto_mode_queues_and_launches_in_the_pr_checkout(chat, tmp_path, monkeypatch):
    checkout = tmp_path / "workspaces" / "pr-7"
    repo_path = tmp_path / "clone"
    repo_path.mkdir()
    settings = make_settings(tmp_path, github_watch_mode="auto", repo_path=str(repo_path))
    client = FakeClient(prs=[pr()], comments={7: []})
    watcher, runner = make_watcher(chat, settings, client)
    watcher.poll_once()

    calls = []

    def fake_checkout(repo, github_repo, number, workspace, token, git_runner=None):
        calls.append((repo, github_repo, number, workspace, token))
        return {"path": str(checkout), "reused": False, "branch_ref": "refs/superagent/pr-7"}

    monkeypatch.setattr(sa_watcher.sa_github, "prepare_pr_checkout", fake_checkout)
    client.comments[7].append(comment(cid=9))
    summary = watcher.poll_once()

    assert len(summary["created"]) == 1
    session = chat.get_session(summary["created"][0])
    assert session["status"] == sa_chats.QUEUED
    assert session["repo"] == str(checkout)
    assert calls == [(str(repo_path), REPO, 7, str(tmp_path / "workspaces"), TOKEN)]
    assert len(runner.launches) == 1
    assert runner.launches[0]["repo"] == str(checkout)
    assert str(checkout) in runner.launches[0]["prompt"]


def test_auto_mode_without_a_repository_fails_the_session(chat, tmp_path):
    settings = make_settings(tmp_path, github_watch_mode="auto", repo_path="")
    client = FakeClient(prs=[pr()], comments={7: []})
    watcher, runner = make_watcher(chat, settings, client)
    watcher.poll_once()

    client.comments[7].append(comment(cid=10))
    summary = watcher.poll_once()
    session = chat.get_session(summary["created"][0])
    assert session["status"] == sa_chats.FAILED
    assert "no local repository" in session["error"]
    assert runner.launches == []


def test_a_checkout_failure_still_launches_in_the_configured_clone(chat, tmp_path, monkeypatch):
    repo_path = tmp_path / "clone"
    repo_path.mkdir()
    settings = make_settings(tmp_path, github_watch_mode="auto", repo_path=str(repo_path))
    client = FakeClient(prs=[pr()], comments={7: []})
    watcher, runner = make_watcher(chat, settings, client)
    watcher.poll_once()

    def broken_checkout(*args, **kwargs):
        raise sa_github.GitHubError("could not fetch pull request #7: auth failed")

    monkeypatch.setattr(sa_watcher.sa_github, "prepare_pr_checkout", broken_checkout)
    client.comments[7].append(comment(cid=11))
    summary = watcher.poll_once()

    session = chat.get_session(summary["created"][0])
    assert session["status"] == sa_chats.QUEUED
    assert runner.launches[0]["repo"] == str(repo_path)
    details = " ".join(event["detail"] for event in session["events"])
    assert "checkout failed" in details


def test_a_new_pr_head_creates_one_review_session_per_sha(chat, tmp_path):
    settings = make_settings(tmp_path, github_watch_new_prs=True)
    client = FakeClient(prs=[pr(sha="a" * 40)], comments={7: []})
    watcher, _ = make_watcher(chat, settings, client)
    watcher.poll_once()  # baseline, head a

    client.prs[0] = pr(sha="b" * 40)
    first = watcher.poll_once()
    second = watcher.poll_once()
    client.prs[0] = pr(sha="c" * 40)
    third = watcher.poll_once()

    assert len(first["created"]) == 1
    assert second["created"] == []
    assert len(third["created"]) == 1
    sessions = chat.list_sessions()["sessions"]
    assert len(sessions) == 2
    assert all("Review this pull request" in row["prompt"] for row in sessions)
    for session_id in first["created"] + third["created"]:
        assert "github" in [event["kind"] for event in chat.get_session(session_id)["events"]]


def test_poll_errors_are_recorded(chat, tmp_path):
    settings = make_settings(tmp_path)
    client = FakeClient(prs=[pr()], fail="GitHub 401: Bad credentials")
    watcher, _ = make_watcher(chat, settings, client)

    summary = watcher.poll_once()
    assert summary["configured"] is True
    assert summary["polled"] == 0
    assert "Bad credentials" in summary["errors"][0]
    assert "Bad credentials" in watcher.last_error
    assert watcher.last_poll is not None
    assert chat.get_state(f"gh_last_poll:{REPO}")


def test_comment_fetch_errors_do_not_stop_the_other_prs(chat, tmp_path):
    settings = make_settings(tmp_path)
    client = FakeClient(prs=[pr(7), pr(8)], comments={7: [comment()]}, fail_comments="boom")
    watcher, _ = make_watcher(chat, settings, client)
    summary = watcher.poll_once()
    assert len(summary["errors"]) == 2
    assert all("PR #" in item for item in summary["errors"])


def test_a_failed_comment_fetch_keeps_the_baseline_open(chat, tmp_path):
    settings = make_settings(tmp_path)
    client = FakeClient(prs=[pr(7)], comments={7: [comment()]}, fail_comments="boom")
    watcher, _ = make_watcher(chat, settings, client)

    watcher.poll_once()
    assert not chat.is_seen("gh_baseline:owner/name")

    client.fail_comments = ""
    summary = watcher.poll_once()
    assert summary["created"] == []  # the old comment is still not work
    assert chat.is_seen("gh_baseline:owner/name")


def test_not_configured_returns_early(chat, tmp_path):
    settings = make_settings(tmp_path, github_repo="", github_token="")
    watcher = sa_watcher.GitHubWatcher(
        chat,
        FakeRunner(),
        lambda: settings,
        client_factory=lambda token, repo: pytest.fail("client must not be built"),
    )
    assert watcher.poll_once() == {
        "configured": False,
        "polled": 0,
        "created": [],
        "errors": [],
    }


def test_overlapping_polls_are_rejected(chat, tmp_path):
    settings = make_settings(tmp_path)
    watcher, _ = make_watcher(chat, settings, FakeClient(prs=[pr()]))
    acquired = watcher._poll_lock.acquire(blocking=False)
    assert acquired
    try:
        summary = watcher.poll_once()
    finally:
        watcher._poll_lock.release()
    assert summary["errors"] == ["a poll is already running"]


def test_status_is_json_safe_and_never_contains_the_token(chat, tmp_path):
    settings = make_settings(tmp_path)
    client = FakeClient(prs=[pr()], comments={7: []})
    watcher, _ = make_watcher(chat, settings, client)

    status = watcher.status()

    assert status["configured"] is True
    assert status["repo"] == REPO
    assert status["login"] == "agent"
    assert status["token_set"] is True
    assert len(status["open_prs"]) == 1
    assert status["watch_mode"] == "observe"
    assert status["watch_new_prs"] is False
    assert status["allowed_users"] == ["alice"]
    assert status["poll_seconds"] == 90
    assert status["watching"] is False
    assert status["sessions_created"] == 0
    assert "github_token" not in status
    assert TOKEN not in json.dumps(status)


def test_sessions_created_counts_work(chat, tmp_path):
    settings = make_settings(tmp_path)
    client = FakeClient(prs=[pr()], comments={7: []})
    watcher, _ = make_watcher(chat, settings, client)
    watcher.poll_once()
    client.comments[7].append(comment(cid=12))
    watcher.poll_once()
    assert watcher.sessions_created == 1
