"""CLI tests for `hermes superagent github ...`.

Everything is offline: the GitHub client and the watcher are replaced with
fakes, settings are written to a throwaway HERMES_HOME, and the runner records
launches instead of spawning `hermes chat`.
"""

from __future__ import annotations

import argparse
import json
import types

import pytest

REPO = "owner/name"
TOKEN = "ghp_cli_secret_987654"


def _args(**kwargs):
    base = dict(
        superagent_command="github",
        action="status",
        number=None,
        repo="",
        token="",
        allowed=None,
        watch_mode="",
        request="",
        json=False,
    )
    base.update(kwargs)
    return types.SimpleNamespace(**base)


def pr(number=7, title="Add parser tests", author="alice"):
    return {
        "number": number,
        "title": title,
        "body": "Closes #3",
        "author": author,
        "state": "open",
        "head_ref": "feature/parser",
        "head_sha": "b" * 40,
        "base_ref": "main",
        "html_url": f"https://github.com/{REPO}/pull/{number}",
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-02T00:00:00Z",
        "draft": False,
        "repo": REPO,
    }


STATUS = {
    "configured": True,
    "repo": REPO,
    "login": "agent",
    "token_set": True,
    "open_prs": [pr()],
    "last_poll": None,
    "last_error": "",
    "watching": True,
    "watch_mode": "observe",
    "watch_new_prs": False,
    "allowed_users": ["alice"],
    "poll_seconds": 90,
    "sessions_created": 0,
}


class FakeRunner:
    def __init__(self, fail=""):
        self.fail = fail
        self.launched = []
        self.stopped = []

    def launch_turn(self, session_id, prompt, **kwargs):
        if self.fail:
            raise RuntimeError(self.fail)
        self.launched.append({"session_id": session_id, "prompt": prompt, **kwargs})
        return {"session_id": session_id}

    def stop(self, session_id):
        self.stopped.append(session_id)
        return True


class FakeClient:
    instances = []
    fail = ""
    prs = [pr()]

    def __init__(self, token, repo, timeout=20):
        self.token = token
        self.repo = repo
        FakeClient.instances.append(self)

    def me(self):
        if FakeClient.fail:
            raise _sa_github().GitHubError(FakeClient.fail, 401)
        return {"login": "agent", "id": 1}

    def repo_info(self):
        if FakeClient.fail:
            raise _sa_github().GitHubError(FakeClient.fail, 401)
        return {
            "full_name": self.repo,
            "default_branch": "main",
            "private": False,
            "open_issues": 3,
        }

    def list_open_prs(self, limit=10):
        return [dict(item) for item in FakeClient.prs]

    def get_pr(self, number):
        for item in FakeClient.prs:
            if item["number"] == number:
                return dict(item)
        raise _sa_github().GitHubError(f"pull request #{number} was not found", 404)


_PLUGIN = None


def _sa_github():
    return _PLUGIN.sa_github


@pytest.fixture
def cli(sa, tmp_path, monkeypatch):
    global _PLUGIN
    _PLUGIN = sa
    home = tmp_path / "hermes"
    home.mkdir()
    monkeypatch.setattr(sa.sa_settings, "_hermes_home", lambda: home)
    monkeypatch.setattr(sa.sa_settings, "config_path", lambda: home / "config.yaml")
    for name in sa.sa_settings.ENV_VARS.values():
        monkeypatch.delenv(name, raising=False)
    sa.sa_settings.clear_cache()

    db = tmp_path / "superagent.db"
    monkeypatch.setattr(sa.sa_cli, "_db_path", lambda: db)
    runner = FakeRunner()
    monkeypatch.setattr(sa.sa_cli, "_get_runner", lambda store: runner)
    FakeClient.instances = []
    FakeClient.fail = ""
    FakeClient.prs = [pr()]
    monkeypatch.setattr(sa.sa_cli.sa_github, "GitHubClient", FakeClient)
    yield types.SimpleNamespace(sa=sa, home=home, db=db, runner=runner)
    sa.sa_settings.clear_cache()


def install_watcher(sa, monkeypatch, status=None, poll=None):
    class FakeWatcher:
        def __init__(self, store, runner, settings_getter=None):
            self.store = store
            self.runner = runner
            self.settings_getter = settings_getter
            self.polls = 0

        def status(self):
            return dict(STATUS if status is None else status)

        def poll_once(self):
            self.polls += 1
            return dict(
                {"configured": True, "polled": 2, "created": ["S-0001"], "errors": []}
                if poll is None
                else poll
            )

    monkeypatch.setattr(sa.sa_cli.sa_watcher, "GitHubWatcher", FakeWatcher)
    return FakeWatcher


def connect_settings(sa, **overrides):
    patch = {"github_repo": REPO, "github_token": TOKEN}
    patch.update(overrides)
    sa.sa_settings.save_settings(patch)


# -- status / prs / poll -------------------------------------------------


def test_status_reports_the_watcher_state(cli, monkeypatch, capsys):
    install_watcher(cli.sa, monkeypatch)

    rc = cli.sa.sa_cli.run(_args(action="status"), object())

    out = capsys.readouterr().out
    assert rc == 0
    assert REPO in out
    assert "agent" in out
    assert "alice" in out
    assert "poll=90s" in out


def test_status_json_shape(cli, monkeypatch, capsys):
    install_watcher(cli.sa, monkeypatch)

    rc = cli.sa.sa_cli.run(_args(action="status", json=True), object())

    data = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert data["repo"] == REPO
    assert data["open_prs"][0]["number"] == 7
    assert data["allowed_users"] == ["alice"]
    assert TOKEN not in json.dumps(data)


def test_status_not_configured_points_at_connect(cli, monkeypatch, capsys):
    install_watcher(
        cli.sa,
        monkeypatch,
        status={"configured": False, "repo": "", "open_prs": [], "last_error": ""},
    )

    rc = cli.sa.sa_cli.run(_args(action="status"), object())

    out = capsys.readouterr().out
    assert rc == 0
    assert "not connected" in out
    assert "github connect" in out


def test_prs_lists_number_title_and_author(cli, monkeypatch, capsys):
    install_watcher(cli.sa, monkeypatch)

    rc = cli.sa.sa_cli.run(_args(action="prs"), object())

    out = capsys.readouterr().out
    assert rc == 0
    assert "#7" in out
    assert "Add parser tests" in out
    assert "alice" in out


def test_prs_reports_the_empty_state(cli, monkeypatch, capsys):
    install_watcher(
        cli.sa,
        monkeypatch,
        status={"configured": True, "repo": REPO, "open_prs": []},
    )

    rc = cli.sa.sa_cli.run(_args(action="prs"), object())

    out = capsys.readouterr().out
    assert rc == 0
    assert "Open pull requests (0)" in out
    assert "(none)" in out


def test_poll_reports_created_sessions(cli, monkeypatch, capsys):
    install_watcher(
        cli.sa, monkeypatch, poll={"configured": True, "polled": 3, "created": ["S-0002"], "errors": []}
    )

    rc = cli.sa.sa_cli.run(_args(action="poll"), object())

    out = capsys.readouterr().out
    assert rc == 0
    assert "Polled 3" in out
    assert "S-0002" in out


def test_unknown_action_is_a_usage_error(cli, capsys):
    rc = cli.sa.sa_cli.run(_args(action="frobnicate"), object())

    out = capsys.readouterr().out
    assert rc == 2
    assert "usage:" in out


def test_a_bad_watch_mode_is_a_usage_error(cli, capsys):
    rc = cli.sa.sa_cli.run(_args(action="connect", repo=REPO, token=TOKEN, watch_mode="always"), object())

    out = capsys.readouterr().out
    assert rc == 2
    assert "watch-mode" in out


# -- test / connect / disconnect ----------------------------------------


def test_test_validates_a_repo_and_never_echoes_the_token(cli, capsys):
    rc = cli.sa.sa_cli.run(_args(action="test", repo=REPO, token=TOKEN), object())

    out = capsys.readouterr().out
    assert rc == 0
    assert REPO in out
    assert "agent" in out
    assert "open PRs:  1" in out
    assert TOKEN not in out
    assert not cli.sa.sa_settings.settings_path().exists()


def test_test_without_a_repo_is_a_usage_error(cli, capsys):
    rc = cli.sa.sa_cli.run(_args(action="test", token=TOKEN), object())

    out = capsys.readouterr().out
    assert rc == 2
    assert "owner/name" in out


def test_test_surfaces_an_auth_failure_as_runtime_error(cli, capsys):
    FakeClient.fail = f"GitHub 401: Bad credentials for {TOKEN}"

    rc = cli.sa.sa_cli.run(_args(action="test", repo=REPO, token=TOKEN), object())

    out = capsys.readouterr().out
    assert rc == 1
    assert "Bad credentials" in out
    assert TOKEN not in out


def test_connect_saves_repo_token_login_and_form_settings(cli, capsys):
    rc = cli.sa.sa_cli.run(
        _args(
            action="connect",
            repo=REPO,
            token=TOKEN,
            allowed=" @alice, bob,ALICE ",
            watch_mode="auto",
        ),
        object(),
    )

    out = capsys.readouterr().out
    saved = json.loads(cli.sa.sa_settings.settings_path().read_text(encoding="utf-8"))
    assert rc == 0
    assert saved["github_repo"] == REPO
    assert saved["github_token"] == TOKEN
    assert saved["github_agent_login"] == "agent"
    assert saved["github_allowed_users"] == ["alice", "bob"]
    assert saved["github_watch_mode"] == "auto"
    assert "connected as agent" in out
    assert TOKEN not in out


def test_connect_rejects_a_malformed_repo(cli, capsys):
    rc = cli.sa.sa_cli.run(_args(action="connect", repo="owner-only", token=TOKEN), object())

    out = capsys.readouterr().out
    assert rc == 2
    assert "owner/name" in out
    assert not cli.sa.sa_settings.settings_path().exists()


def test_connect_reuses_a_saved_token(cli, capsys):
    connect_settings(cli.sa)

    rc = cli.sa.sa_cli.run(_args(action="connect", repo=REPO), object())

    out = capsys.readouterr().out
    assert rc == 0
    assert FakeClient.instances[-1].token == TOKEN
    assert TOKEN not in out


def test_disconnect_clears_the_repo_and_token(cli, capsys):
    connect_settings(cli.sa)

    rc = cli.sa.sa_cli.run(_args(action="disconnect"), object())

    out = capsys.readouterr().out
    saved = json.loads(cli.sa.sa_settings.settings_path().read_text(encoding="utf-8"))
    assert rc == 0
    assert saved["github_repo"] == ""
    assert saved["github_token"] == ""
    assert "disconnected" in out


# -- work ----------------------------------------------------------------


def test_work_creates_a_queued_github_pr_session_and_launches_blocking(cli, capsys):
    connect_settings(cli.sa, default_model="m1", default_provider="p1")

    rc = cli.sa.sa_cli.run(
        _args(action="work", number="7", request="add a regression test"), object()
    )

    out = capsys.readouterr().out
    assert rc == 0
    assert len(cli.runner.launched) == 1
    call = cli.runner.launched[0]
    assert call["block"] is True
    assert call["model"] == "m1" and call["provider"] == "p1"
    assert call["repo"] == ""
    assert "add a regression test" in call["prompt"]

    session = cli.sa.sa_chats.ChatStore(cli.db).get_session(call["session_id"], message_limit=0)
    assert session["source"] == "github_pr"
    assert session["github_repo"] == REPO
    assert session["pr_number"] == 7
    assert session["pr_url"].endswith("/pull/7")
    assert session["status"] == "queued"
    assert session["title"].startswith("PR #7")
    assert call["session_id"] in out
    assert f"hermes superagent show {call['session_id']}" in out


def test_work_uses_the_pr_checkout_when_a_clone_is_configured(cli, monkeypatch, tmp_path, capsys):
    clone = tmp_path / "clone"
    clone.mkdir()
    checkout = tmp_path / "workspaces" / "pr-7"
    connect_settings(cli.sa, repo_path=str(clone))
    monkeypatch.setattr(
        cli.sa.sa_cli.sa_github,
        "prepare_pr_checkout",
        lambda *args, **kwargs: {"path": str(checkout), "reused": False, "branch_ref": "x"},
    )

    rc = cli.sa.sa_cli.run(_args(action="work", number=7), object())

    out = capsys.readouterr().out
    assert rc == 0
    assert cli.runner.launched[0]["repo"] == str(checkout)
    assert str(checkout) in cli.runner.launched[0]["prompt"]
    assert str(checkout) in out


def test_work_survives_a_checkout_failure(cli, monkeypatch, tmp_path, capsys):
    clone = tmp_path / "clone"
    clone.mkdir()
    connect_settings(cli.sa, repo_path=str(clone))

    def broken(*args, **kwargs):
        raise cli.sa.sa_github.GitHubError("could not fetch pull request #7: nope")

    monkeypatch.setattr(cli.sa.sa_cli.sa_github, "prepare_pr_checkout", broken)

    rc = cli.sa.sa_cli.run(_args(action="work", number=7), object())

    out = capsys.readouterr().out
    assert rc == 0
    assert cli.runner.launched[0]["repo"] == str(clone)
    assert "nope" in out  # the warning is shown, the session still runs


def test_work_without_a_number_is_a_usage_error(cli, capsys):
    rc = cli.sa.sa_cli.run(_args(action="work"), object())

    out = capsys.readouterr().out
    assert rc == 2
    assert "usage:" in out


def test_work_requires_a_connected_repository(cli, capsys):
    rc = cli.sa.sa_cli.run(_args(action="work", number=7), object())

    out = capsys.readouterr().out
    assert rc == 1
    assert "not connected" in out


def test_work_reports_a_launch_failure(cli, capsys):
    connect_settings(cli.sa)
    cli.runner.fail = "hermes executable not found"

    rc = cli.sa.sa_cli.run(_args(action="work", number=7), object())

    out = capsys.readouterr().out
    assert rc == 1
    assert "hermes executable not found" in out
    sessions = cli.sa.sa_chats.ChatStore(cli.db).list_sessions(limit=10)["sessions"]
    assert sessions[0]["status"] == "failed"


def test_work_json_shape(cli, capsys):
    connect_settings(cli.sa)

    rc = cli.sa.sa_cli.run(_args(action="work", number="7", json=True), object())

    data = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert data["ok"] is True
    assert data["session"]["source"] == "github_pr"
    assert data["transcript"].endswith("show " + data["session"]["id"])


# -- parser --------------------------------------------------------------


def test_parser_registers_the_github_command_and_flags(sa):
    parser = argparse.ArgumentParser(prog="hermes superagent")
    sa.sa_cli.setup_cli(parser)

    ns = parser.parse_args(["github"])
    assert ns.superagent_command == "github" and ns.action == "status"

    ns = parser.parse_args(
        ["github", "connect", "--repo", "a/b", "--token", "t", "--allowed", "x,y", "--watch-mode", "auto"]
    )
    assert ns.action == "connect"
    assert ns.repo == "a/b" and ns.token == "t"
    assert ns.allowed == "x,y" and ns.watch_mode == "auto"

    ns = parser.parse_args(["github", "work", "7", "--request", "fix it", "--json"])
    assert ns.action == "work" and ns.number == "7"
    assert ns.request == "fix it" and ns.json is True

    ns = parser.parse_args(["--json", "github", "status"])
    assert ns.json is True and ns.action == "status"
