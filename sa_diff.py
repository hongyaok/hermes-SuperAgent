"""What a finished session actually changed, and the comment that says so.

Two halves, both stdlib-only:

``collect_diff`` runs the three git commands a reviewer needs (status, stat,
patch) in the session's repository and returns one dict. Git is invoked as an
argv list with no shell, so a path with a space or a quote cannot turn into a
command; every failure (not a repository, git missing, a timeout) comes back as
``{ok: False, error: ...}`` instead of an exception, because this feeds an HTTP
handler and a CLI that must both degrade instead of 500.

``default_comment_body`` composes the summary a human can post back to the pull
request: what the session was asked to do, what the worker said last, and how
much code moved. It never posts anything itself.

The patch is capped (60 000 characters by default): a lockfile rewrite or a
generated bundle would otherwise travel through JSON, SQLite events and the
desktop panel. The cap is explicit — ``truncated`` and a trailing marker line —
so a reader knows the view is partial.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any, Callable, Optional

DEFAULT_PATCH_CHARS = 60000
GIT_TIMEOUT = 30
TRUNCATED_MARKER = "… diff truncated …"
STAGED_MARKER = "=== staged changes ==="
# A status listing of an untracked `node_modules` can be tens of thousands of
# lines; the panel only ever shows the head of it, so stop parsing eventually.
MAX_FILES = 5000

# stdin/stdout are decoded as UTF-8 with replacement: a repository may hold a
# Latin-1 source file, and `git diff` echoing it must not kill the request.
GitRunner = Callable[[list[str], str, int], "tuple[int, str, str]"]

_PRIORITY = ("R", "C", "D", "A", "M", "T")
_LABELS = {
    "A": "added",
    "C": "copied",
    "D": "deleted",
    "M": "modified",
    "R": "renamed",
    "T": "typechange",
}
# `git diff --stat` summary line, e.g. "3 files changed, 12 insertions(+), 1 deletion(-)"
_FILES_RE = re.compile(r"(\d+)\s+files?\s+changed")
_INSERTIONS_RE = re.compile(r"(\d+)\s+insertions?\(\+\)")
_DELETIONS_RE = re.compile(r"(\d+)\s+deletions?\(-\)")
_AUTH_RE = re.compile(r"(?i)(authorization\s*:\s*(?:basic|bearer|token)\s+)\S+")
_URL_AUTH_RE = re.compile(r"(?i)(https?://)[^/\s:@]+:[^/\s@]+@")


def _redact(text: Any) -> str:
    """Remove credentials from git stderr before it reaches a user."""
    value = "" if text is None else str(text)
    value = _AUTH_RE.sub(r"\1[redacted]", value)
    return _URL_AUTH_RE.sub(r"\1[redacted]@", value)


def _tail(text: Any, limit: int = 600) -> str:
    value = _redact(text).strip()
    return value[-limit:]


def _default_git_runner(argv: list[str], cwd: str, timeout: int) -> tuple[int, str, str]:
    proc = subprocess.run(
        argv,
        cwd=cwd or None,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )
    return proc.returncode, proc.stdout or "", proc.stderr or ""


def _status_label(code: str) -> str:
    """Map a porcelain XY code to a short human label.

    ``??`` is untracked; a rename outranks the modification that usually comes
    with it (``RM``), and an unmerged pair (``UU``/``AA``/``DD``) is a conflict.
    """
    if code == "??":
        return "untracked"
    if "U" in code or code in ("AA", "DD"):
        return "conflict"
    for letter in _PRIORITY:
        if letter in code:
            return _LABELS[letter]
    return "changed"


def parse_status(output: str, limit: int = MAX_FILES) -> list[dict[str, str]]:
    """Parse ``git status --porcelain=v1`` into ``[{status, path}]`` rows."""
    files: list[dict[str, str]] = []
    for line in str(output or "").splitlines():
        if len(line) < 3:
            continue
        if len(files) >= limit:
            break
        code = line[:2]
        path = line[3:].strip()
        if not path:
            continue
        # A rename/copy line is "old -> new"; the reviewer cares about the new path.
        if " -> " in path:
            path = path.rsplit(" -> ", 1)[1].strip()
        if len(path) > 1 and path.startswith('"') and path.endswith('"'):
            path = path[1:-1]
        files.append({"status": _status_label(code), "path": path})
    return files


def stat_totals(stat: str) -> dict[str, int]:
    """``{files, insertions, deletions}`` from a ``--stat`` summary, zeros if absent."""
    text = str(stat or "")
    files = sum(int(value) for value in _FILES_RE.findall(text))
    insertions = sum(int(value) for value in _INSERTIONS_RE.findall(text))
    deletions = sum(int(value) for value in _DELETIONS_RE.findall(text))
    return {"files": files, "insertions": insertions, "deletions": deletions}


def _run(
    run: GitRunner, argv: list[str], repo: str
) -> tuple[int, str, str]:
    return run(argv, repo, GIT_TIMEOUT)


def _failure(repo: str, message: str) -> dict[str, Any]:
    return {
        "ok": False,
        "repo": repo,
        "files": [],
        "stat": "",
        "patch": "",
        "truncated": False,
        "error": message or "could not read the diff",
    }


def collect_diff(
    repo_path: str,
    *,
    max_patch_chars: int = DEFAULT_PATCH_CHARS,
    git_runner: Optional[GitRunner] = None,
) -> dict[str, Any]:
    """Status, stat and patch for one repository; never raises.

    Returns ``{ok, repo, files, stat, patch, truncated, error}``. ``repo`` is
    the path as given (resolved when possible) so the caller can show it; an
    empty or missing path, a directory that is not a git checkout, a missing
    ``git`` binary and a timeout are all ``ok: False`` with a redacted error.
    """
    repo = str(repo_path or "").strip()
    try:
        if not repo:
            return _failure(repo, "no repository is set for this session")
        path = Path(repo).expanduser()
        if not path.is_dir():
            return _failure(repo, f"repository no longer exists: {repo}")
        repo = str(path)
        run = git_runner or _default_git_runner

        code, status_out, status_err = _run(
            run, ["git", "-C", repo, "status", "--porcelain=v1"], repo
        )
        if code != 0:
            detail = _tail(status_err) or f"git exited with code {code}"
            return _failure(repo, f"not a git repository: {detail}")

        _, stat_out, _ = _run(run, ["git", "-C", repo, "diff", "--stat"], repo)
        _, patch_out, _ = _run(run, ["git", "-C", repo, "diff", "--no-color"], repo)

        files = parse_status(status_out)
        staged = any(_is_staged(line[:2]) for line in status_out.splitlines() if len(line) >= 3)
        stat = stat_out.rstrip()
        patch = patch_out.rstrip()
        if staged:
            _, cached_stat, _ = _run(run, ["git", "-C", repo, "diff", "--cached", "--stat"], repo)
            _, cached_patch, _ = _run(
                run, ["git", "-C", repo, "diff", "--cached", "--no-color"], repo
            )
            cached_stat = cached_stat.rstrip()
            if cached_stat:
                stat = f"{stat}\n\nstaged:\n{cached_stat}".strip()
            cached_patch = cached_patch.rstrip()
            if cached_patch:
                patch = f"{patch}\n\n{STAGED_MARKER}\n{cached_patch}".strip()

        truncated = False
        try:
            cap = int(max_patch_chars)
        except (TypeError, ValueError):
            cap = DEFAULT_PATCH_CHARS
        if cap <= 0:
            cap = DEFAULT_PATCH_CHARS
        if len(patch) > cap:
            patch = patch[:cap].rstrip() + "\n" + TRUNCATED_MARKER + "\n"
            truncated = True

        return {
            "ok": True,
            "repo": repo,
            "files": files,
            "stat": stat,
            "patch": patch,
            "truncated": truncated,
            "error": "",
        }
    except subprocess.TimeoutExpired:
        return _failure(repo, f"git timed out after {GIT_TIMEOUT}s")
    except (OSError, ValueError) as exc:
        return _failure(repo, f"could not run git: {_tail(exc) or exc.__class__.__name__}")
    except Exception as exc:  # a bug here must not take an HTTP handler down
        return _failure(repo, f"could not read the diff: {_tail(exc) or exc.__class__.__name__}")


def _is_staged(code: str) -> bool:
    """True when the index column says something is staged.

    ``??`` is untracked (not staged) and a worktree-only change (`` M``) is not
    staged either, so only the two-letter forms with a non-space, non-``?``
    index column count.
    """
    return len(code) >= 1 and code[0] not in (" ", "?")


def _one_line(text: Any, limit: int = 120) -> str:
    value = " ".join(str(text or "").split())
    return value if len(value) <= limit else value[: max(1, limit - 1)] + "…"


def last_assistant_text(session: Any) -> str:
    """The newest non-empty assistant message in a session dict ('' if none)."""
    data = session if isinstance(session, dict) else {}
    for message in reversed(data.get("messages") or []):
        if not isinstance(message, dict):
            continue
        if str(message.get("role") or "").lower() != "assistant":
            continue
        content = str(message.get("content") or "").strip()
        if content:
            return content
    return ""


def _session_title(session: dict) -> str:
    title = str(session.get("title") or "").strip()
    if title:
        return _one_line(title, 90)
    for line in str(session.get("prompt") or "").splitlines():
        text = " ".join(line.split())
        if text:
            return _one_line(text, 90)
    return ""


def _files_line(diff: Optional[dict]) -> str:
    if not diff or not diff.get("ok"):
        return ""
    files = diff.get("files") or []
    if not files:
        return ""
    totals = stat_totals(str(diff.get("stat") or ""))
    line = f"Files changed: {len(files)}"
    if totals["insertions"] or totals["deletions"]:
        line += f" (+{totals['insertions']}/-{totals['deletions']})"
    return line


def default_comment_body(
    session: Any, diff: Optional[dict] = None, limit: int = 4000
) -> str:
    """The PR comment a human can review before posting.

    ``Superagent finished S-0001 — <title>`` / the last assistant message / the
    diff totals / a provenance footer. Missing pieces are omitted rather than
    rendered as blanks; the whole body is clipped to ``limit`` characters,
    shortening the assistant summary first so the id, totals and footer
    survive.
    """
    data = session if isinstance(session, dict) else {}
    session_id = str(data.get("id") or "").strip() or "session"
    title = _session_title(data)
    head = f"Superagent finished {session_id}" + (f" — {title}" if title else "")
    footer = "_Generated by Superagent._"
    summary = last_assistant_text(data)
    files_line = _files_line(diff)

    try:
        cap = max(200, int(limit))
    except (TypeError, ValueError):
        cap = 4000

    def build(text: str) -> str:
        parts = [head]
        if text:
            parts += ["", text]
        if files_line:
            parts += ["", files_line]
        parts += ["", footer]
        return "\n".join(parts)

    body = build(summary)
    if len(body) <= cap:
        return body

    # Fixed overhead is everything except the assistant summary.
    overhead = len(build(""))
    room = cap - overhead - 8  # room for the blank line + the ellipsis
    if room > 80:
        clipped = summary[:room].rstrip() + "…"
        body = build(clipped)
        if len(body) <= cap:
            return body
    return body[: cap - 1].rstrip() + "…"
