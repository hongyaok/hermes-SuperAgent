"""Superagent dashboard backend, mounted at /api/plugins/superagent/.

Ticket views plus desktop-initiated actions for the desktop tab.
GET routes stay read-only for tickets; POST /ask records a ticket, POST /run
queues an approved ticket, POST /tick returns a routing-pass summary.
GET /sessions returns per-ticket enriched session rows for the desktop
sessions grid.

Chat sessions (v0.2) live under /chats: POST /chats starts one headless
`hermes chat` worker, POST /chats/{id}/messages resumes it,
POST /chats/{id}/model swaps the model for future turns, and
POST /chats/{id}/stop cancels it. They are kept in their own namespace so the
existing ticket grid and this release's desktop UI keep working unchanged.

GitHub (v0.3) lives under /github: connect a repository, test the token, poll
pull requests, and start work on one. The token is stored by the shared
settings loader and never appears in a response.

Settings and model presets live under /settings and /models: GET /settings is
the public merged view (token-free), POST /settings validates and saves the
desktop-editable keys, and both /models and POST /chats resolve the same
normalised preset catalog (explicit model/provider > named preset > defaults).

Handoff (v0.3) lives under /chats/{id}: GET /diff returns git status, stat and
a capped patch for the session's repository, GET /comment-preview renders the
summary a human may post, and POST /comment posts exactly one comment to the
session's pull request (a saved token is required; nothing is committed or
pushed).

Imported by the gateway on its own, outside the plugin package, so this
module is self-contained: stdlib sqlite3 only, no relative imports except the
optional package import below.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
import threading
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException

# The gateway loads this file standalone (no package), so the plugin root has
# to be importable before `import sa_chats` / `import sa_runner` below.
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

try:  # loaded as part of the plugin package
    from .. import sa_assessor, sa_chats, sa_diff, sa_github, sa_runner, sa_settings, sa_watcher
except ImportError:  # loaded standalone by the gateway / pytest
    import sa_assessor  # type: ignore[no-redef]
    import sa_chats  # type: ignore[no-redef]
    import sa_diff  # type: ignore[no-redef]
    import sa_github  # type: ignore[no-redef]
    import sa_runner  # type: ignore[no-redef]
    import sa_settings  # type: ignore[no-redef]
    import sa_watcher  # type: ignore[no-redef]

router = APIRouter()

VERSION = "0.4.0"
# Contract generation for the desktop: bumped when a release adds routes the
# panel feature-detects (v2 = session management: rename/archive/delete).
API_VERSION = 2
FEATURES = ("chats", "settings", "github", "diff", "session_management")
# Module import time: when the gateway loaded this plugin, not the request time.
STARTED_AT = time.time()

# Server-side caps (mirrored in sa_runner so the runner enforces them too).
MAX_PROMPT_CHARS = sa_runner.MAX_PROMPT_CHARS
MAX_CONTENT_CHARS = sa_runner.MAX_MESSAGE_CHARS
MAX_EVENT_CHARS = sa_runner.MAX_EVENT_CHARS
# Ticket asks keep their own, shorter limit: a ticket is a request, not a chat.
MAX_ASK_CHARS = 2000

# Historical built-in picker list, kept as a reference for the legacy
# SUPERAGENT_MODEL_PRESETS spelling only. It is never advertised on its own:
# a model id the user has no credentials for turns a preset chip into a
# guaranteed failed worker, which is worse than showing no chip at all.
MODEL_PRESETS = [
    "deepseek-v4.1-flash",
    "qwen/qwen3.8-27b",
    "z-ai/glm-5.3-flash",
]
LEGACY_PRESETS_ENV = "SUPERAGENT_MODEL_PRESETS"

# Settings the panel may change. Everything else (the GitHub token in
# particular) is written by its own endpoint or read-only here.
SETTINGS_MODES = ("off", "observe", "auto")
SETTINGS_KEYS = (
    "mode",
    "repo_path",
    "default_model",
    "default_provider",
    "model_presets",
    "max_workers",
    "worker_yolo",
    "chat_max_turns",
    "github_repo",
    "github_agent_login",
    "github_allowed_users",
    "github_watch_mode",
    "github_watch_new_prs",
    "github_poll_seconds",
)
SETTINGS_EDITABLE = (
    "mode",
    "repo_path",
    "default_model",
    "default_provider",
    "model_presets",
    "max_workers",
    "worker_yolo",
    "chat_max_turns",
)
MIN_WORKERS, MAX_WORKERS_SETTING = 1, 8
MIN_CHAT_TURNS, MAX_CHAT_TURNS = 1, 500
SETTINGS_STRING_LIMIT = 200

# One Runner per database path, created on first use so repeated requests reuse
# the same worker threads and semaphore. Tests patch `_get_runner`.
_RUNNERS: dict[str, Any] = {}
_RUNNERS_LOCK = threading.Lock()
# One GitHub watcher per database path (same reasoning); tests patch
# `_get_watcher`. Kept separate from _RUNNERS so a disconnect can drop the
# watcher without losing the worker pool.
_WATCHERS: dict[str, Any] = {}
_WATCHERS_LOCK = threading.Lock()

_TICKET_COLUMNS = (
    "id", "source", "source_ref", "repo", "issue_number", "requester",
    "request", "status", "verdict", "reason", "mode", "result", "error",
    "created_at", "updated_at", "started_at",
)

# Minimal copy of the ticket-store DDL so POST /ask works even on a fresh
# machine (idempotent; existing tables are left alone).
_SCHEMA = """
CREATE TABLE IF NOT EXISTS tickets (
    id            TEXT PRIMARY KEY,
    source        TEXT NOT NULL,
    source_ref    TEXT,
    repo          TEXT,
    issue_number  INTEGER,
    requester     TEXT,
    request       TEXT NOT NULL,
    status        TEXT NOT NULL,
    verdict       TEXT,
    reason        TEXT,
    mode          TEXT,
    handle        TEXT,
    result        TEXT,
    error         TEXT,
    created_at    REAL NOT NULL,
    updated_at    REAL NOT NULL,
    started_at    REAL
);
CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id  TEXT,
    kind       TEXT NOT NULL,
    detail     TEXT,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS state (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS seen (
    source_key TEXT PRIMARY KEY,
    seen_at    REAL NOT NULL
);
"""

_RUNNABLE = ("observed", "blocked", "failed", "queued")
_TICK_LAUNCH_NOTE = "workers are launched only from a Hermes agent turn (chat or cron)"


def _db_path() -> Path:
    """Plugin database path, shared with the CLI (see sa_settings.data_dir)."""
    return sa_settings.data_dir() / "superagent.db"


def _connect_ro(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def _connect_rw(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(_SCHEMA)
    return conn


def _brief(row: sqlite3.Row) -> dict[str, Any]:
    request = _scrub((row["request"] or "").strip().replace("\n", " "))
    return {
        "id": row["id"],
        "status": row["status"],
        "source": row["source"],
        "requester": row["requester"],
        "request": request[:160] + ("…" if len(request) > 160 else ""),
        "reason": _maybe_scrub(row["reason"]),
        "error": _maybe_scrub(row["error"]),
        "updated_at": row["updated_at"],
    }


def _check_ticket_id(ticket_id: str) -> str:
    ticket_id = (ticket_id or "").strip()
    if not ticket_id or "/" in ticket_id or "\\" in ticket_id or ticket_id in (".", ".."):
        raise HTTPException(status_code=400, detail="invalid ticket id")
    return ticket_id


def _check_session_id(session_id: str) -> str:
    session_id = (session_id or "").strip()
    if not session_id or "/" in session_id or "\\" in session_id or session_id in (".", ".."):
        raise HTTPException(status_code=400, detail="invalid session id")
    return session_id


# -- chat helpers --------------------------------------------------------


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if not raw or not raw.strip():
        return default
    try:
        return int(raw.strip())
    except (TypeError, ValueError):
        return default


def _env_list(name: str, default: list[str]) -> list[str]:
    raw = os.environ.get(name)
    if not raw:
        return list(default)
    items = [part.strip() for part in raw.split(",") if part.strip()]
    return items or list(default)


# -- hardening helpers ---------------------------------------------------


def _scrub(text: Any) -> str:
    """Strip credentials before anything is written to SQLite."""
    return sa_runner.redact_secrets(text)


def _maybe_scrub(value: Any) -> Any:
    """Scrub a nullable column without turning None into an empty string."""
    return None if value is None else _scrub(value)


def _scrub_event(text: Any) -> str:
    """Scrub and apply the 2 000-char event detail cap."""
    return _scrub(text)[:MAX_EVENT_CHARS]


def _scrub_github(text: Any) -> str:
    """Scrub credentials plus the configured GitHub token, whatever its shape.

    `redact_secrets` knows the documented token prefixes; a self-hosted token
    or an unusual value is only known to the settings, so it is replaced
    explicitly before an error reaches an HTTP response.
    """
    value = _scrub_event(text)
    try:
        token = str(sa_settings.load_settings().get("github_token") or "")
    except Exception:
        token = ""
    return value.replace(token, "[redacted]") if token else value


def _repo_root() -> str:
    """The configured root(s) a chat repo must stay inside.

    `SUPERAGENT_REPO_ROOT` (or `SUPERAGENT_REPO`) is the primary root, and the
    plugin's PR workspaces are always allowed alongside it: a GitHub session is
    checked out below `<data_dir>/workspaces` and can be resumed from the chat
    panel. With no root configured, containment stays off, exactly as before.
    """
    repo = (
        os.environ.get("SUPERAGENT_REPO_ROOT") or os.environ.get("SUPERAGENT_REPO") or ""
    ).strip()
    if not repo:
        return ""
    try:
        workspaces = str(sa_settings.data_dir() / "workspaces")
    except Exception:
        workspaces = ""
    return os.pathsep.join(dict.fromkeys(part for part in (repo, workspaces) if part))


def _runner_root(repo: str) -> str:
    """Allowed roots for the shared Runner: the clone plus PR workspaces.

    With no configured clone there is no containment root at all (the previous
    behaviour): the runner falls back to its own default, which is also empty.
    """
    primary = (
        os.environ.get("SUPERAGENT_REPO_ROOT") or repo or os.environ.get("SUPERAGENT_REPO") or ""
    ).strip()
    if not primary:
        return ""
    try:
        workspaces = str(sa_settings.data_dir() / "workspaces")
    except Exception:
        workspaces = ""
    return os.pathsep.join(dict.fromkeys(part for part in (primary, workspaces) if part))


def _validated_repo(repo: str) -> str:
    """Resolve a repo path or fail the request with a 400 (never 500)."""
    try:
        return sa_runner.resolve_repo_path(repo, root=_repo_root())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _chat_store() -> Any:
    """ChatStore on the plugin database (creates the file and tables if needed)."""
    path = _db_path()
    # A chat request may be the first thing that ever writes the file. Keep the
    # ticket tables present too, so /tickets and /sessions keep working.
    _ensure_ticket_schema(path)
    return sa_chats.ChatStore(path)


def _ensure_ticket_schema(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with _connect_rw(path):
        pass  # _connect_rw runs the idempotent ticket DDL


def _get_runner() -> Any:
    """Lazy Runner singleton keyed by database path (patched in tests)."""
    path = _db_path()
    key = str(path)
    with _RUNNERS_LOCK:
        runner = _RUNNERS.get(key)
        if runner is None:
            settings = sa_settings.load_settings()
            repo = str(settings.get("repo_path") or "")
            runner = sa_runner.Runner(
                _chat_store(),
                hermes_bin=os.environ.get("SUPERAGENT_HERMES_BIN") or None,
                max_workers=int(settings.get("max_workers") or 2),
                toolsets=tuple(_env_list("SUPERAGENT_WORKER_TOOLSETS", ["file", "terminal"])),
                yolo=bool(settings.get("worker_yolo", True)),
                model=str(settings.get("default_model") or ""),
                provider=str(settings.get("default_provider") or ""),
                repo=repo,
                repo_root=_runner_root(repo),
                max_turns=int(settings.get("chat_max_turns") or 120),
                turn_timeout_seconds=_env_int(
                    "SUPERAGENT_TURN_TIMEOUT_SECONDS", sa_runner.DEFAULT_TURN_TIMEOUT_SECONDS
                ),
            )
            _RUNNERS[key] = runner
        return runner


def _reset_runner() -> None:
    """Drop the Runner singleton so the next turn reads the new settings.

    A running turn keeps its own process and thread; the next launch (or the
    next resumed turn) builds a Runner with the saved worker cap, yolo flag,
    model defaults and turn limit.
    """
    key = str(_db_path())
    with _RUNNERS_LOCK:
        _RUNNERS.pop(key, None)


def _get_watcher() -> Any:
    """Lazy GitHubWatcher singleton keyed by database path (patched in tests)."""
    key = str(_db_path())
    with _WATCHERS_LOCK:
        watcher = _WATCHERS.get(key)
        if watcher is None:
            watcher = sa_watcher.GitHubWatcher(
                _chat_store(), _get_runner(), sa_settings.load_settings
            )
            _WATCHERS[key] = watcher
        return watcher


def _reset_watcher() -> None:
    """Drop the watcher (and stop its thread) after connect/disconnect."""
    key = str(_db_path())
    with _WATCHERS_LOCK:
        watcher = _WATCHERS.pop(key, None)
    if watcher is not None:
        try:
            watcher.stop_background()
        except Exception:
            pass


def _reap_turns() -> list[dict]:
    """Cancel runaway turns before the chat list answers; never break the read."""
    try:
        runner = _get_runner()
        reap = getattr(runner, "reap", None)
        return list(reap() or []) if callable(reap) else []
    except Exception:
        return []


def _fail_session(store: Any, session_id: str, message: str) -> dict[str, Any]:
    """Mark a session failed when a launch cannot happen, and report it."""
    message = _scrub_event(message) or "launch failed"
    store.update_session(
        session_id, status=sa_chats.FAILED, error=message, finished_at=time.time(), pid=None
    )
    store.add_event(session_id, "failed", message)
    return {
        "ok": False,
        "launched": False,
        "error": message,
        "session": store.get_session(session_id, message_limit=0),
    }



def _session_row(ticket: sqlite3.Row, last_event: dict[str, Any] | None) -> dict[str, Any]:
    request = _scrub((ticket["request"] or "").strip().replace("\n", " "))
    worker_id: str | None = None
    has_handle = False
    try:
        raw_handle = ticket["handle"] if "handle" in ticket.keys() else None
    except Exception:
        raw_handle = None
    if raw_handle:
        has_handle = True
        try:
            parsed = json.loads(raw_handle) if isinstance(raw_handle, str) else raw_handle
            if isinstance(parsed, dict):
                worker_id = parsed.get("subagent_id") or parsed.get("session_id")
        except Exception:
            worker_id = None
    keys = ticket.keys()
    return {
        "id": ticket["id"],
        "status": ticket["status"],
        "source": ticket["source"],
        "requester": ticket["requester"],
        "request": request[:160] + ("…" if len(request) > 160 else ""),
        "reason": _maybe_scrub(ticket["reason"]),
        "error": _maybe_scrub(ticket["error"]),
        "updated_at": ticket["updated_at"],
        "created_at": ticket["created_at"],
        "mode": ticket["mode"] if "mode" in keys else None,
        "verdict": ticket["verdict"] if "verdict" in keys else None,
        "result": _maybe_scrub(ticket["result"]) if "result" in keys else None,
        "worker_id": worker_id,
        "has_handle": has_handle,
        "last_event": last_event,
    }


@router.get("/health")
def health() -> dict[str, Any]:
    """Liveness plus the shape of this API, so a desktop can adapt.

    ``api_version`` is the contract generation; ``features`` names the
    namespaces this build serves. ``started_at`` is the module import time
    (when the gateway loaded the plugin), not the request time.
    """
    path = _db_path()
    archived = 0
    if path.is_file():
        # Read-only and defensive: an older database without chat_sessions (or
        # without the column) still answers, it just reports zero.
        try:
            with _connect_ro(path) as conn:
                archived = int(
                    conn.execute(
                        "SELECT COUNT(*) AS n FROM chat_sessions"
                        " WHERE archived_at IS NOT NULL"
                    ).fetchone()["n"]
                )
        except Exception:
            archived = 0  # a health answer is never worth a 500
    return {
        "ok": True,
        "version": VERSION,
        "api_version": API_VERSION,
        "features": list(FEATURES),
        "pid": os.getpid(),
        "started_at": STARTED_AT,
        "db_exists": path.is_file(),
        "db_path": str(path),
        "archived_count": archived,
    }


@router.get("/tickets")
def list_tickets(limit: int = 30) -> dict[str, Any]:
    path = _db_path()
    if not path.is_file():
        return {"db_exists": False, "counts": {}, "attention": [], "tickets": []}
    limit = max(1, min(limit, 100))
    with _connect_ro(path) as conn:
        counts = {row["status"]: row["n"] for row in conn.execute(
            "SELECT status, COUNT(*) AS n FROM tickets GROUP BY status")}
        attention = [_brief(r) for r in conn.execute(
            "SELECT * FROM tickets WHERE status IN ('observed','blocked','review','failed')"
            " ORDER BY updated_at DESC LIMIT 10")]
        recent = [_brief(r) for r in conn.execute(
            "SELECT * FROM tickets ORDER BY created_at DESC LIMIT ?", (limit,))]
    return {"db_exists": True, "counts": counts, "attention": attention, "tickets": recent,
            "running": counts.get("running", 0), "queued": counts.get("queued", 0)}


@router.get("/tickets/{ticket_id}")
def get_ticket(ticket_id: str) -> dict[str, Any]:
    path = _db_path()
    if not path.is_file():
        raise HTTPException(status_code=503, detail="superagent database not found (no tickets yet)")
    ticket_id = _check_ticket_id(ticket_id)
    with _connect_ro(path) as conn:
        row = conn.execute("SELECT * FROM tickets WHERE id = ?", (ticket_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail=f"unknown ticket {ticket_id}")
        ticket = {col: row[col] for col in _TICKET_COLUMNS if col in row.keys()}
        events = [dict(r) for r in conn.execute(
            "SELECT * FROM events WHERE ticket_id = ? ORDER BY id DESC LIMIT 20", (ticket_id,))]
    ticket["events"] = events
    return ticket


@router.get("/sessions")
def list_sessions(limit: int = 50) -> dict[str, Any]:
    path = _db_path()
    if not path.is_file():
        return {"db_exists": False, "sessions": []}
    limit = max(1, min(limit, 100))
    with _connect_ro(path) as conn:
        rows = conn.execute(
            "SELECT * FROM tickets ORDER BY updated_at DESC LIMIT ?", (limit,)).fetchall()
        sessions = []
        for ticket in rows:
            ev = conn.execute(
                "SELECT kind, detail, created_at FROM events WHERE ticket_id = ?"
                " ORDER BY id DESC LIMIT 1", (ticket["id"],)).fetchone()
            last = dict(ev) if ev is not None else None
            sessions.append(_session_row(ticket, last))
    return {"db_exists": True, "sessions": sessions}


@router.post("/ask")
def ask_ticket(body: dict) -> dict[str, Any]:
    """Record a desktop ask as a ticket, using the same rules as the router.

    A skip marker or acknowledgement is still recorded (as ``ignored``), a
    design question becomes ``blocked`` for a human, and only an actionable
    request becomes ``observed`` work.
    """
    raw_request = str((body or {}).get("request") or "").strip()
    if not raw_request:
        raise HTTPException(status_code=400, detail="request is required")
    if len(raw_request) > MAX_ASK_CHARS:
        raise HTTPException(
            status_code=400, detail=f"request is too long (max {MAX_ASK_CHARS} chars)"
        )
    request = _scrub(raw_request)
    requester = _scrub(str((body or {}).get("requester") or "desktop").strip() or "desktop")

    decision = sa_assessor.assess(request, explicit=True, allow_proactive=False)
    if decision.verdict == sa_assessor.NEEDS_HUMAN:
        status = "blocked"
    elif decision.verdict == sa_assessor.IGNORE:
        status = "ignored"
    else:
        status = "observed"
    reason = _scrub(decision.reason)

    path = _db_path()
    now = time.time()
    with _connect_rw(path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT value FROM state WHERE key = 'ticket_seq'").fetchone()
        seq = int(row["value"]) if row else 0
        seq += 1
        conn.execute(
            "INSERT INTO state(key, value) VALUES('ticket_seq', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (str(seq),),
        )
        ticket_id = f"T-{seq:04d}"
        conn.execute(
            "INSERT INTO tickets (id, source, source_ref, repo, issue_number, requester,"
            " request, status, verdict, reason, mode, handle, result, error,"
            " created_at, updated_at, started_at)"
            " VALUES (?, 'desktop', NULL, NULL, NULL, ?, ?, ?, ?, ?,"
            " 'observe', NULL, NULL, NULL, ?, ?, NULL)",
            (ticket_id, requester, request, status, decision.verdict, reason, now, now),
        )
        conn.execute(
            "INSERT INTO events(ticket_id, kind, detail, created_at) VALUES (?, ?, ?, ?)",
            (ticket_id, "created", _scrub_event(f"desktop request -> {status}"), now),
        )
        conn.execute(
            "INSERT INTO events(ticket_id, kind, detail, created_at) VALUES (?, ?, ?, ?)",
            (
                ticket_id,
                "assessed",
                _scrub_event(f"{decision.verdict}: {decision.reason}"),
                now,
            ),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM tickets WHERE id = ?", (ticket_id,)).fetchone()
    assert row is not None
    return {"ok": True, "ticket": _brief(row)}


@router.post("/run")
def run_ticket(body: dict) -> dict[str, Any]:
    raw = (body or {}).get("ticket_id") or (body or {}).get("id")
    if not raw:
        raise HTTPException(status_code=400, detail="ticket_id is required")
    ticket_id = _check_ticket_id(str(raw))
    path = _db_path()
    if not path.is_file():
        raise HTTPException(status_code=503, detail="superagent database not found (no tickets yet)")
    now = time.time()
    with _connect_rw(path) as conn:
        ticket = conn.execute("SELECT * FROM tickets WHERE id = ?", (ticket_id,)).fetchone()
        if ticket is None:
            raise HTTPException(status_code=404, detail=f"unknown ticket {ticket_id}")
        if ticket["status"] not in _RUNNABLE:
            raise HTTPException(
                status_code=409,
                detail=f"ticket {ticket_id} is {ticket['status']};"
                " only observed, blocked, failed or queued tickets can be run",
            )
        conn.execute(
            "UPDATE tickets SET status = 'queued', mode = 'manual', error = NULL,"
            " updated_at = ? WHERE id = ?",
            (now, ticket_id),
        )
        conn.execute(
            "INSERT INTO events(ticket_id, kind, detail, created_at) VALUES (?, ?, ?, ?)",
            (ticket_id, "promoted", "a human approved the work (desktop)", now),
        )
        conn.commit()
        updated = conn.execute("SELECT * FROM tickets WHERE id = ?", (ticket_id,)).fetchone()
    assert updated is not None
    return {"ok": True, "launched": False, "ticket": _brief(updated),
            "warning": _TICK_LAUNCH_NOTE}


@router.post("/tick")
def tick(body: dict | None = None) -> dict[str, Any]:
    _ = body  # accepted for forward-compat ({wait, launch}); currently a no-op summary
    path = _db_path()
    if not path.is_file():
        return {"db_exists": False, "mode": "observe", "polled": 0, "created": [],
                "launched": [], "finished": [], "warnings": [_TICK_LAUNCH_NOTE],
                "waiting": 0, "counts": {}}
    with _connect_ro(path) as conn:
        counts = {row["status"]: row["n"] for row in conn.execute(
            "SELECT status, COUNT(*) AS n FROM tickets GROUP BY status")}
        latest = conn.execute(
            "SELECT mode FROM tickets ORDER BY updated_at DESC LIMIT 1").fetchone()
    mode = (latest["mode"] if latest and latest["mode"] else "observe")
    running = counts.get("running", 0)
    return {"db_exists": True, "mode": mode, "polled": 0, "created": [],
            "launched": [], "finished": [], "warnings": [_TICK_LAUNCH_NOTE],
            "waiting": running, "counts": counts,
            "running": running, "queued": counts.get("queued", 0)}


# -- chat sessions -------------------------------------------------------
#
# Each chat is one headless `hermes chat` worker (see sa_runner.py): the HTTP
# request creates the record and starts a thread, then the grid polls
# GET /chats for status, tokens and the last event.


# -- settings --------------------------------------------------------------
#
# One small, validated surface over the shared settings loader. GET never
# returns the GitHub token (only `github_token_set`); POST accepts the eight
# keys the desktop panel edits and rejects everything else, so a typo is a 400
# instead of a silently ignored write.


def _preset_catalog(settings: dict[str, Any] | None = None) -> list[dict[str, str]]:
    """The presets this user configured — never a hardcoded catalogue.

    An empty list is a valid answer: the desktop composer then hides its preset
    chip row and the model picker falls back to the configured default model.
    Advertising :data:`MODEL_PRESETS` here used to hand users model ids they may
    have no credentials for, so a chip could only ever start a failed worker.
    The env spelling still wins through :func:`sa_settings.load_settings`.
    """
    data = settings if settings is not None else sa_settings.load_settings()
    presets = sa_settings.normalize_presets(data.get("model_presets") or [])
    if presets:
        return presets
    # The legacy env spelling is the only opt-in that may surface the built-in
    # list (e.g. an env value that normalises to nothing). No env, no chips.
    if os.environ.get(LEGACY_PRESETS_ENV):
        return [{"name": name, "model": name, "provider": ""} for name in MODEL_PRESETS]
    return []


def _settings_payload() -> dict[str, Any]:
    """The public, panel-safe view of the merged settings."""
    public = sa_settings.public_settings()
    payload = {key: public.get(key, sa_settings.DEFAULTS.get(key)) for key in SETTINGS_KEYS}
    payload["model_presets"] = sa_settings.normalize_presets(payload.get("model_presets"))
    payload["github_token_set"] = bool(public.get("github_token_set"))
    return payload


def _settings_int(value: Any, field: str, low: int, high: int) -> int:
    if isinstance(value, bool):
        raise HTTPException(status_code=400, detail=f"{field} must be an integer")
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail=f"{field} must be an integer") from None
    if number < low or number > high:
        raise HTTPException(
            status_code=400, detail=f"{field} must be between {low} and {high}"
        )
    return number


def _settings_bool(value: Any, field: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value if value is not None else "").strip().lower()
    if text in ("true", "1", "yes", "on"):
        return True
    if text in ("false", "0", "no", "off"):
        return False
    raise HTTPException(status_code=400, detail=f"{field} must be true or false")


def _settings_patch(data: Any) -> dict[str, Any]:
    """Validate the editable settings keys; returns only what was supplied."""
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise HTTPException(status_code=400, detail="settings must be an object")
    unknown = sorted(set(data) - set(SETTINGS_EDITABLE))
    if unknown:
        raise HTTPException(
            status_code=400, detail=f"unknown setting(s): {', '.join(unknown)}"
        )

    patch: dict[str, Any] = {}
    if "mode" in data:
        mode = str(data.get("mode") or "").strip().lower()
        if mode not in SETTINGS_MODES:
            raise HTTPException(status_code=400, detail="mode must be 'off', 'observe' or 'auto'")
        patch["mode"] = mode
    for key in ("default_model", "default_provider"):
        if key not in data:
            continue
        value = data.get(key)
        if value is not None and not isinstance(value, str):
            raise HTTPException(status_code=400, detail=f"{key} must be a string")
        text = str(value or "").strip()
        if len(text) > SETTINGS_STRING_LIMIT:
            raise HTTPException(
                status_code=400,
                detail=f"{key} is too long (max {SETTINGS_STRING_LIMIT} chars)",
            )
        patch[key] = text
    if "model_presets" in data:
        raw = data.get("model_presets")
        if raw is not None and not isinstance(raw, (list, tuple)):
            raise HTTPException(status_code=400, detail="model_presets must be a list")
        try:
            patch["model_presets"] = sa_settings.normalize_presets(raw, strict=True)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    if "max_workers" in data:
        patch["max_workers"] = _settings_int(
            data.get("max_workers"), "max_workers", MIN_WORKERS, MAX_WORKERS_SETTING
        )
    if "worker_yolo" in data:
        patch["worker_yolo"] = _settings_bool(data.get("worker_yolo"), "worker_yolo")
    if "chat_max_turns" in data:
        patch["chat_max_turns"] = _settings_int(
            data.get("chat_max_turns"), "chat_max_turns", MIN_CHAT_TURNS, MAX_CHAT_TURNS
        )
    if "repo_path" in data:
        value = data.get("repo_path")
        if value is not None and not isinstance(value, str):
            raise HTTPException(status_code=400, detail="repo_path must be a string")
        raw = str(value or "").strip()
        if raw:
            try:
                raw = sa_runner.resolve_repo_path(raw, root=_repo_root())
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        patch["repo_path"] = raw
    return patch


@router.get("/settings")
def get_settings() -> dict[str, Any]:
    """Merged runtime settings, token-free."""
    return _settings_payload()


@router.post("/settings")
def update_settings(body: dict | None = None) -> dict[str, Any]:
    """Save the editable settings and rebuild the Runner with them."""
    patch = _settings_patch(body)
    if patch:
        try:
            sa_settings.save_settings(patch)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=_scrub(str(exc))) from exc
        except OSError as exc:
            raise HTTPException(status_code=500, detail=_scrub(str(exc))) from exc
        _reset_runner()
    return {"ok": True, "settings": _settings_payload()}


@router.get("/usage")
def usage(days: int = 14) -> dict[str, Any]:
    """Chat activity for the last ``days``: totals, a daily series, per-model rows.

    Read-only and aggregated from the chat tables only — nothing here depends
    on the worker runner, so it answers while every slot is busy.
    """
    days = max(1, min(int(days if days is not None else 14), 90))
    return {
        "ok": True,
        "db_exists": _db_path().is_file(),
        **_chat_store().usage(days),
    }


@router.get("/models")
def list_models() -> dict[str, Any]:
    settings = sa_settings.load_settings()
    presets = _preset_catalog(settings)
    default_model = str(settings.get("default_model") or "").strip()
    # `models` stays the historical flat list the fallback <select> reads:
    # preset models in configured order, then the default model if it is not
    # already covered. Never the hardcoded list.
    models = list(dict.fromkeys(
        [preset["model"] for preset in presets] + ([default_model] if default_model else [])
    ))
    return {
        "default_model": default_model,
        "default_provider": str(settings.get("default_provider") or ""),
        "models": models,
        "presets": presets,
    }


@router.get("/chats")
def list_chats(
    limit: int = 50, status: str = "", archived: str = "hide", q: str = ""
) -> dict[str, Any]:
    """Sessions newest-first; live rows carry ``recent_events``/``elapsed_seconds``.

    ``?status=running,queued`` narrows the rows (comma separated); omitting it
    keeps the historical behaviour. ``?archived=hide|only|all`` picks whether
    archived sessions are visible (default ``hide``); an unknown value is a 400
    rather than a silently different board. Status counts describe the visible
    set, and ``archived_count`` always says how many are filed away.

    ``?q=`` searches instead of listing: titles, opening prompts and every
    stored message are matched case-insensitively, and each returned row adds
    ``match_field`` / ``match_snippet`` so the rail can show why it matched.
    The term is capped at ``SEARCH_QUERY_CHARS``; longer input is a 400 rather
    than a silently truncated search.
    """
    path = _db_path()
    if not path.is_file():
        return {
            "db_exists": False, "sessions": [], "counts": {}, "running": 0,
            "archived_count": 0,
        }
    limit = max(1, min(int(limit or 50), 200))
    mode = str(archived if archived is not None else "hide").strip().lower() or "hide"
    if mode not in sa_chats.ARCHIVED_MODES:
        raise HTTPException(
            status_code=400,
            detail=f"archived must be one of {', '.join(sa_chats.ARCHIVED_MODES)}",
        )
    term = str(q or "").strip()
    if len(term) > sa_chats.SEARCH_QUERY_CHARS:
        raise HTTPException(
            status_code=400,
            detail=f"q must be at most {sa_chats.SEARCH_QUERY_CHARS} characters",
        )
    # A list read is also the supervisor: turns past the timeout are cancelled
    # here, so a forgotten chat cannot hold a worker slot forever.
    _reap_turns()
    statuses = [part.strip() for part in str(status or "").split(",") if part.strip()]
    store = _chat_store()
    if term:
        data = store.search_sessions(
            term, limit=limit, statuses=statuses, archived=mode
        )
    else:
        data = store.list_sessions(limit=limit, statuses=statuses, archived=mode)
    return {"db_exists": True, **data}


@router.get("/chats/{session_id}")
def get_chat(session_id: str) -> dict[str, Any]:
    session_id = _check_session_id(session_id)
    session = _chat_store().get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail=f"unknown chat session {session_id}")
    return {"db_exists": True, "session": session}


@router.get("/chats/{session_id}/export")
def export_chat(session_id: str) -> dict[str, Any]:
    """The transcript as a Markdown document.

    One stored read, rendered by ``sa_chats.session_markdown``: the same text
    the CLI writes and the desktop saves, so a review comment and a saved file
    cannot disagree. It never touches git, the network or the GitHub token.
    """
    session_id = _check_session_id(session_id)
    session = _chat_store().get_session(session_id, message_limit=200, event_limit=200)
    if session is None:
        raise HTTPException(status_code=404, detail=f"unknown chat session {session_id}")
    markdown = sa_chats.session_markdown(session)
    return {
        "ok": True,
        "session_id": session_id,
        "filename": f"superagent-{session_id}.md",
        "markdown": markdown,
        "length": len(markdown),
    }


@router.patch("/chats/{session_id}")
def rename_chat(session_id: str, body: dict | None = None) -> dict[str, Any]:
    """Rename one chat. 400 for an empty/oversized title, 404 for an unknown id.

    A title is a human label, not a prompt: it is trimmed, whitespace-collapsed
    and capped at 200 chars, and nothing else about the session changes.
    """
    session_id = _check_session_id(session_id)
    data = body if isinstance(body, dict) else {}
    raw = data.get("title")
    if raw is not None and not isinstance(raw, str):
        raise HTTPException(status_code=400, detail="title must be a string")
    store = _chat_store()
    try:
        updated = store.rename_session(session_id, raw if raw is not None else "")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if updated is None:
        raise HTTPException(status_code=404, detail=f"unknown chat session {session_id}")
    return {"ok": True, "session": updated}


@router.post("/chats/{session_id}/pin")
def pin_chat(session_id: str, body: dict | None = None) -> dict[str, Any]:
    """Pin a chat to the top of the rail (default), or release it.

    Pinning is display-only, so a live turn is allowed: the row keeps its
    status, transcript and worktree, and unpinning returns it to its real place.
    """
    session_id = _check_session_id(session_id)
    data = body if isinstance(body, dict) else {}
    raw = data.get("pinned", True)
    pinned = True if raw is None else _settings_bool(raw, "pinned")
    updated = _chat_store().set_pinned(session_id, pinned=pinned)
    if updated is None:
        raise HTTPException(status_code=404, detail=f"unknown chat session {session_id}")
    return {"ok": True, "session": updated}


@router.post("/chats/{session_id}/archive")
def archive_chat(session_id: str, body: dict | None = None) -> dict[str, Any]:
    """Archive (default) or restore one chat. 409 while its turn is live.

    The row stays in SQLite with its transcript; only ``archived_at`` changes,
    so the rail can hide it and the header can offer "unarchive".
    """
    session_id = _check_session_id(session_id)
    data = body if isinstance(body, dict) else {}
    raw = data.get("archived", True)
    archived = True if raw is None else _settings_bool(raw, "archived")
    store = _chat_store()
    session = store.get_session(session_id, message_limit=0)
    if session is None:
        raise HTTPException(status_code=404, detail=f"unknown chat session {session_id}")
    if session["status"] in (sa_chats.QUEUED, sa_chats.RUNNING):
        raise HTTPException(
            status_code=409,
            detail=f"session {session_id} is {session['status']}; wait for the turn to finish",
        )
    updated = store.set_archived(session_id, archived=archived)
    if updated is None:  # deleted between the read and the write
        raise HTTPException(status_code=404, detail=f"unknown chat session {session_id}")
    return {"ok": True, "session": updated}


@router.delete("/chats/{session_id}")
def delete_chat(session_id: str) -> dict[str, Any]:
    """Delete one chat and its transcript. 409 while its turn is live.

    A database delete only: the session row, its messages and its events go;
    the repository, worktree, branch and pull request are never touched.
    """
    session_id = _check_session_id(session_id)
    store = _chat_store()
    session = store.get_session(session_id, message_limit=0)
    if session is None:
        raise HTTPException(status_code=404, detail=f"unknown chat session {session_id}")
    if session["status"] in (sa_chats.QUEUED, sa_chats.RUNNING):
        raise HTTPException(
            status_code=409,
            detail=f"session {session_id} is {session['status']}; stop it before deleting",
        )
    if not store.delete_session(session_id):
        raise HTTPException(status_code=404, detail=f"unknown chat session {session_id}")
    return {"ok": True, "id": session_id}


@router.post("/chats")
def create_chat(body: dict) -> dict[str, Any]:
    data = body or {}
    raw_prompt = str(data.get("prompt") or "").strip()
    if not raw_prompt:
        raise HTTPException(status_code=400, detail="prompt is required")
    if len(raw_prompt) > MAX_PROMPT_CHARS:
        raise HTTPException(
            status_code=400, detail=f"prompt is too long (max {MAX_PROMPT_CHARS} chars)"
        )
    prompt = _scrub(raw_prompt)
    settings = sa_settings.load_settings()
    preset_name = _scrub(str(data.get("preset") or "").strip())
    preset: dict[str, str] | None = None
    if preset_name:
        presets = _preset_catalog(settings)
        preset = next((item for item in presets if item["name"] == preset_name), None)
        if preset is None:
            available = ", ".join(item["name"] for item in presets) or "(none configured)"
            raise HTTPException(
                status_code=400,
                detail=f"unknown preset '{preset_name}'; available: {available}",
            )
    explicit_model = str(data.get("model") or "").strip()
    explicit_provider = str(data.get("provider") or "").strip()
    # Resolution order per field: explicit request > named preset > settings.
    model = _scrub(
        explicit_model
        or str((preset or {}).get("model") or "")
        or str(settings.get("default_model") or "")
    ).strip()
    provider = _scrub(
        explicit_provider
        or str((preset or {}).get("provider") or "")
        or str(settings.get("default_provider") or "")
    ).strip()
    repo = _validated_repo(str(data.get("repo") or os.environ.get("SUPERAGENT_REPO", "") or "").strip())
    title = _scrub(str(data.get("title") or "").strip())
    source = str(data.get("source") or "desktop").strip() or "desktop"

    store = _chat_store()
    session = store.create_session(
        prompt,
        title=title[:200],
        model=model[:200],
        provider=provider[:200],
        repo=repo[:1000],
        source=source[:40],
    )
    session_id = session["id"]
    if preset is not None:
        label = f"{provider}: {model}" if provider and model else (model or provider or "default")
        store.add_event(session_id, "preset", f"{preset_name} -> {label}")
    try:
        _get_runner().launch_turn(session_id, prompt, model=model, provider=provider, repo=repo)
    except Exception as exc:  # missing binary, bad repo, ...
        return _fail_session(store, session_id, str(exc) or exc.__class__.__name__)
    store.add_event(session_id, "launched", "headless hermes chat worker")
    return {
        "ok": True,
        "launched": True,
        "session": store.get_session(session_id, message_limit=0),
    }


@router.post("/chats/{session_id}/messages")
def post_chat_message(session_id: str, body: dict) -> dict[str, Any]:
    session_id = _check_session_id(session_id)
    raw_content = str((body or {}).get("content") or "").strip()
    if not raw_content:
        raise HTTPException(status_code=400, detail="content is required")
    if len(raw_content) > MAX_CONTENT_CHARS:
        raise HTTPException(
            status_code=400, detail=f"content is too long (max {MAX_CONTENT_CHARS} chars)"
        )
    content = _scrub(raw_content)

    store = _chat_store()
    session = store.get_session(session_id, message_limit=0)
    if session is None:
        raise HTTPException(status_code=404, detail=f"unknown chat session {session_id}")
    if session["status"] in (sa_chats.QUEUED, sa_chats.RUNNING):
        raise HTTPException(
            status_code=409,
            detail=f"session {session_id} is {session['status']}; wait for the turn to finish",
        )

    model = _scrub(session.get("model") or "")
    provider = _scrub(session.get("provider") or "")
    # The stored repo was validated at creation; validate again so a deleted
    # or replaced directory fails as a 400 instead of reaching argv.
    repo = _validated_repo(session.get("repo") or "")
    resume_id = (session.get("hermes_session_id") or "").strip()

    if not resume_id:
        # Nothing to resume (the first turn never started). Keep the user's
        # message by starting a new chat instead of dropping it.
        replacement = store.create_session(
            content,
            model=model,
            provider=provider,
            repo=repo,
            source=session.get("source") or "desktop",
        )
        new_id = replacement["id"]
        store.add_message(new_id, "user", content)
        try:
            _get_runner().launch_turn(new_id, content, model=model, provider=provider, repo=repo)
        except Exception as exc:
            return _fail_session(store, new_id, str(exc) or exc.__class__.__name__) | {
                "previous_session_id": session_id,
            }
        store.add_event(new_id, "launched", f"new chat (nothing to resume in {session_id})")
        return {
            "ok": True,
            "launched": True,
            "resumed": False,
            "previous_session_id": session_id,
            "session": store.get_session(new_id),
        }

    store.add_message(session_id, "user", content)
    try:
        _get_runner().launch_turn(
            session_id,
            content,
            resume_session_id=resume_id,
            model=model,
            provider=provider,
            repo=repo,
        )
    except Exception as exc:
        return _fail_session(store, session_id, str(exc) or exc.__class__.__name__) | {
            "resumed": True,
        }
    store.add_event(session_id, "launched", f"resumed {resume_id}")
    return {
        "ok": True,
        "launched": True,
        "resumed": True,
        "session": store.get_session(session_id),
    }


@router.post("/chats/{session_id}/retry")
def retry_chat(session_id: str) -> dict[str, Any]:
    """Re-run the last prompt of a failed or cancelled chat.

    A retry is not a new message: the transcript already holds the user's text,
    so the same row is relaunched with that prompt, resuming its Hermes session
    when one was created. A live turn is already working and a ``review`` chat
    is a result to read, so both are 409 rather than a silent duplicate.
    """
    session_id = _check_session_id(session_id)
    store = _chat_store()
    session = store.get_session(session_id, message_limit=0)
    if session is None:
        raise HTTPException(status_code=404, detail=f"unknown chat session {session_id}")
    status = session["status"]
    if status in (sa_chats.QUEUED, sa_chats.RUNNING):
        raise HTTPException(
            status_code=409, detail=f"session {session_id} is {status}; nothing to retry"
        )
    if status not in (sa_chats.FAILED, sa_chats.CANCELLED):
        raise HTTPException(
            status_code=409,
            detail=(
                f"session {session_id} is {status}; retry only a failed or cancelled chat"
            ),
        )
    message = store.last_user_message(session_id)
    prompt = str((message or {}).get("content") or "").strip()
    if not prompt:
        raise HTTPException(status_code=400, detail="no user message to retry")

    model = _scrub(session.get("model") or "")
    provider = _scrub(session.get("provider") or "")
    # The stored repo was validated when the chat was created; validate again
    # so a deleted or replaced directory fails as a 400, never as argv.
    repo = _validated_repo(session.get("repo") or "")
    resume_id = (session.get("hermes_session_id") or "").strip()

    # Forget the failed attempt's numbers: a retried turn starts clean, and a
    # stale token count next to a live turn reads as if it were already done.
    store.update_session(
        session_id,
        error=None,
        tokens=None,
        duration_ms=None,
        last_event=None,
        finished_at=None,
    )
    try:
        _get_runner().launch_turn(
            session_id,
            prompt,
            resume_session_id=resume_id or None,
            model=model,
            provider=provider,
            repo=repo,
        )
    except Exception as exc:
        return _fail_session(store, session_id, str(exc) or exc.__class__.__name__)
    resumed = f"resumed {resume_id}" if resume_id else "fresh session"
    store.add_event(session_id, "retry", f"retry after {status} ({resumed})")
    return {
        "ok": True,
        "launched": True,
        "resumed": bool(resume_id),
        "session": store.get_session(session_id),
    }


@router.post("/chats/{session_id}/model")
def set_chat_model(session_id: str, body: dict) -> dict[str, Any]:
    """Change the model/provider a session will use for its *next* turn.

    The model is fixed for a chat at creation time in the composer, so this is
    the only way to move an existing conversation to another model. A live turn
    already passed its model to the worker: 409 until it finishes.
    """
    session_id = _check_session_id(session_id)
    data = body or {}
    model = str(data.get("model") or "").strip()
    provider = str(data.get("provider") or "").strip()
    if not model and not provider:
        raise HTTPException(status_code=400, detail="model or provider is required")

    store = _chat_store()
    session = store.get_session(session_id, message_limit=0)
    if session is None:
        raise HTTPException(status_code=404, detail=f"unknown chat session {session_id}")
    if session["status"] in (sa_chats.QUEUED, sa_chats.RUNNING):
        raise HTTPException(
            status_code=409,
            detail=f"session {session_id} is {session['status']}; wait for the turn to finish",
        )

    model = model[:200]
    provider = provider[:200]
    label = f"{provider}: {model}" if model and provider else (model or provider)
    # Event first, then the row: once the new model is visible its audit trail
    # already explains it (same order the runner uses when it finishes a turn).
    store.add_event(session_id, "model", f"next turn uses {label}")
    updated = store.update_session(session_id, model=model, provider=provider)
    return {"ok": True, "session": updated}


@router.post("/chats/{session_id}/stop")
def stop_chat(session_id: str) -> dict[str, Any]:
    session_id = _check_session_id(session_id)
    store = _chat_store()
    session = store.get_session(session_id, message_limit=0)
    if session is None:
        raise HTTPException(status_code=404, detail=f"unknown chat session {session_id}")
    if session["status"] not in (sa_chats.QUEUED, sa_chats.RUNNING):
        raise HTTPException(
            status_code=409,
            detail=f"session {session_id} is {session['status']}; nothing to stop",
        )
    try:
        stopped = bool(_get_runner().stop(session_id))
    except Exception as exc:
        detail = _scrub_event(str(exc)) or "stop failed"
        store.update_session(
            session_id,
            status=sa_chats.CANCELLED,
            error=detail,
            finished_at=time.time(),
            pid=None,
        )
        store.add_event(session_id, "cancelled", detail)
        stopped = False
    return {
        "ok": True,
        "stopped": stopped,
        "session": store.get_session(session_id, message_limit=0),
    }


# -- session handoff: diff + comment (v0.3) --------------------------------
#
# Closing the loop on a finished chat: show what the worker actually changed in
# its repository, then post a human-reviewed summary back to the pull request.
# Neither handler commits, pushes or edits the pull request beyond one comment.

# A diff response is capped here as well as in sa_diff: a lockfile rewrite must
# not travel through JSON and the desktop panel unbounded.
DIFF_PATCH_LIMIT = sa_diff.DEFAULT_PATCH_CHARS
COMMENT_BODY_LIMIT = 20000
COMMENT_PREVIEW_CHARS = 280


def _session_repo(session: dict) -> str:
    """The session's repository, resolved, or a 400 explaining why not."""
    repo = str((session or {}).get("repo") or "").strip()
    if not repo:
        raise HTTPException(
            status_code=400,
            detail="this session has no repository; start it with a repo path first",
        )
    try:
        return sa_runner.resolve_repo_path(repo, root=_repo_root())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=_scrub(str(exc))) from exc


def _session_diff(session: dict) -> dict[str, Any]:
    """collect_diff for a session's repo; a broken repo is still a 400."""
    diff = sa_diff.collect_diff(_session_repo(session), max_patch_chars=DIFF_PATCH_LIMIT)
    if not diff.get("ok"):
        raise HTTPException(
            status_code=400, detail=_scrub(str(diff.get("error") or "could not read the diff"))
        )
    return diff


def _session_or_404(store: Any, session_id: str) -> dict[str, Any]:
    session = store.get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail=f"unknown chat session {session_id}")
    return session


def _comment_pr(session: dict[str, Any]) -> tuple[int, str]:
    """(PR number, GitHub repo) for a comment, or the 400 that stops it.

    Token-free on purpose: the preview needs the PR and the repository to
    render the same body the post would send, but asking for a token before
    there is anything to read would be rude.
    """
    pr_number = session.get("pr_number")
    if not pr_number:
        raise HTTPException(
            status_code=400,
            detail=(
                f"session {session.get('id')} is not linked to a pull request;"
                " start it from GitHub or `github work <n>`"
            ),
        )
    settings = sa_settings.load_settings()
    repo = str(session.get("github_repo") or settings.get("github_repo") or "").strip()
    if not sa_settings.valid_repo_name(repo):
        raise HTTPException(status_code=400, detail="no GitHub repository is configured")
    try:
        number = int(pr_number)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="the session's pull request number is invalid") from None
    return number, repo


def _comment_token() -> str:
    """The saved GitHub token, or the 400 that says how to set one."""
    token = str(sa_settings.load_settings().get("github_token") or "").strip()
    if not token:
        raise HTTPException(
            status_code=400, detail="a GitHub token is required to comment (Settings → GitHub)"
        )
    return token


def _comment_body(session: dict[str, Any], body: Any, diff: dict[str, Any] | None) -> str:
    """The text to post: the caller's, or the generated summary, capped."""
    supplied = str(body or "").strip()
    if supplied:
        text = _scrub(supplied)
    else:
        text = sa_diff.default_comment_body(session, diff, limit=COMMENT_BODY_LIMIT)
    text = text.strip()[:COMMENT_BODY_LIMIT]
    if not text:
        raise HTTPException(status_code=400, detail="the comment body is empty")
    return text


@router.get("/chats/{session_id}/diff")
def chat_diff(session_id: str) -> dict[str, Any]:
    """Status, stat and (capped) patch for the session's repository.

    404 for an unknown session, 400 when the session has no repo or the repo
    no longer exists / is not a git checkout. The patch is truncated with a
    marker rather than dropped, so the panel can still show the shape of a
    large change.
    """
    session_id = _check_session_id(session_id)
    session = _session_or_404(_chat_store(), session_id)
    diff = _session_diff(session)
    return {"db_exists": True, "session_id": session_id, **diff}


@router.get("/chats/{session_id}/comment-preview")
def chat_comment_preview(session_id: str) -> dict[str, Any]:
    """The body ``POST /comment`` would post — no network, no writes.

    The diff totals are included when the repository still reads; a missing
    repo only drops that line here (the post itself refuses with a 400).
    """
    session_id = _check_session_id(session_id)
    session = _session_or_404(_chat_store(), session_id)
    pr_number, repo = _comment_pr(session)
    diff: dict[str, Any] | None = None
    try:
        diff = sa_diff.collect_diff(_session_repo(session), max_patch_chars=DIFF_PATCH_LIMIT)
    except HTTPException:
        diff = None
    body = sa_diff.default_comment_body(session, diff, limit=COMMENT_BODY_LIMIT)
    return {
        "ok": True,
        "session_id": session_id,
        "pr_number": pr_number,
        "github_repo": repo,
        "url": str(session.get("pr_url") or ""),
        "body": body,
        "length": len(body),
    }


@router.post("/chats/{session_id}/comment")
def chat_comment(session_id: str, body: dict | None = None) -> dict[str, Any]:
    """Post one comment to the session's PR after a human confirms it.

    400 without a PR, a GitHub repo, a saved token or a readable repository;
    502 when GitHub rejects the post (message redacted). Never commits, pushes
    or changes the PR itself.
    """
    session_id = _check_session_id(session_id)
    store = _chat_store()
    session = _session_or_404(store, session_id)
    pr_number, github_repo = _comment_pr(session)
    token = _comment_token()
    # Reading the diff first keeps the "repo is gone" and "no PR" answers
    # separate, and gives the generated body its file totals.
    diff = _session_diff(session)
    supplied = (body or {}).get("body")
    if supplied is not None and len(str(supplied)) > COMMENT_BODY_LIMIT:
        raise HTTPException(
            status_code=400,
            detail=f"comment is too long (max {COMMENT_BODY_LIMIT} chars)",
        )
    text = _comment_body(session, supplied, diff)

    client = sa_github.GitHubClient(token, github_repo)
    try:
        posted = client.post_comment(pr_number, text)
    except sa_github.GitHubError as exc:
        raise HTTPException(status_code=502, detail=_scrub_github(str(exc))) from exc
    url = str((posted or {}).get("html_url") or "")
    store.add_event(
        session_id, "commented", f"PR #{pr_number}: {url}" if url else f"PR #{pr_number}"
    )
    preview = text if len(text) <= COMMENT_PREVIEW_CHARS else text[:COMMENT_PREVIEW_CHARS] + "…"
    return {
        "ok": True,
        "url": url,
        "pr_number": pr_number,
        "github_repo": github_repo,
        "body_preview": preview,
        "session": store.get_session(session_id, message_limit=0),
    }


# -- GitHub (v0.3) -------------------------------------------------------
#
# Connect a repository, poll its pull requests and comments, and start work on
# a PR. The watcher owns the polling loop and the dedupe state; these handlers
# only validate input, persist settings and answer with JSON. The token is
# read from settings and never copied into a response or an event.


def _github_settings() -> dict[str, Any]:
    return sa_settings.load_settings()


# Settings the panel can change without reconnecting. Values are validated
# here (not in sa_settings) so a bad form field is a 400 with a useful message
# instead of a silent coercion.
GITHUB_WATCH_MODES = ("observe", "auto")
GITHUB_POLL_MIN_SECONDS = 15
GITHUB_POLL_MAX_SECONDS = 3600
_GITHUB_LOGIN_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")
_TRUE = ("true", "1", "yes", "on")
_FALSE = ("false", "0", "no", "off")


def _clean_allowed_users(value: Any) -> list[str]:
    """Normalise an allow-list: strip, drop empties/@handles, dedupe.

    GitHub logins are case-insensitive, so the first spelling wins and later
    duplicates are dropped. A leading `@` (how people paste a mention) is
    removed rather than stored, because the watcher compares bare logins.
    """
    if isinstance(value, str):
        parts: list[Any] = value.split(",")
    elif isinstance(value, (list, tuple, set)):
        parts = list(value)
    else:
        raise ValueError("allowed_users must be a list or a comma-separated string")
    cleaned: list[str] = []
    seen: set[str] = set()
    for part in parts:
        name = str(part if part is not None else "").strip()
        if name.startswith("@"):
            name = name[1:].strip()
        if not name:
            continue
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(name)
    return cleaned


def _github_bool(value: Any, field: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value if value is not None else "").strip().lower()
    if text in _TRUE:
        return True
    if text in _FALSE:
        return False
    raise HTTPException(status_code=400, detail=f"{field} must be true or false")


def _github_settings_patch(data: dict) -> dict[str, Any]:
    """Validate the optional settings keys shared by /connect and /settings.

    Only keys present in the body are returned, so a patch never blanks a
    value the caller did not mention. Raises 400 on anything malformed.
    """
    patch: dict[str, Any] = {}
    if "allowed_users" in data:
        try:
            patch["github_allowed_users"] = _clean_allowed_users(data.get("allowed_users"))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    if "watch_mode" in data:
        mode = str(data.get("watch_mode") or "").strip().lower()
        if mode not in GITHUB_WATCH_MODES:
            raise HTTPException(status_code=400, detail="watch_mode must be 'observe' or 'auto'")
        patch["github_watch_mode"] = mode
    if "watch_new_prs" in data:
        patch["github_watch_new_prs"] = _github_bool(data.get("watch_new_prs"), "watch_new_prs")
    if "poll_seconds" in data:
        raw = data.get("poll_seconds")
        try:
            seconds = int(str(raw).strip()) if not isinstance(raw, bool) else 0
        except (TypeError, ValueError):
            seconds = 0
        if seconds < GITHUB_POLL_MIN_SECONDS or seconds > GITHUB_POLL_MAX_SECONDS:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"poll_seconds must be between {GITHUB_POLL_MIN_SECONDS}"
                    f" and {GITHUB_POLL_MAX_SECONDS}"
                ),
            )
        patch["github_poll_seconds"] = seconds
    if "agent_login" in data:
        login = str(data.get("agent_login") or "").strip()
        if login.startswith("@"):
            login = login[1:].strip()
        if login and not _GITHUB_LOGIN_RE.match(login):
            raise HTTPException(status_code=400, detail="agent_login is not a valid GitHub login")
        patch["github_agent_login"] = login
    return patch


def _github_identity(client: Any) -> tuple[str, dict[str, Any]]:
    """Authenticated login + repository info; any failure is a 400."""
    try:
        login = str((client.me() or {}).get("login") or "")
        info = client.repo_info()
    except sa_github.GitHubError as exc:
        raise HTTPException(status_code=400, detail=_scrub(str(exc))) from exc
    return login, info


def _clear_repo_baseline(repo: str) -> None:
    """Forget dedupe state for one repo, so a reconnect starts a new baseline."""
    if not repo:
        return
    try:
        store = _chat_store()
        for prefix in (
            f"gh_baseline:{repo}",
            f"gh_comment:{repo}:",
            f"gh_head:{repo}:",
        ):
            store.clear_seen(prefix)
    except Exception:
        pass


@router.get("/github/status")
def github_status() -> dict[str, Any]:
    watcher = _get_watcher()
    try:
        watcher.start_background()
    except Exception:
        pass  # status must answer even when the thread cannot start
    return watcher.status()


@router.post("/github/test")
def github_test(body: dict | None = None) -> dict[str, Any]:
    """Check a token + repository without saving anything."""
    data = body or {}
    settings = _github_settings()
    repo = str(data.get("repo") or settings.get("github_repo") or "").strip()
    token = str(data.get("token") or settings.get("github_token") or "").strip()
    if not sa_settings.valid_repo_name(repo):
        raise HTTPException(status_code=400, detail="repository must be owner/name")
    if not token:
        raise HTTPException(status_code=400, detail="a GitHub token is required")
    client = sa_github.GitHubClient(token, repo)
    login, info = _github_identity(client)
    try:
        open_pr_count = len(client.list_open_prs(limit=100))
    except sa_github.GitHubError:
        open_pr_count = int(info.get("open_issues") or 0)
    return {
        "ok": True,
        "login": login,
        "full_name": info.get("full_name") or repo,
        "default_branch": info.get("default_branch") or "",
        "private": bool(info.get("private")),
        "open_pr_count": open_pr_count,
    }


@router.post("/github/connect")
def github_connect(body: dict) -> dict[str, Any]:
    """Validate a repository/token pair, save it, and start watching.

    Optional `allowed_users`, `watch_mode`, `watch_new_prs`, `poll_seconds` and
    `agent_login` keys are applied in the same write, so the panel's Connect
    button saves the form in one round-trip.
    """
    data = body or {}
    repo = str(data.get("repo") or "").strip()
    if not sa_settings.valid_repo_name(repo):
        raise HTTPException(status_code=400, detail="repository must be owner/name")
    extra = _github_settings_patch(data)
    settings = _github_settings()
    supplied_token = str(data.get("token") or "").strip()
    token = supplied_token or str(settings.get("github_token") or "").strip()
    if not token:
        raise HTTPException(status_code=400, detail="a GitHub token is required")

    client = sa_github.GitHubClient(token, repo)
    login, info = _github_identity(client)

    patch: dict[str, Any] = {"github_repo": repo}
    patch.update(extra)
    if supplied_token:
        patch["github_token"] = supplied_token
    if login and "github_agent_login" not in patch:
        patch["github_agent_login"] = login
    try:
        sa_settings.save_settings(patch)
    except (ValueError, OSError) as exc:
        raise HTTPException(status_code=500, detail=_scrub(str(exc))) from exc

    _clear_repo_baseline(repo)
    _reset_watcher()
    watcher = _get_watcher()
    try:
        watcher.start_background()
    except Exception:
        pass
    status = watcher.status()
    result = dict(status)
    result.update(
        {
            "ok": True,
            "login": status.get("login") or login,
            "full_name": info.get("full_name") or repo,
        }
    )
    return result


@router.post("/github/settings")
def github_settings(body: dict) -> dict[str, Any]:
    """Save watch settings without touching the token or the repository.

    The watcher is dropped afterwards so the next status/poll reads the new
    allowed list, mode and interval instead of the cached settings dict.
    """
    patch = _github_settings_patch(body or {})
    try:
        sa_settings.save_settings(patch)
    except (ValueError, OSError) as exc:
        raise HTTPException(status_code=500, detail=_scrub(str(exc))) from exc
    _reset_watcher()
    watcher = _get_watcher()
    try:
        watcher.start_background()
    except Exception:
        pass  # a status answer is still useful when the thread cannot start
    return {"ok": True, "status": watcher.status()}


@router.post("/github/disconnect")
def github_disconnect() -> dict[str, Any]:
    """Forget the repository and token and stop polling."""
    try:
        sa_settings.save_settings({"github_repo": "", "github_token": ""})
    except (ValueError, OSError) as exc:
        raise HTTPException(status_code=500, detail=_scrub(str(exc))) from exc
    _reset_watcher()
    return {"ok": True}


@router.post("/github/poll")
def github_poll() -> dict[str, Any]:
    """Run one polling pass now (the UI's 'Check now' button and tests)."""
    try:
        summary = _get_watcher().poll_once()
    except Exception as exc:
        message = _scrub_github(str(exc)) or "poll failed"
        return {"configured": False, "polled": 0, "created": [], "errors": [message]}
    return {
        "configured": bool(summary.get("configured")),
        "polled": int(summary.get("polled") or 0),
        "created": list(summary.get("created") or []),
        "errors": [_scrub_github(str(item)) for item in (summary.get("errors") or [])],
    }


@router.get("/github/prs")
def github_prs() -> dict[str, Any]:
    try:
        status = _get_watcher().status()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=_scrub(str(exc))) from exc
    return {"configured": bool(status.get("configured")), "prs": status.get("open_prs") or []}


@router.post("/github/prs/{number}/work")
def github_pr_work(number: int, body: dict | None = None) -> dict[str, Any]:
    """Start a worker on one PR immediately (an explicit human action)."""
    if number <= 0:
        raise HTTPException(status_code=400, detail="invalid pull request number")
    settings = _github_settings()
    repo = str(settings.get("github_repo") or "").strip()
    token = str(settings.get("github_token") or "").strip()
    if not repo or not token:
        raise HTTPException(status_code=400, detail="GitHub is not connected")
    client = sa_github.GitHubClient(token, repo)
    try:
        pr = client.get_pr(number)
    except sa_github.GitHubError as exc:
        code = 404 if exc.status == 404 else 400
        detail = f"pull request #{number} was not found" if code == 404 else _scrub(str(exc))
        raise HTTPException(status_code=code, detail=detail) from exc

    repo_path = str(settings.get("repo_path") or "").strip()
    checkout = ""
    warning = ""
    if repo_path:
        workspace = str(sa_settings.data_dir() / "workspaces")
        try:
            info = sa_github.prepare_pr_checkout(repo_path, repo, number, workspace, token)
            checkout = str(info.get("path") or "")
        except sa_github.GitHubError as exc:
            warning = _scrub(str(exc))

    request_text = _scrub(str((body or {}).get("request") or "").strip())
    comment = {"author": "you", "body": request_text} if request_text else None
    prompt = sa_github.build_pr_prompt(pr, comment=comment, checkout=checkout)
    session_repo = checkout
    if not session_repo and repo_path and Path(repo_path).expanduser().is_dir():
        session_repo = repo_path
    store = _chat_store()
    session = store.create_session(
        prompt,
        title=f"PR #{number}: {pr.get('title') or ''}".strip(),
        model=str(settings.get("default_model") or ""),
        provider=str(settings.get("default_provider") or ""),
        repo=session_repo,
        source="github_pr",
        github_repo=repo,
        pr_number=number,
        pr_url=str(pr.get("html_url") or ""),
        status=sa_chats.QUEUED,
    )
    session_id = str(session["id"])
    store.add_event(session_id, "github", f"manual work on PR #{number}")
    if warning:
        store.add_event(session_id, "github", f"checkout failed: {warning}")

    target = session_repo
    try:
        _get_runner().launch_turn(
            session_id,
            prompt,
            model=str(settings.get("default_model") or "") or None,
            provider=str(settings.get("default_provider") or "") or None,
            repo=target,
        )
    except Exception as exc:
        failed = _fail_session(store, session_id, str(exc) or exc.__class__.__name__)
        failed["checkout"] = checkout
        failed["warning"] = warning
        return failed
    return {
        "ok": True,
        "launched": True,
        "session": store.get_session(session_id, message_limit=0),
        "checkout": checkout,
        "warning": warning,
    }

