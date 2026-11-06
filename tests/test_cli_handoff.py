"""CLI tests for `hermes superagent diff|comment`.

Offline like the other CLI tests: a throwaway HERMES_HOME, a real (temporary)
git repository for the diff, and a fake GitHub client for the comment.
"""

from __future__ import annotations

import json
import subprocess
import types

import pytest

REPO = "owner/name"
TOKEN = "ghp_cli_handoff_123456"


def _args(**kwargs):
    base = dict(
        superagent_command="comment",
        session_id="",
        body="",
        body_file="",
        json=False,
    )
    base.update(kwargs)
    return types.SimpleNamespace(**base)


class FakeClient:
    instances = []
    fail = ""

    def __init__(self, token, repo, timeout=20):
        self.token = token
        self.repo = repo
        FakeClient.instances.append(self)

    def post_comment(self, number, body):
        if FakeClient.fail:
            raise _GITHUB.GitHubError(FakeClient.fail, 403)
        return {
            "id": 42,
            "html_url": f"https://github.com/{self.repo}/pull/{number}#issuecomment-42",
            "created_at": "2026-01-01T00:00:00Z",
        }


_GITHUB = None


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


@pytest.fixture
def cli(sa, tmp_path, monkeypatch):
    global _GITHUB
    _GITHUB = sa.sa_github
    home = tmp_path / "hermes"
    home.mkdir()
    monkeypatch.setattr(sa.sa_settings, "_hermes_home", lambda: home)
    monkeypatch.setattr(sa.sa_settings, "config_path", lambda: home / "config.yaml")
    for name in sa.sa_settings.ENV_VARS.values():
        monkeypatch.delenv(name, raising=False)
    sa.sa_settings.clear_cache()

    db = tmp_path / "superagent.db"
    monkeypatch.setattr(sa.sa_cli, "_db_path", lambda: db)

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "config", "core.autocrlf", "false")
    (repo / "a.txt").write_text("one\ntwo\nthree\n", encoding="utf-8")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-q", "-m", "initial")

    FakeClient.instances = []
    FakeClient.fail = ""
    monkeypatch.setattr(sa.sa_cli.sa_github, "GitHubClient", FakeClient)
    yield types.SimpleNamespace(
        sa=sa, home=home, db=db, repo=repo, store=sa.sa_chats.ChatStore(db)
    )
    sa.sa_settings.clear_cache()


def session(cli, *, pr_number=7, with_repo=True, github_repo=REPO):
    row = cli.store.create_session(
        "Fix the parser crash",
        title="Fix the parser crash",
        repo=str(cli.repo) if with_repo else "",
        source="github_pr",
        github_repo=github_repo,
        pr_number=pr_number,
        pr_url=f"https://github.com/{REPO}/pull/{pr_number}" if pr_number else None,
        status="idle",
    )
    cli.store.add_message(row["id"], "user", "Fix the parser crash")
    cli.store.add_message(row["id"], "assistant", "Patched the tokenizer; added a test.")
    return row["id"]


def connect(cli, token=TOKEN):
    cli.sa.sa_settings.save_settings({"github_repo": REPO, "github_token": token})


# -- diff ------------------------------------------------------------------


def test_diff_prints_stat_files_and_patch(cli, capsys):
    session_id = session(cli)
    (cli.repo / "a.txt").write_text("one\nTWO\nthree\n", encoding="utf-8")

    rc = cli.sa.sa_cli.run(
        _args(superagent_command="diff", session_id=session_id), object()
    )
    out = capsys.readouterr().out

    assert rc == 0
    assert session_id in out
    assert "1 file changed" in out
    assert "modified" in out and "a.txt" in out
    assert "+TWO" in out


def test_diff_json_shape(cli, capsys):
    session_id = session(cli)
    (cli.repo / "new.txt").write_text("new\n", encoding="utf-8")

    rc = cli.sa.sa_cli.run(
        _args(superagent_command="diff", session_id=session_id, json=True), object()
    )
    data = json.loads(capsys.readouterr().out)

    assert rc == 0
    assert data["ok"] is True
    assert data["session_id"] == session_id
    assert data["diff"]["files"] == [{"status": "untracked", "path": "new.txt"}]


def test_diff_unknown_session_exits_1(cli, capsys):
    rc = cli.sa.sa_cli.run(
        _args(superagent_command="diff", session_id="S-9999"), object()
    )
    out = capsys.readouterr().out
    assert rc == 1
    assert "unknown chat session" in out


def test_diff_without_a_repository_exits_1(cli, capsys):
    session_id = session(cli, with_repo=False)

    rc = cli.sa.sa_cli.run(
        _args(superagent_command="diff", session_id=session_id), object()
    )
    out = capsys.readouterr().out

    assert rc == 1
    assert "no repository" in out


# -- comment ---------------------------------------------------------------


def test_comment_posts_the_generated_summary(cli, capsys):
    session_id = session(cli)
    connect(cli)
    (cli.repo / "a.txt").write_text("one\nTWO\nthree\n", encoding="utf-8")

    rc = cli.sa.sa_cli.run(
        _args(superagent_command="comment", session_id=session_id), object()
    )
    out = capsys.readouterr().out

    assert rc == 0
    assert "commented on owner/name#7" in out
    assert "https://github.com/owner/name/pull/7#issuecomment-42" in out
    assert FakeClient.instances[-1].token == TOKEN
    events = cli.store.get_events(session_id)
    assert any(event["kind"] == "commented" for event in events)


def test_comment_body_flag_is_used_verbatim(cli, capsys):
    session_id = session(cli)
    connect(cli)

    rc = cli.sa.sa_cli.run(
        _args(superagent_command="comment", session_id=session_id, body="Hand reviewed."),
        object(),
    )

    assert rc == 0
    assert capsys.readouterr().out.startswith("commented on owner/name#7")


def test_comment_body_file_is_read(cli, tmp_path, capsys):
    session_id = session(cli)
    connect(cli)
    note = tmp_path / "note.md"
    note.write_text("From a file.\n", encoding="utf-8")

    rc = cli.sa.sa_cli.run(
        _args(superagent_command="comment", session_id=session_id, body_file=str(note)),
        object(),
    )
    out = capsys.readouterr().out

    assert rc == 0
    assert FakeClient.instances  # a post happened


def test_comment_json_reports_the_url(cli, capsys):
    session_id = session(cli)
    connect(cli)

    rc = cli.sa.sa_cli.run(
        _args(superagent_command="comment", session_id=session_id, json=True), object()
    )
    data = json.loads(capsys.readouterr().out)

    assert rc == 0
    assert data["ok"] is True
    assert data["pr_number"] == 7
    assert data["url"].endswith("#issuecomment-42")


def test_comment_without_a_pull_request_exits_1(cli, capsys):
    session_id = session(cli, pr_number=None)
    connect(cli)

    rc = cli.sa.sa_cli.run(
        _args(superagent_command="comment", session_id=session_id), object()
    )
    out = capsys.readouterr().out

    assert rc == 1
    assert "no pull request" in out
    assert FakeClient.instances == []


def test_comment_without_a_token_exits_1(cli, capsys):
    session_id = session(cli)
    cli.sa.sa_settings.save_settings({"github_repo": REPO, "github_token": ""})

    rc = cli.sa.sa_cli.run(
        _args(superagent_command="comment", session_id=session_id), object()
    )
    out = capsys.readouterr().out

    assert rc == 1
    assert "token" in out
    assert FakeClient.instances == []


def test_comment_unknown_session_exits_1(cli, capsys):
    connect(cli)

    rc = cli.sa.sa_cli.run(
        _args(superagent_command="comment", session_id="S-9999"), object()
    )
    out = capsys.readouterr().out

    assert rc == 1
    assert "unknown chat session" in out


def test_comment_github_failure_exits_1_and_masks_the_token(cli, capsys):
    session_id = session(cli)
    connect(cli)
    FakeClient.fail = f"Bad credentials {TOKEN}"

    rc = cli.sa.sa_cli.run(
        _args(superagent_command="comment", session_id=session_id), object()
    )
    out = capsys.readouterr().out

    assert rc == 1
    assert TOKEN not in out
    assert "error" in out


def test_comment_body_and_body_file_together_is_usage_error(cli, tmp_path, capsys):
    session_id = session(cli)
    connect(cli)

    rc = cli.sa.sa_cli.run(
        _args(
            superagent_command="comment",
            session_id=session_id,
            body="one",
            body_file=str(tmp_path / "x.md"),
        ),
        object(),
    )
    out = capsys.readouterr().out

    assert rc == 2
    assert "not both" in out
    assert FakeClient.instances == []
