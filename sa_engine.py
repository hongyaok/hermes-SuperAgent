"""The router: poll sources, judge requests, keep tickets, drive workers."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

try:  # normal: imported as part of the plugin package
    from . import sa_assessor, sa_executor, sa_sources
    from .sa_store import Store
except ImportError:  # top-level import (root __init__ loaded as `__init__` by pytest)
    import sa_assessor  # type: ignore[no-redef]
    import sa_executor  # type: ignore[no-redef]
    import sa_sources  # type: ignore[no-redef]
    from sa_store import Store  # type: ignore[no-redef]

# Ticket states. `review` is the normal end of a worker: a human still has to
# look at the diff before anything is merged.
OBSERVED = "observed"
QUEUED = "queued"
RUNNING = "running"
REVIEW = "review"
BLOCKED = "blocked"
FAILED = "failed"
DONE = "done"
IGNORED = "ignored"

NEEDS_ATTENTION = (OBSERVED, BLOCKED, REVIEW, FAILED)

MODE_OFF = "off"
MODE_OBSERVE = "observe"
MODE_AUTO = "auto"
MODES = (MODE_OFF, MODE_OBSERVE, MODE_AUTO)


@dataclass(frozen=True)
class Settings:
    mode: str = MODE_OBSERVE
    repo_path: str = ""
    github_repo: str = ""
    github_agent_login: str = ""
    github_allowed_users: tuple[str, ...] = ()
    github_token: str = ""
    max_workers: int = 2
    worker_toolsets: tuple[str, ...] = ("file", "terminal")
    allow_proactive: bool = False
    max_runtime_seconds: int = 1800
    poll_limit: int = 30

    @classmethod
    def from_ctx(cls, ctx: Any) -> "Settings":
        def get(key: str, default: Any) -> Any:
            try:
                return ctx.get_config(key, default=default)
            except Exception:
                return default

        mode = str(get("mode", MODE_OBSERVE)).lower()
        if mode not in MODES:
            mode = MODE_OBSERVE

        def as_list(value: Any) -> tuple[str, ...]:
            if isinstance(value, str):
                return tuple(part.strip() for part in value.split(",") if part.strip())
            if isinstance(value, (list, tuple)):
                return tuple(str(part).strip() for part in value if str(part).strip())
            return ()

        return cls(
            mode=mode,
            repo_path=str(get("repo_path", "") or ""),
            github_repo=str(get("github_repo", "") or ""),
            github_agent_login=str(get("github_agent_login", "") or ""),
            github_allowed_users=as_list(get("github_allowed_users", [])),
            github_token=str(get("github_token", "") or ""),
            max_workers=max(1, int(get("max_workers", 2) or 2)),
            worker_toolsets=as_list(get("worker_toolsets", ["file", "terminal"])) or ("file", "terminal"),
            allow_proactive=bool(get("allow_proactive", False)),
            max_runtime_seconds=max(60, int(get("max_runtime_seconds", 1800) or 1800)),
            poll_limit=max(1, min(100, int(get("poll_limit", 30) or 30))),
        )


@dataclass
class TickResult:
    mode: str
    polled: int = 0
    created: list[str] = field(default_factory=list)
    launched: list[str] = field(default_factory=list)
    finished: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    waiting: int = 0

    def as_dict(self) -> dict:
        return {
            "mode": self.mode,
            "polled": self.polled,
            "created": self.created,
            "launched": self.launched,
            "finished": self.finished,
            "warnings": self.warnings,
            "waiting": self.waiting,
        }


class Engine:
    def __init__(
        self,
        store: Store,
        settings: Settings,
        executor: Optional[Any] = None,
        github: Optional[sa_sources.GitHubSource] = None,
    ):
        self.store = store
        self.settings = settings
        self.executor = executor
        self.github = github

    # -- public API -----------------------------------------------------

    def ask(self, request: str, requester: str = "operator") -> dict:
        """Record a request typed directly into Hermes."""
        if self.settings.mode == MODE_OFF:
            raise RuntimeError("Superagent is off. Set mode: observe or auto in its settings.")
        inbound = sa_sources.LocalSource().ask(request, requester=requester)
        decision = sa_assessor.assess(inbound.request, explicit=True, allow_proactive=False)
        return self._record(inbound, decision)

    def tick(self, wait: bool = False, launch: bool = True) -> TickResult:
        result = TickResult(mode=self.settings.mode)
        if self.settings.mode == MODE_OFF:
            result.warnings.append("Superagent is off; nothing polled.")
            return result

        for inbound in self._poll(result):
            if self.store.is_seen(inbound.source_ref):
                continue
            self.store.mark_seen(inbound.source_ref)
            decision = sa_assessor.assess(
                inbound.request,
                explicit=inbound.explicit,
                allow_proactive=self.settings.allow_proactive,
            )
            ticket = self._record(inbound, decision)
            result.created.append(ticket["id"])
        result.polled += len(result.created)

        result.launched = self._launch_queued() if launch else []
        if not launch:
            result.warnings.append("workers are launched only from a Hermes agent turn (chat or cron)")
        self._reap(result)
        if wait:
            self._wait(result, timeout_seconds=min(self.settings.max_runtime_seconds, 600))
        result.waiting = len(self.store.list(status=RUNNING, limit=100))
        return result

    def promote(self, ticket_id: str, wait: bool = False) -> dict:
        """A human approves an observed/blocked ticket; run it now."""
        if self.settings.mode == MODE_OFF:
            raise RuntimeError("Superagent is off. Set mode: observe or auto in its settings.")
        ticket = self.store.get(ticket_id)
        if not ticket:
            raise KeyError(f"unknown ticket {ticket_id}")
        if ticket["status"] not in (OBSERVED, BLOCKED, FAILED, QUEUED):
            raise RuntimeError(
                f"ticket {ticket_id} is {ticket['status']}; only observed, blocked, failed or queued tickets can be promoted"
            )

        self.store.update(ticket_id, status=QUEUED, mode="manual", error=None)
        self.store.add_event(ticket_id, "promoted", "a human approved the work")
        launched = self._launch_ticket(ticket_id)
        if wait and launched:
            self._wait(TickResult(mode=self.settings.mode), timeout_seconds=300)
        return self.store.get(ticket_id) | {"launched": launched}

    def status(self, ticket_id: Optional[str] = None) -> dict:
        if ticket_id:
            ticket = self.store.get(ticket_id)
            if not ticket:
                raise KeyError(f"unknown ticket {ticket_id}")
            ticket["events"] = self.store.events(ticket_id)
            return ticket
        counts = self.store.count_by_status()
        tickets = self.store.list(limit=30)
        attention = [t for t in self.store.list(limit=100) if t["status"] in NEEDS_ATTENTION][:10]
        return {
            "mode": self.settings.mode,
            "counts": counts,
            "attention": [self._ticket_brief(t) for t in attention],
            "tickets": [self._ticket_brief(t) for t in tickets],
            "running": counts.get(RUNNING, 0),
            "queued": counts.get(QUEUED, 0),
        }

    def doctor(self) -> dict:
        repo = Path(self.settings.repo_path) if self.settings.repo_path else None
        checks = {
            "mode": self.settings.mode,
            "repo_path": str(repo) if repo else "",
            "repo_exists": bool(repo and repo.is_dir()),
            "github_repo": self.settings.github_repo,
            "github_configured": bool(
                self.settings.github_repo
                and self.settings.github_agent_login
                and (self.settings.github_token or (self.github and self.github.token))
            ),
            "github_allowed_users": list(self.settings.github_allowed_users),
            "max_workers": self.settings.max_workers,
            "worker_toolsets": list(self.settings.worker_toolsets),
        }
        available, reason = (self.executor.available() if self.executor else (False, "no executor"))
        checks["workers_available"] = available
        checks["workers_reason"] = reason
        return checks

    # -- intake ---------------------------------------------------------

    def _poll(self, result: TickResult) -> list[sa_sources.Inbound]:
        if not self.github:
            return []
        since = self.store.get_state("github_since")
        try:
            inbound, cursor = self.github.poll(since=since, limit=self.settings.poll_limit)
        except sa_sources.SourceError as exc:
            result.warnings.append(str(exc))
            return []
        if cursor and cursor != since:
            self.store.set_state("github_since", cursor)
        return inbound

    def _record(self, inbound: sa_sources.Inbound, decision: sa_assessor.Decision) -> dict:
        if decision.verdict == sa_assessor.IGNORE:
            status = IGNORED
        elif decision.verdict == sa_assessor.NEEDS_HUMAN:
            status = BLOCKED
        elif self.settings.mode == MODE_AUTO:
            status = QUEUED
        else:
            status = OBSERVED

        ticket = self.store.create_ticket(
            source=inbound.source,
            source_ref=inbound.source_ref,
            repo=inbound.repo,
            issue_number=inbound.issue_number,
            requester=inbound.requester,
            request=inbound.request,
            status=status,
            verdict=decision.verdict,
            reason=decision.reason,
            mode=self.settings.mode,
        )
        self.store.add_event(ticket["id"], "assessed", f"{decision.verdict}: {decision.reason}")
        if status == OBSERVED:
            self._reply(
                inbound,
                f"Recorded as {ticket['id']} (observe mode). A human can approve it from the Superagent panel.",
            )
        return ticket

    # -- workers --------------------------------------------------------

    def _launch_queued(self) -> list[str]:
        if self.settings.mode == MODE_OFF:
            return []
        running = len(self.store.list(status=RUNNING, limit=100))
        capacity = max(0, self.settings.max_workers - running)
        if capacity == 0:
            return []
        candidates = list(reversed(self.store.list(status=QUEUED, limit=100)))
        if self.settings.mode == MODE_OBSERVE:
            candidates = [t for t in candidates if t.get("mode") == "manual"]
        launched = []
        for ticket in candidates[:capacity]:
            if self._launch_ticket(ticket["id"]):
                launched.append(ticket["id"])
        return launched

    def _launch_ticket(self, ticket_id: str) -> bool:
        ticket = self.store.get(ticket_id)
        if not ticket or ticket["status"] != QUEUED:
            return False
        if not self.executor:
            self.store.update(ticket_id, error="no worker executor")
            return False
        available, reason = self.executor.available()
        if not available:
            self.store.update(ticket_id, error=reason)
            return False

        goal, context = sa_executor.build_worker_prompt(
            request=ticket["request"],
            requester=ticket["requester"] or "unknown",
            source=ticket["source"],
            ticket_id=ticket_id,
            repo_path=self.settings.repo_path,
        )
        try:
            handle = self.executor.launch(goal=goal, context=context, correlation_id=ticket_id)
        except Exception as exc:  # no parent session, duplicate id, ...
            self.store.update(ticket_id, error=str(exc))
            self.store.add_event(ticket_id, "launch_failed", str(exc))
            return False

        self.store.update(
            ticket_id,
            status=RUNNING,
            handle=json.dumps(handle),
            started_at=time.time(),
            error=None,
        )
        self.store.add_event(ticket_id, "launched", f"worker {handle.get('subagent_id', '?')}")
        return True

    def _wait(self, result: TickResult, timeout_seconds: int) -> None:
        deadline = time.time() + timeout_seconds
        while time.time() < deadline:
            if not self.store.list(status=RUNNING, limit=1):
                return
            time.sleep(1.5)
            self._reap(result)

    def _reap(self, result: Optional[TickResult] = None) -> None:
        for ticket in self.store.list(status=RUNNING, limit=100):
            handle = self._handle(ticket)
            if not handle:
                self._finish(ticket["id"], FAILED, error="worker handle missing")
                continue
            if time.time() - float(ticket.get("started_at") or ticket["created_at"]) > self.settings.max_runtime_seconds:
                try:
                    self.executor.cancel(handle)  # type: ignore[union-attr]
                except Exception:
                    pass
                self._finish(ticket["id"], FAILED, error="worker timed out (watchdog)")
                continue
            try:
                update = self.executor.poll(handle)  # type: ignore[union-attr]
            except Exception as exc:
                self._finish(ticket["id"], FAILED, error=f"worker lost: {exc}")
                continue

            if update.state == "SUCCEEDED":
                self._finish(ticket["id"], REVIEW, result=update.summary or "(worker returned no summary)")
                if result:
                    result.finished.append(ticket["id"])
            elif update.state in {"FAILED", "INTERRUPTED", "CANCELLED"}:
                self._finish(
                    ticket["id"],
                    FAILED,
                    error=update.error or f"worker ended {update.state}",
                    result=update.summary,
                )
                if result:
                    result.finished.append(ticket["id"])

    def _finish(self, ticket_id: str, status: str, *, result: Optional[str] = None, error: Optional[str] = None) -> None:
        self.store.update(ticket_id, status=status, result=result, error=error)
        self.store.add_event(ticket_id, status, (error or result or "")[:500])
        ticket = self.store.get(ticket_id)
        if ticket and ticket["source"] == "github" and ticket.get("issue_number"):
            message = (
                f"Superagent {ticket_id} finished with status **{status}**.\n\n"
                f"{(result or error or '')[:1500]}"
            )
            self._reply_raw(ticket, message)

    # -- helpers --------------------------------------------------------

    def _handle(self, ticket: dict) -> Optional[dict]:
        raw = ticket.get("handle")
        if not raw:
            return None
        try:
            return json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            return None

    def _reply(self, inbound: sa_sources.Inbound, text: str) -> None:
        if inbound.source == "github" and inbound.issue_number and self.github:
            try:
                self.github.reply(inbound.issue_number, text)
            except sa_sources.SourceError as exc:
                self.store.add_event(None, "reply_failed", str(exc))

    def _reply_raw(self, ticket: dict, text: str) -> None:
        if not self.github:
            return
        try:
            self.github.reply(int(ticket["issue_number"]), text)
        except Exception as exc:
            self.store.add_event(ticket["id"], "reply_failed", str(exc))

    @staticmethod
    def _ticket_brief(ticket: dict) -> dict:
        request = (ticket.get("request") or "").strip().replace("\n", " ")
        return {
            "id": ticket["id"],
            "status": ticket["status"],
            "source": ticket["source"],
            "requester": ticket["requester"],
            "request": request[:160] + ("…" if len(request) > 160 else ""),
            "reason": ticket["reason"],
            "error": ticket.get("error"),
            "updated_at": ticket["updated_at"],
        }


def build_engine(ctx: Any) -> Engine:
    """Build an engine from the live plugin context."""
    settings = Settings.from_ctx(ctx)
    store = Store(_db_path())
    executor = sa_executor.SubagentExecutor(ctx.subagent_lifecycle, settings.worker_toolsets)

    github = None
    if settings.github_repo:
        github = sa_sources.GitHubSource(
            repo=settings.github_repo,
            token=settings.github_token or _env("SUPERAGENT_GITHUB_TOKEN") or _env("GITHUB_TOKEN"),
            agent_login=settings.github_agent_login,
            allowed_users=list(settings.github_allowed_users),
        )
    return Engine(store, settings, executor=executor, github=github)


def _db_path() -> Path:
    try:
        from plugins.plugin_storage import plugin_data_dir

        return plugin_data_dir("superagent") / "superagent.db"
    except Exception:
        import os

        home = os.environ.get("HERMES_HOME") or str(Path.home() / ".hermes")
        return Path(home) / "plugin-data" / "superagent" / "superagent.db"


def _env(name: str) -> str:
    import os

    return os.environ.get(name, "")
