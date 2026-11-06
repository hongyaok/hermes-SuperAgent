"""SQLite storage for tickets, events and small key/value state.

One file, one table per concept. A connection is opened per operation so the
plugin can be called from an agent turn, a gateway request or a CLI process
without sharing a socket between threads.
"""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Optional

SCHEMA = """
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
CREATE INDEX IF NOT EXISTS tickets_status ON tickets(status, created_at);

CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id  TEXT,
    kind       TEXT NOT NULL,
    detail     TEXT,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS events_ticket ON events(ticket_id, id);

CREATE TABLE IF NOT EXISTS state (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS seen (
    source_key TEXT PRIMARY KEY,
    seen_at    REAL NOT NULL
);
"""


class Store:
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

    # -- tickets ---------------------------------------------------------

    def new_ticket_id(self) -> str:
        """Return the next readable id (T-0001, T-0002, ...)."""
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT value FROM state WHERE key = 'ticket_seq'").fetchone()
            seq = int(row["value"]) if row else 0
            seq += 1
            conn.execute(
                "INSERT INTO state(key, value) VALUES('ticket_seq', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (str(seq),),
            )
        return f"T-{seq:04d}"

    def create_ticket(self, **fields: Any) -> dict:
        now = time.time()
        ticket = {
            "id": fields.get("id") or self.new_ticket_id(),
            "source": fields.get("source", "local"),
            "source_ref": fields.get("source_ref"),
            "repo": fields.get("repo"),
            "issue_number": fields.get("issue_number"),
            "requester": fields.get("requester"),
            "request": fields["request"],
            "status": fields.get("status", "queued"),
            "verdict": fields.get("verdict"),
            "reason": fields.get("reason"),
            "mode": fields.get("mode"),
            "handle": None,
            "result": None,
            "error": None,
            "created_at": now,
            "updated_at": now,
            "started_at": None,
        }
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO tickets
                   (id, source, source_ref, repo, issue_number, requester, request,
                    status, verdict, reason, mode, handle, result, error,
                    created_at, updated_at, started_at)
                   VALUES (:id, :source, :source_ref, :repo, :issue_number, :requester, :request,
                           :status, :verdict, :reason, :mode, :handle, :result, :error,
                           :created_at, :updated_at, :started_at)""",
                ticket,
            )
        self.add_event(ticket["id"], "created", f"{ticket['source']} request -> {ticket['status']}")
        return self.get(ticket["id"])  # type: ignore[return-value]

    def update(self, ticket_id: str, **fields: Any) -> dict:
        if not fields:
            return self.get(ticket_id)  # type: ignore[return-value]
        fields["updated_at"] = time.time()
        assignments = ", ".join(f"{key} = :{key}" for key in fields)
        fields["id"] = ticket_id
        with self._connect() as conn:
            conn.execute(f"UPDATE tickets SET {assignments} WHERE id = :id", fields)
        return self.get(ticket_id)  # type: ignore[return-value]

    def get(self, ticket_id: str) -> Optional[dict]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM tickets WHERE id = ?", (ticket_id,)).fetchone()
        return dict(row) if row else None

    def list(self, status: Optional[str] = None, limit: int = 50) -> list[dict]:
        query = "SELECT * FROM tickets"
        params: list[Any] = []
        if status:
            placeholders = ", ".join("?" for _ in status.split(","))
            query += f" WHERE status IN ({placeholders})"
            params.extend(status.split(","))
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    def count_by_status(self) -> dict[str, int]:
        with self._connect() as conn:
            rows = conn.execute("SELECT status, COUNT(*) AS n FROM tickets GROUP BY status").fetchall()
        return {row["status"]: row["n"] for row in rows}

    # -- events ----------------------------------------------------------

    def add_event(self, ticket_id: Optional[str], kind: str, detail: str = "") -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO events(ticket_id, kind, detail, created_at) VALUES (?, ?, ?, ?)",
                (ticket_id, kind, detail, time.time()),
            )

    def events(self, ticket_id: str, limit: int = 20) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM events WHERE ticket_id = ? ORDER BY id DESC LIMIT ?",
                (ticket_id, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    # -- key/value state -------------------------------------------------

    def get_state(self, key: str, default: Any = None) -> Any:
        with self._connect() as conn:
            row = conn.execute("SELECT value FROM state WHERE key = ?", (key,)).fetchone()
        if not row:
            return default
        try:
            return json.loads(row["value"])
        except json.JSONDecodeError:
            return row["value"]

    def set_state(self, key: str, value: Any) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO state(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, json.dumps(value)),
            )

    # -- source dedupe ---------------------------------------------------

    def is_seen(self, source_key: str) -> bool:
        with self._connect() as conn:
            row = conn.execute("SELECT 1 FROM seen WHERE source_key = ?", (source_key,)).fetchone()
        return row is not None

    def mark_seen(self, source_key: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO seen(source_key, seen_at) VALUES (?, ?)",
                (source_key, time.time()),
            )
