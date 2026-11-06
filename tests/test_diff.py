"""Tests for sa_diff: real git in a throwaway repository, plus body composition.

``collect_diff`` shells out to git, so the fixture here builds a genuine
repository (init, commit, edit) instead of mocking the commands — the parsing
and the cap are only worth trusting against real porcelain/stat/patch output.
"""

from __future__ import annotations

import subprocess

import pytest


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
def repo(tmp_path):
    """A one-commit repository with `a.txt` tracked."""
    path = tmp_path / "repo"
    path.mkdir()
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "test@example.com")
    _git(path, "config", "user.name", "Test")
    _git(path, "config", "core.autocrlf", "false")
    (path / "a.txt").write_text("one\ntwo\nthree\n", encoding="utf-8")
    _git(path, "add", "a.txt")
    _git(path, "commit", "-q", "-m", "initial")
    return path


@pytest.fixture
def session():
    return {
        "id": "S-0007",
        "title": "Fix the parser crash",
        "prompt": "Fix the parser crash",
        "messages": [
            {"role": "user", "content": "Fix the parser crash"},
            {"role": "assistant", "content": "Patched the tokenizer and added a test."},
        ],
    }


# -- collect_diff ---------------------------------------------------------


def test_clean_repository_reports_no_changes(sa, repo):
    diff = sa.sa_diff.collect_diff(str(repo))
    assert diff["ok"] is True
    assert diff["files"] == []
    assert diff["stat"] == ""
    assert diff["patch"] == ""
    assert diff["truncated"] is False
    assert diff["error"] == ""


def test_modified_file_appears_in_status_stat_and_patch(sa, repo):
    (repo / "a.txt").write_text("one\nTWO\nthree\n", encoding="utf-8")

    diff = sa.sa_diff.collect_diff(str(repo))

    assert diff["ok"] is True
    assert diff["files"] == [{"status": "modified", "path": "a.txt"}]
    assert "1 file changed" in diff["stat"]
    assert "-two" in diff["patch"] and "+TWO" in diff["patch"]
    assert "a.txt" in diff["patch"]


def test_untracked_file_is_labelled_and_listed(sa, repo):
    (repo / "new.txt").write_text("brand new\n", encoding="utf-8")

    diff = sa.sa_diff.collect_diff(str(repo))

    assert diff["ok"] is True
    assert diff["files"] == [{"status": "untracked", "path": "new.txt"}]
    # Untracked content is not in `git diff`; the file list is how it shows up.
    assert diff["patch"] == ""


def test_staged_changes_get_their_own_section(sa, repo):
    (repo / "a.txt").write_text("one\nstaged\nthree\n", encoding="utf-8")
    _git(repo, "add", "a.txt")

    diff = sa.sa_diff.collect_diff(str(repo))

    assert diff["ok"] is True
    assert diff["files"] == [{"status": "modified", "path": "a.txt"}]
    assert sa.sa_diff.STAGED_MARKER in diff["patch"]
    assert "+staged" in diff["patch"]
    # The stat keeps both halves: the unstaged summary is empty, staged is not.
    assert "staged:" in diff["stat"]
    assert "1 file changed" in diff["stat"]


def test_patch_is_capped_with_a_marker(sa, repo):
    (repo / "a.txt").write_text("x\n" * 400, encoding="utf-8")

    diff = sa.sa_diff.collect_diff(str(repo), max_patch_chars=120)

    assert diff["ok"] is True
    assert diff["truncated"] is True
    assert diff["patch"].rstrip().endswith(sa.sa_diff.TRUNCATED_MARKER)
    assert len(diff["patch"]) < 300


def test_not_a_repository_is_an_error_not_an_exception(sa, tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()

    diff = sa.sa_diff.collect_diff(str(plain))

    assert diff["ok"] is False
    assert diff["files"] == [] and diff["patch"] == ""
    assert "git" in diff["error"].lower()


def test_missing_and_empty_paths_are_errors(sa, tmp_path):
    missing = sa.sa_diff.collect_diff(str(tmp_path / "nope"))
    assert missing["ok"] is False
    assert "does not exist" in missing["error"] or "no longer exists" in missing["error"]

    empty = sa.sa_diff.collect_diff("")
    assert empty["ok"] is False
    assert empty["error"]


def test_git_failure_is_reported_without_the_stderr_secret(sa, tmp_path):
    def failing_runner(argv, cwd, timeout):
        return 128, "", "fatal: Authorization: Bearer ghp_secret_value"

    diff = sa.sa_diff.collect_diff(str(tmp_path), git_runner=failing_runner)

    assert diff["ok"] is False
    assert "ghp_secret_value" not in diff["error"]
    assert "[redacted]" in diff["error"]


def test_git_runner_receives_argv_without_a_shell(sa, tmp_path):
    calls = []

    def runner(argv, cwd, timeout):
        calls.append(list(argv))
        return 0, "", ""

    diff = sa.sa_diff.collect_diff(str(tmp_path), git_runner=runner)

    assert diff["ok"] is True
    assert calls[0][:4] == ["git", "-C", str(tmp_path), "status"]
    assert all(isinstance(argv, list) for argv in calls)


def test_parse_status_handles_renames_and_conflicts(sa):
    rows = sa.sa_diff.parse_status(
        "R  old/name.py -> new/name.py\nUU both.py\nA  added.py\n M changed.py\n?? loose.py\n"
    )
    assert rows == [
        {"status": "renamed", "path": "new/name.py"},
        {"status": "conflict", "path": "both.py"},
        {"status": "added", "path": "added.py"},
        {"status": "modified", "path": "changed.py"},
        {"status": "untracked", "path": "loose.py"},
    ]


def test_stat_totals_reads_git_stats(sa):
    totals = sa.sa_diff.stat_totals(
        " a.py | 4 ++--\n 2 files changed, 12 insertions(+), 3 deletions(-)\n"
    )
    assert totals == {"files": 2, "insertions": 12, "deletions": 3}
    assert sa.sa_diff.stat_totals("") == {"files": 0, "insertions": 0, "deletions": 0}


# -- default_comment_body -------------------------------------------------


def test_comment_body_has_id_title_summary_totals_and_footer(sa, repo, session):
    (repo / "a.txt").write_text("one\nTWO\nthree\n", encoding="utf-8")
    diff = sa.sa_diff.collect_diff(str(repo))

    body = sa.sa_diff.default_comment_body(session, diff)

    lines = body.splitlines()
    assert lines[0] == "Superagent finished S-0007 — Fix the parser crash"
    assert "Patched the tokenizer and added a test." in body
    assert "Files changed: 1 (+1/-1)" in body
    assert body.rstrip().endswith("_Generated by Superagent._")


def test_comment_body_omits_missing_parts(sa):
    body = sa.sa_diff.default_comment_body({"id": "S-0001"})
    assert body.splitlines()[0] == "Superagent finished S-0001"
    assert "Files changed" not in body
    assert "_Generated by Superagent._" in body


def test_comment_body_uses_the_last_assistant_message(sa, session):
    session["messages"].append({"role": "assistant", "content": "Actually also fixed the lexer."})
    body = sa.sa_diff.default_comment_body(session)
    assert "Actually also fixed the lexer." in body
    assert "Patched the tokenizer" not in body


def test_comment_body_is_clipped_to_the_limit(sa, session):
    session["messages"][-1]["content"] = "long line " * 800
    body = sa.sa_diff.default_comment_body(session, limit=500)
    assert len(body) <= 500
    assert body.rstrip().endswith("_Generated by Superagent._")


def test_comment_body_counts_untracked_files_without_totals(sa, repo, session):
    (repo / "new.txt").write_text("brand new\n", encoding="utf-8")
    diff = sa.sa_diff.collect_diff(str(repo))

    body = sa.sa_diff.default_comment_body(session, diff)

    assert "Files changed: 1" in body
    assert "(+" not in body
