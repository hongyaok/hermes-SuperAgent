"""Superagent dashboard backend, mounted at /api/plugins/superagent/.

Read-only views over the ticket SQLite database for the desktop panel.
All state changes go through the agent-turn tools (superagent_ask / tick /
run), never through this backend — it only shows what needs a human.

Imported by the gateway on its own, outside the plugin package, so this
module is self-contained: stdlib sqlite3 only, no relative imports.
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException

router = APIRouter()

VERSION = "0.1.0"

_TICKET_COLUMNS = (
    "id", "source", "source_ref", "repo", "issue_number", "requester",
    "request", "status", "verdict", "reason", "mode", "result", "error",
    "created_at", "updated_at", "started_at",
)


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


def _brief(row: sqlite3.Row) -> dict[str, Any]:
    request = (row["request"] or "").strip().replace("\n", " ")
    return {
        "id": row["id"],
        "status": row["status"],
        "source": row["source"],
        "requester": row["requester"],
        "request": request[:160] + ("…" if len(request) > 160 else ""),
        "reason": row["reason"],
        "error": row["error"],
        "updated_at": row["updated_at"],
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
    ticket_id = (ticket_id or "").strip()
    if not ticket_id or "/" in ticket_id or "\\" in ticket_id or ticket_id in (".", ".."):
        raise HTTPException(status_code=400, detail="invalid ticket id")
    with _connect_ro(path) as conn:
        row = conn.execute("SELECT * FROM tickets WHERE id = ?", (ticket_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail=f"unknown ticket {ticket_id}")
        ticket = {col: row[col] for col in _TICKET_COLUMNS if col in row.keys()}
        events = [dict(r) for r in conn.execute(
            "SELECT * FROM events WHERE ticket_id = ? ORDER BY id DESC LIMIT 20", (ticket_id,))]
    ticket["events"] = events
    return ticket
