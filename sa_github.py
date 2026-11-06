"""A tiny GitHub REST client for the watcher (stdlib only, no `gh` CLI).

Only what the plugin needs: the authenticated login, one repository, its open
pull requests, their comments, and posting a comment back. Everything goes
through `GitHubClient._call`, which adds the headers once and turns an HTTP or
network failure into a `GitHubError` with a message safe to show a user (no
token, ever).

`prepare_pr_checkout` is the other half: it fetches `refs/pull/<n>/head` from
GitHub into the local clone and creates a detached `git worktree` for it, so a
worker can edit the PR without disturbing the user's checkout.
"""

from __future__ import annotations

import base64
import json
import re
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable, Optional

API_ROOT = "https://api.github.com"
USER_AGENT = "superagent-plugin"
API_VERSION = "2022-11-28"
DEFAULT_TIMEOUT = 20
GIT_TIMEOUT = 120
BODY_LIMIT = 4000
MAX_PROMPT_BODY = 1500
MAX_PROMPT_COMMENT = 2000

Handler = Callable[[list[str], str, int], tuple[int, str, str]]


class GitHubError(Exception):
    """A GitHub or git failure with the HTTP status when there is one."""

    def __init__(self, message: str, status: int = 0):
        super().__init__(str(message))
        self.status = int(status or 0)


# The module-level handle lets tests replace the network call without touching
# urllib globally.
_urlopen = urllib.request.urlopen


def _redact(text: Any, token: str = "") -> str:
    """Remove the token and any Authorization header from a message."""
    value = "" if text is None else str(text)
    if token:
        value = value.replace(token, "[redacted]")
    return re.sub(
        r"(?i)(authorization\s*:\s*(?:basic|bearer|token)\s+)\S+", r"\1[redacted]", value
    )


def _tail(text: Any, limit: int = 600) -> str:
    value = ("" if text is None else str(text)).strip()
    return value[-limit:] if len(value) > limit else value


class GitHubClient:
    """Minimal REST client bound to one repository."""

    def __init__(self, token: str, repo: str, timeout: int = DEFAULT_TIMEOUT):
        self.token = (token or "").strip()
        self.repo = (repo or "").strip()
        self.timeout = int(timeout or DEFAULT_TIMEOUT)

    # -- transport -------------------------------------------------------

    def _url(self, path: str) -> str:
        if path.startswith("http://") or path.startswith("https://"):
            return path
        return API_ROOT + "/" + path.lstrip("/")

    def _call(self, method: str, path: str, payload: Optional[dict] = None) -> Any:
        data = None
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": API_VERSION,
            "User-Agent": USER_AGENT,
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            self._url(path), data=data, headers=headers, method=method.upper()
        )
        try:
            with _urlopen(request, timeout=self.timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            message = ""
            try:
                body = exc.read().decode("utf-8", "replace")
                parsed = json.loads(body) if body.strip() else {}
                if isinstance(parsed, dict):
                    message = str(parsed.get("message") or "")
                if not message:
                    message = body.strip()[:300]
            except Exception:
                message = ""
            detail = _redact(message or exc.reason, self.token)
            raise GitHubError(f"GitHub {exc.code}: {detail}", exc.code) from None
        except urllib.error.URLError as exc:
            raise GitHubError(f"cannot reach GitHub: {_redact(exc.reason, self.token)}") from None
        except (OSError, ValueError) as exc:
            raise GitHubError(f"GitHub request failed: {_redact(exc, self.token)}") from None
        if not raw:
            return {}
        try:
            return json.loads(raw.decode("utf-8", "replace"))
        except ValueError:
            raise GitHubError("GitHub returned a response that is not JSON") from None

    def _repo_path(self, suffix: str) -> str:
        if not self.repo:
            raise GitHubError("no GitHub repository configured")
        base = f"repos/{self.repo}"
        return f"{base}/{suffix.lstrip('/')}" if suffix else base

    # -- identity and repository ----------------------------------------

    def me(self) -> dict[str, Any]:
        data = self._call("GET", "/user") or {}
        return {"login": str(data.get("login") or ""), "id": data.get("id")}

    def repo_info(self) -> dict[str, Any]:
        data = self._call("GET", self._repo_path("")) or {}
        return {
            "full_name": str(data.get("full_name") or self.repo),
            "default_branch": str(data.get("default_branch") or ""),
            "private": bool(data.get("private")),
            "open_issues": int(data.get("open_issues") or 0),
        }

    # -- pull requests ---------------------------------------------------

    def _normalize_pr(self, item: dict) -> dict[str, Any]:
        head = item.get("head") if isinstance(item.get("head"), dict) else {}
        base = item.get("base") if isinstance(item.get("base"), dict) else {}
        user = item.get("user") if isinstance(item.get("user"), dict) else {}
        return {
            "number": item.get("number"),
            "title": str(item.get("title") or ""),
            "body": str(item.get("body") or ""),
            "author": str(user.get("login") or ""),
            "state": str(item.get("state") or ""),
            "head_ref": str(head.get("ref") or ""),
            "head_sha": str(head.get("sha") or ""),
            "base_ref": str(base.get("ref") or ""),
            "html_url": str(item.get("html_url") or ""),
            "created_at": str(item.get("created_at") or ""),
            "updated_at": str(item.get("updated_at") or ""),
            "draft": bool(item.get("draft")),
            "repo": self.repo,
        }

    def list_open_prs(self, limit: int = 10) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit or 10), 100))
        query = urllib.parse.urlencode(
            {"state": "open", "sort": "updated", "direction": "desc", "per_page": limit}
        )
        data = self._call("GET", self._repo_path(f"pulls?{query}")) or []
        if not isinstance(data, list):
            return []
        return [self._normalize_pr(item) for item in data if isinstance(item, dict)]

    def get_pr(self, number: int) -> dict[str, Any]:
        data = self._call("GET", self._repo_path(f"pulls/{int(number)}")) or {}
        if not isinstance(data, dict) or not data:
            raise GitHubError(f"pull request #{number} was not found", 404)
        return self._normalize_pr(data)

    # -- comments --------------------------------------------------------

    def _normalize_comment(self, item: dict, kind: str) -> dict[str, Any]:
        user = item.get("user") if isinstance(item.get("user"), dict) else {}
        return {
            "id": item.get("id"),
            "kind": kind,
            "author": str(user.get("login") or ""),
            "body": str(item.get("body") or ""),
            "html_url": str(item.get("html_url") or ""),
            "path": item.get("path") or "",
            "line": item.get("line") or item.get("original_line"),
            "created_at": str(item.get("created_at") or ""),
        }

    def list_comments(self, number: int) -> list[dict[str, Any]]:
        """Issue comments and review comments, merged and oldest first."""
        number = int(number)
        issue = self._call("GET", self._repo_path(f"issues/{number}/comments?per_page=100")) or []
        review = self._call("GET", self._repo_path(f"pulls/{number}/comments?per_page=100")) or []
        items: list[dict[str, Any]] = []
        for kind, rows in (("issue", issue), ("review", review)):
            if not isinstance(rows, list):
                continue
            for row in rows:
                if isinstance(row, dict):
                    items.append(self._normalize_comment(row, kind))
        # ISO-8601 timestamps sort lexicographically; ids break ties (GitHub
        # allocates them monotonically) so two same-second comments keep order.
        items.sort(key=lambda item: (item["created_at"], item["id"] or 0))
        return items

    def post_comment(self, number: int, body: str) -> dict[str, Any]:
        payload = {"body": str(body or "")[:BODY_LIMIT]}
        data = self._call("POST", self._repo_path(f"issues/{int(number)}/comments"), payload) or {}
        return {
            "id": data.get("id"),
            "html_url": str(data.get("html_url") or ""),
            "created_at": str(data.get("created_at") or ""),
        }


# -- triggers ------------------------------------------------------------


def is_trigger(body: str, login: str = "") -> bool:
    """True when a comment asks Superagent to do something.

    Accepted forms: `@<login>`, `@superagent`, `/superagent`, or a line that
    starts with `superagent:`. Case-insensitive; an empty body never triggers.
    """
    text = str(body or "").strip()
    if not text:
        return False
    lowered = text.lower()
    login = (login or "").strip().lower()
    if login and f"@{login}" in lowered:
        return True
    if "@superagent" in lowered or "/superagent" in lowered:
        return True
    return any(line.strip().lower().startswith("superagent:") for line in lowered.splitlines())


def is_bot(login: str) -> bool:
    """GitHub Apps end in `[bot]`; the common service accounts are known."""
    name = (login or "").strip().lower()
    if not name:
        return False
    if name.endswith("[bot]"):
        return True
    return name in ("github-actions", "dependabot")


# -- checkout ------------------------------------------------------------


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


def prepare_pr_checkout(
    repo_path: str,
    github_repo: str,
    number: int,
    workspace_root: str,
    token: str,
    git_runner: Optional[Handler] = None,
) -> dict[str, Any]:
    """Fetch a PR's head into `repo_path` and check it out as a worktree.

    Returns ``{path, reused, branch_ref}``. The clone is only ever used as the
    object store; the work happens in `<workspace_root>/pr-<number>`, detached
    at the fetched ref, so the user's checkout and branch are untouched.
    """
    number = int(number)
    source = Path(str(repo_path or "")).expanduser()
    if not (source / ".git").exists():
        raise GitHubError(f"not a git repository: {repo_path}")

    workspace = Path(str(workspace_root or "")).expanduser()
    dest = workspace / f"pr-{number}"
    ref = f"refs/superagent/pr-{number}"
    if (dest / ".git").exists():
        return {"path": str(dest), "reused": True, "branch_ref": ref}
    if dest.exists():
        raise GitHubError(f"checkout path exists and is not a git checkout: {dest}")
    workspace.mkdir(parents=True, exist_ok=True)

    run = git_runner or _default_git_runner
    auth = base64.b64encode(f"x-access-token:{token or ''}".encode("utf-8")).decode("ascii")
    fetch_argv = [
        "git",
        "-C",
        str(source),
        "-c",
        f"http.extraHeader=Authorization: Basic {auth}",
        "fetch",
        "--no-tags",
        f"https://github.com/{github_repo}.git",
        f"+refs/pull/{number}/head:{ref}",
    ]
    try:
        code, _out, err = run(fetch_argv, str(source), GIT_TIMEOUT)
    except Exception as exc:
        detail = _tail(_redact(exc, token)) or exc.__class__.__name__
        raise GitHubError(f"could not fetch pull request #{number}: {detail}") from None
    if code != 0:
        detail = _tail(_redact(err, token)) or f"git exited with code {code}"
        raise GitHubError(f"could not fetch pull request #{number}: {detail}")

    worktree_argv = ["git", "-C", str(source), "worktree", "add", "--detach", str(dest), ref]
    try:
        code, _out, err = run(worktree_argv, str(source), GIT_TIMEOUT)
    except Exception as exc:
        detail = _tail(_redact(exc, token)) or exc.__class__.__name__
        raise GitHubError(f"could not create a checkout for pull request #{number}: {detail}") from None
    if code != 0:
        detail = _tail(_redact(err, token)) or f"git exited with code {code}"
        raise GitHubError(f"could not create a checkout for pull request #{number}: {detail}")

    return {"path": str(dest), "reused": False, "branch_ref": ref}


# -- prompt --------------------------------------------------------------


def build_pr_prompt(
    pr: dict[str, Any], comment: Optional[dict[str, Any]] = None, checkout: str = ""
) -> str:
    """The worker prompt for one PR: context, the ask, and the ground rules."""
    pr = pr or {}
    number = pr.get("number")
    title = str(pr.get("title") or "").strip()
    repo = str(pr.get("repo") or "").strip()
    author = str(pr.get("author") or "").strip()
    if not author:
        user = pr.get("user") if isinstance(pr.get("user"), dict) else {}
        author = str(user.get("login") or "").strip()
    base = str(pr.get("base_ref") or "").strip()
    head = str(pr.get("head_ref") or "").strip()
    if not base:
        base = str((pr.get("base") or {}).get("ref") if isinstance(pr.get("base"), dict) else "").strip()
    if not head:
        head = str((pr.get("head") or {}).get("ref") if isinstance(pr.get("head"), dict) else "").strip()
    base = base or "main"
    body = str(pr.get("body") or "").strip()

    lines = [f"Work on GitHub pull request #{number}" + (f" in {repo}." if repo else "."), ""]
    lines.append(f"PR #{number}: {title}".rstrip())
    if pr.get("html_url"):
        lines.append(f"URL: {pr['html_url']}")
    lines.append(f"Author: {author or 'unknown'}")
    lines.append(f"Branches: {head or '?'} -> {base or '?'}")
    if body:
        clipped = body if len(body) <= MAX_PROMPT_BODY else body[:MAX_PROMPT_BODY] + "…"
        lines += ["", "Description:", clipped]

    if comment:
        c_author = str(comment.get("author") or "someone").strip() or "someone"
        c_body = str(comment.get("body") or "").strip() or "(no text)"
        if len(c_body) > MAX_PROMPT_COMMENT:
            c_body = c_body[:MAX_PROMPT_COMMENT] + "…"
        lines += ["", f"Triggering comment from @{c_author}:", c_body]

    lines += ["", "Checkout:"]
    if checkout:
        lines += [
            f"An isolated git worktree for this PR is at: {checkout}",
            "Work only inside that directory; do not touch the original clone.",
        ]
    else:
        lines.append("No isolated checkout was prepared; work in the repository you were given.")

    lines += [
        "",
        "What to do:",
        f"- Inspect what this PR changes: `git diff origin/{base}...HEAD`"
        " (fetch the base branch first if that ref is missing).",
        "- Make the smallest fix that addresses the request; follow the repository's existing style.",
        "- Run the relevant tests or checks. If there are none, say what you checked instead.",
        "- Do NOT commit, push, merge, or open/update a pull request."
        " Leave changes in the working tree for a human.",
        "- If the request needs a design decision, ask one concrete question rather than guessing.",
        "- Finish with a summary of at most 200 words: what changed, files touched,"
        " checks run, and anything unresolved.",
    ]
    return "\n".join(lines)
