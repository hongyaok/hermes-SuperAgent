"""Chat sessions: durable conversations for the Superagent desktop chat.

The ticket tables in ``sa_store`` describe work the router decided to do. Chat
sessions describe conversations: a user message starts (or resumes) one
headless Hermes turn, the assistant's replies and tool calls are stored next to
it, and every session row carries enough state for a live multi-session grid
(status, model, provider, tokens, pid, last event).

Same SQLite file as ``sa_store.Store`` so one plugin install keeps one
database. The tables here are additive; nothing in the ticket schema changes.
"""

from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS chat_sessions (
    id                TEXT PRIMARY KEY,
    title             TEXT,
    prompt            TEXT NOT NULL,
    status            TEXT NOT NULL,
    model             TEXT,
    provider          TEXT,
    repo              TEXT,
    hermes_session_id TEXT,
    source            TEXT,
    github_repo       TEXT,
    pr_number         INTEGER,
    pr_url            TEXT,
    pid               INTEGER,
    tokens            INTEGER,
    duration_ms       INTEGER,
    error             TEXT,
    last_event        TEXT,
    created_at        REAL NOT NULL,
    updated_at        REAL NOT NULL,
    started_at        REAL,
    finished_at       REAL
);
CREATE INDEX IF NOT EXISTS chat_sessions_updated ON chat_sessions(updated_at);
CREATE INDEX IF NOT EXISTS chat_sessions_status ON chat_sessions(status, updated_at);

CREATE TABLE IF NOT EXISTS chat_messages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    role       TEXT NOT NULL,
    content    TEXT,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS chat_messages_session ON chat_messages(session_id, id);

CREATE TABLE IF NOT EXISTS chat_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    kind       TEXT NOT NULL,
    detail     TEXT,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS chat_events_session ON chat_events(session_id, id);
CREATE INDEX IF NOT EXISTS chat_events_created ON chat_events(session_id, created_at);

CREATE TABLE IF NOT EXISTS state (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

# A chat is `queued` until a worker slot is free, `running` for the length of a
# turn, then `idle` (waiting for the next user message) or `review` (work a
# human should look at). `failed` and `cancelled` are terminal for that turn.
QUEUED = "queued"
RUNNING = "running"
REVIEW = "review"
IDLE = "idle"
FAILED = "failed"
CANCELLED = "cancelled"
STATUSES = (QUEUED, RUNNING, REVIEW, IDLE, FAILED, CANCELLED)

# Columns a caller may change through update_session(). Everything else
# (id, created_at, ...) is owned by the store.
UPDATABLE = (
    "title",
    "prompt",
    "status",
    "model",
    "provider",
    "repo",
    "hermes_session_id",
    "source",
    "github_repo",
    "pr_number",
    "pr_url",
    "pid",
    "tokens",
    "duration_ms",
    "error",
    "last_event",
    "started_at",
    "finished_at",
)

MAX_MESSAGE_CHARS = 20000
MAX_EVENT_CHARS = 2000
MAX_TITLE_CHARS = 80


def derive_title(prompt: str, limit: int = MAX_TITLE_CHARS) -> str:
    """First non-empty line of the prompt, clipped to ``limit`` characters."""
    for line in (prompt or "").splitlines():
        text = " ".join(line.split())
        if not text:
            continue
        if len(text) <= limit:
            return text
        return text[: max(1, limit - 1)] + "…"
    return ""


def _clip(text: Any, limit: int) -> str:
    value = "" if text is None else str(text)
    return value if len(value) <= limit else value[:limit]


class ChatStore:
    """SQLite-backed chat sessions, messages and events."""

    def __init__(self, path: str | Path):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(SCHEMA)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=15)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # -- sessions --------------------------------------------------------

    def new_session_id(self) -> str:
        """Return the next readable id (S-0001, S-0002, ...)."""
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT value FROM state WHERE key = 'chat_seq'").fetchone()
            seq = int(row["value"]) if row else 0
            seq += 1
            conn.execute(
                "INSERT INTO state(key, value) VALUES('chat_seq', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (str(seq),),
            )
        return f"S-{seq:04d}"

    def create_session(
        self,
        prompt: str,
        title: str = "",
        model: str = "",
        provider: str = "",
        repo: str = "",
        source: str = "desktop",
        github_repo: Optional[str] = None,
        pr_number: Optional[int] = None,
        pr_url: Optional[str] = None,
        status: str = QUEUED,
    ) -> dict:
        now = time.time()
        session_id = self.new_session_id()
        row = {
            "id": session_id,
            "title": _clip(title.strip() or derive_title(prompt), MAX_TITLE_CHARS),
            "prompt": _clip(prompt, MAX_MESSAGE_CHARS),
            "status": status,
            "model": model or "",
            "provider": provider or "",
            "repo": repo or "",
            "hermes_session_id": None,
            "source": source or "desktop",
            "github_repo": github_repo,
            "pr_number": pr_number,
            "pr_url": pr_url,
            "pid": None,
            "tokens": None,
            "duration_ms": None,
            "error": None,
            "last_event": None,
            "created_at": now,
            "updated_at": now,
            "started_at": None,
            "finished_at": None,
        }
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO chat_sessions
                   (id, title, prompt, status, model, provider, repo, hermes_session_id,
                    source, github_repo, pr_number, pr_url, pid, tokens, duration_ms, error,
                    last_event, created_at, updated_at, started_at, finished_at)
                   VALUES (:id, :title, :prompt, :status, :model, :provider, :repo, :hermes_session_id,
                           :source, :github_repo, :pr_number, :pr_url, :pid, :tokens, :duration_ms, :error,
                           :last_event, :created_at, :updated_at, :started_at, :finished_at)""",
                row,
            )
        self.add_event(session_id, "created", f"{row['source']} chat -> {status}")
        return self.get_session(session_id, message_limit=0)  # type: ignore[return-value]

    def update_session(self, session_id: str, **fields: Any) -> Optional[dict]:
        unknown = [key for key in fields if key not in UPDATABLE]
        if unknown:
            raise ValueError(f"cannot update chat session fields: {', '.join(sorted(unknown))}")
        if fields:
            payload = {key: fields[key] for key in fields}
            payload["updated_at"] = time.time()
            assignments = ", ".join(f"{key} = :{key}" for key in payload)
            payload["id"] = session_id
            with self._connect() as conn:
                conn.execute(f"UPDATE chat_sessions SET {assignments} WHERE id = :id", payload)
        return self.get_session(session_id, message_limit=0)

    def get_session(
        self, session_id: str, message_limit: int = 200, event_limit: int = 50
    ) -> Optional[dict]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM chat_sessions WHERE id = ?", (session_id,)).fetchone()
        if row is None:
            return None
        session = dict(row)
        session["messages"] = self.get_messages(session_id, limit=message_limit) if message_limit else []
        session["events"] = self.get_events(session_id, limit=event_limit)
        return session

    def list_sessions(self, limit: int = 50) -> dict:
        """Recent sessions plus status counts for the live session grid."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT s.*, (SELECT COUNT(*) FROM chat_messages m WHERE m.session_id = s.id)"
                " AS message_count FROM chat_sessions s ORDER BY s.updated_at DESC LIMIT ?",
                (max(1, int(limit)),),
            ).fetchall()
            counts = {
                row["status"]: row["n"]
                for row in conn.execute(
                    "SELECT status, COUNT(*) AS n FROM chat_sessions GROUP BY status"
                )
            }
        return {
            "sessions": [dict(row) for row in rows],
            "counts": counts,
            "running": counts.get(RUNNING, 0),
        }

    # -- messages --------------------------------------------------------

    def add_message(self, session_id: str, role: str, content: str) -> dict:
        now = time.time()
        message = {
            "session_id": session_id,
            "role": role or "assistant",
            "content": _clip(content, MAX_MESSAGE_CHARS),
            "created_at": now,
        }
        with self._connect() as conn:
            cursor = conn.execute(
                "INSERT INTO chat_messages(session_id, role, content, created_at)"
                " VALUES (:session_id, :role, :content, :created_at)",
                message,
            )
            message["id"] = cursor.lastrowid
            conn.execute(
                "UPDATE chat_sessions SET updated_at = ? WHERE id = ?", (now, session_id)
            )
        return message

    def get_messages(self, session_id: str, limit: int = 200) -> list[dict]:
        """Messages in chronological order (the newest ``limit`` of them)."""
        limit = max(1, int(limit))
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM chat_messages WHERE session_id = ? ORDER BY id DESC LIMIT ?",
                (session_id, limit),
            ).fetchall()
        return [dict(row) for row in reversed(rows)]

    # -- events ----------------------------------------------------------

    def add_event(self, session_id: str, kind: str, detail: str = "") -> dict:
        now = time.time()
        kind = _clip(kind or "event", 80)
        detail = _clip(detail, MAX_EVENT_CHARS)
        with self._connect() as conn:
            cursor = conn.execute(
                "INSERT INTO chat_events(session_id, kind, detail, created_at) VALUES (?, ?, ?, ?)",
                (session_id, kind, detail, now),
            )
            conn.execute(
                "UPDATE chat_sessions SET updated_at = ?, last_event = ? WHERE id = ?",
                (now, f"{kind}: {detail}" if detail else kind, session_id),
            )
            event_id = cursor.lastrowid
        return {"id": event_id, "session_id": session_id, "kind": kind, "detail": detail, "created_at": now}

    def get_events(self, session_id: str, limit: int = 50) -> list[dict]:
        """Events in chronological order (the newest ``limit`` of them)."""
        limit = max(1, int(limit))
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM chat_events WHERE session_id = ? ORDER BY id DESC LIMIT ?",
                (session_id, limit),
            ).fetchall()
        return [dict(row) for row in reversed(rows)]

    # -- helpers ---------------------------------------------------------

    def count_by_status(self) -> dict[str, int]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT status, COUNT(*) AS n FROM chat_sessions GROUP BY status"
            ).fetchall()
        return {row["status"]: row["n"] for row in rows}

    def exists(self, session_id: str) -> bool:
        with self._connect() as conn:
            row = conn.execute("SELECT 1 FROM chat_sessions WHERE id = ?", (session_id,)).fetchone()
        return row is not None
