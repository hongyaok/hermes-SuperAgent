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

Imported by the gateway on its own, outside the plugin package, so this
module is self-contained: stdlib sqlite3 only, no relative imports except the
optional package import below.
"""
from __future__ import annotations

import json
import os
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
    from .. import sa_assessor, sa_chats, sa_runner
except ImportError:  # loaded standalone by the gateway / pytest
    import sa_assessor  # type: ignore[no-redef]
    import sa_chats  # type: ignore[no-redef]
    import sa_runner  # type: ignore[no-redef]

router = APIRouter()

VERSION = "0.2.0"

# Server-side caps (mirrored in sa_runner so the runner enforces them too).
MAX_PROMPT_CHARS = sa_runner.MAX_PROMPT_CHARS
MAX_CONTENT_CHARS = sa_runner.MAX_MESSAGE_CHARS
MAX_EVENT_CHARS = sa_runner.MAX_EVENT_CHARS
# Ticket asks keep their own, shorter limit: a ticket is a request, not a chat.
MAX_ASK_CHARS = 2000

# Models offered in the per-session model picker. Extend as providers change;
# SUPERAGENT_MODEL_PRESETS (comma separated) overrides this list.
MODEL_PRESETS = [
    "deepseek-v4.1-flash",
    "qwen/qwen3.8-27b",
    "z-ai/glm-5.3-flash",
]

# One Runner per database path, created on first use so repeated requests reuse
# the same worker threads and semaphore. Tests patch `_get_runner`.
_RUNNERS: dict[str, Any] = {}
_RUNNERS_LOCK = threading.Lock()

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
    try:
        from hermes_constants import get_hermes_home

        home = Path(get_hermes_home())
    except Exception:
        home = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")
    return home / "plugin-data" / "superagent" / "superagent.db"


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


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


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


def _repo_root() -> str:
    """The configured root every chat repo must stay inside."""
    return (
        os.environ.get("SUPERAGENT_REPO_ROOT")
        or os.environ.get("SUPERAGENT_REPO")
        or ""
    ).strip()


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
            runner = sa_runner.Runner(
                _chat_store(),
                hermes_bin=os.environ.get("SUPERAGENT_HERMES_BIN") or None,
                max_workers=_env_int("SUPERAGENT_MAX_WORKERS", 2),
                toolsets=tuple(_env_list("SUPERAGENT_WORKER_TOOLSETS", ["file", "terminal"])),
                yolo=_env_bool("SUPERAGENT_WORKER_YOLO", True),
                model=os.environ.get("SUPERAGENT_DEFAULT_MODEL", ""),
                provider=os.environ.get("SUPERAGENT_DEFAULT_PROVIDER", ""),
                repo=os.environ.get("SUPERAGENT_REPO", ""),
                max_turns=_env_int("SUPERAGENT_CHAT_MAX_TURNS", 120),
                turn_timeout_seconds=_env_int(
                    "SUPERAGENT_TURN_TIMEOUT_SECONDS", sa_runner.DEFAULT_TURN_TIMEOUT_SECONDS
                ),
            )
            _RUNNERS[key] = runner
        return runner


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
    path = _db_path()
    return {"ok": True, "version": VERSION, "db_exists": path.is_file(), "db_path": str(path)}


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


@router.get("/models")
def list_models() -> dict[str, Any]:
    presets = _env_list("SUPERAGENT_MODEL_PRESETS", MODEL_PRESETS)
    return {
        "default_model": os.environ.get("SUPERAGENT_DEFAULT_MODEL", "") or "",
        "default_provider": os.environ.get("SUPERAGENT_DEFAULT_PROVIDER", "") or "",
        "models": presets,
    }


@router.get("/chats")
def list_chats(limit: int = 50, status: str = "") -> dict[str, Any]:
    """Sessions newest-first; live rows carry ``recent_events``/``elapsed_seconds``.

    ``?status=running,queued`` narrows the rows (comma separated); omitting it
    keeps the historical behaviour. Status counts stay board-wide.
    """
    path = _db_path()
    if not path.is_file():
        return {"db_exists": False, "sessions": [], "counts": {}, "running": 0}
    limit = max(1, min(int(limit or 50), 200))
    # A list read is also the supervisor: turns past the timeout are cancelled
    # here, so a forgotten chat cannot hold a worker slot forever.
    _reap_turns()
    statuses = [part.strip() for part in str(status or "").split(",") if part.strip()]
    data = _chat_store().list_sessions(limit=limit, statuses=statuses)
    return {"db_exists": True, **data}


@router.get("/chats/{session_id}")
def get_chat(session_id: str) -> dict[str, Any]:
    session_id = _check_session_id(session_id)
    session = _chat_store().get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail=f"unknown chat session {session_id}")
    return {"db_exists": True, "session": session}


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
    model = _scrub(str(data.get("model") or os.environ.get("SUPERAGENT_DEFAULT_MODEL", "") or "").strip())
    provider = _scrub(str(data.get("provider") or os.environ.get("SUPERAGENT_DEFAULT_PROVIDER", "") or "").strip())
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

