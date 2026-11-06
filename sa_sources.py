"""Sources: where work arrives from.

A source turns its own event stream into ``Inbound`` objects. The engine never
knows whether a request came from a chat message, a GitHub comment or a test.
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Optional


@dataclass(frozen=True)
class Inbound:
    source: str
    source_ref: str
    request: str
    requester: str
    explicit: bool
    repo: Optional[str] = None
    issue_number: Optional[int] = None


class SourceError(RuntimeError):
    pass


class LocalSource:
    """Requests typed by a human into Hermes itself."""

    name = "local"

    def ask(self, request: str, requester: str = "operator") -> Inbound:
        return Inbound(
            source=self.name,
            source_ref=f"local:{time.time_ns()}",
            request=request.strip(),
            requester=requester,
            explicit=True,
        )


class GitHubSource:
    """Issue and pull-request comments on one repository.

    Uses the REST API directly so it needs no ``gh`` install: a token from the
    plugin's secret setting (or GITHUB_TOKEN in the environment) is enough.
    """

    name = "github"

    def __init__(
        self,
        repo: str,
        token: str,
        agent_login: str,
        allowed_users: list[str] | None = None,
        api_base: str = "https://api.github.com",
        timeout: int = 20,
    ):
        self.repo = repo
        self.token = token
        self.agent_login = agent_login
        self.allowed_users = {u.lower() for u in (allowed_users or [])}
        self.api_base = api_base.rstrip("/")
        self.timeout = timeout

    # -- public ---------------------------------------------------------

    def poll(self, since: Optional[str] = None, limit: int = 30) -> tuple[list[Inbound], Optional[str]]:
        if not self.repo or "/" not in self.repo:
            raise SourceError("github.repo is not set (expected 'owner/name')")
        if not self.token:
            raise SourceError("no GitHub token configured")
        if not self.agent_login:
            raise SourceError("github.agent_login is not set")

        params: dict[str, Any] = {"per_page": max(1, min(limit, 100)), "sort": "created", "direction": "desc"}
        if since:
            params["since"] = since
        query = urllib.parse.urlencode(params)
        comments = self._request("GET", f"/repos/{self.repo}/issues/comments?{query}")
        if not isinstance(comments, list):
            raise SourceError("unexpected GitHub response")

        comments = list(reversed(comments))  # oldest first
        found: list[Inbound] = []
        for comment in comments:
            inbound = self._to_inbound(comment)
            if inbound:
                found.append(inbound)

        cursor = since
        stamps = [c.get("created_at") for c in comments if c.get("created_at")]
        if stamps:
            cursor = max(stamps)
        return found, cursor

    def reply(self, issue_number: int, body: str) -> dict:
        return self._request("POST", f"/repos/{self.repo}/issues/{issue_number}/comments", {"body": body})

    # -- internals ------------------------------------------------------

    def _to_inbound(self, comment: dict) -> Optional[Inbound]:
        user = comment.get("user") or {}
        login = str(user.get("login") or "")
        if not login or login.lower() == self.agent_login.lower():
            return None
        if str(user.get("type") or "") == "Bot":
            return None
        if self.allowed_users and login.lower() not in self.allowed_users:
            return None

        body = str(comment.get("body") or "")
        if not self._mentions_agent(body):
            return None

        issue_url = str(comment.get("issue_url") or "")
        issue_number = None
        if issue_url:
            tail = issue_url.rstrip("/").rsplit("/", 1)[-1]
            issue_number = int(tail) if tail.isdigit() else None

        return Inbound(
            source=self.name,
            source_ref=f"github:{comment.get('id')}",
            request=self._strip_mention(body),
            requester=login,
            explicit=True,
            repo=self.repo,
            issue_number=issue_number,
        )

    def _mentions_agent(self, body: str) -> bool:
        pattern = r"(?<![\w-])@" + re.escape(self.agent_login) + r"(?![\w-])"
        return re.search(pattern, body, re.IGNORECASE) is not None

    def _strip_mention(self, body: str) -> str:
        pattern = r"(?<![\w-])@" + re.escape(self.agent_login) + r"(?![\w-])\s*[:,]?\s*"
        return re.sub(pattern, "", body, flags=re.IGNORECASE).strip()

    def _request(self, method: str, path: str, payload: Optional[dict] = None) -> Any:
        url = self.api_base + path
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = urllib.request.Request(url, data=data, method=method)
        request.add_header("Accept", "application/vnd.github+json")
        request.add_header("Authorization", f"Bearer {self.token}")
        request.add_header("X-GitHub-Api-Version", "2022-11-28")
        request.add_header("User-Agent", "hermes-superagent")
        if data is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:300]
            raise SourceError(f"GitHub {method} {path} failed: {exc.code} {detail}") from exc
        except urllib.error.URLError as exc:
            raise SourceError(f"GitHub unreachable: {exc.reason}") from exc
        return json.loads(raw) if raw else {}
