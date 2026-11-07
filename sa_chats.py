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
from typing import Any, Iterator, Optional, Sequence

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
    finished_at       REAL,
    archived_at       REAL
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

CREATE TABLE IF NOT EXISTS gh_seen (
    key     TEXT PRIMARY KEY,
    seen_at REAL NOT NULL
);
"""

# A chat is `queued` until a worker slot is free, `running` for the length of a
# turn, then `idle` (waiting for the next user message) or `review` (work a
# human should look at). `failed` and `cancelled` are terminal for that turn.
# `observed` is a recorded request nobody launched yet: the GitHub watcher
# creates these in observe mode and a human promotes them.
QUEUED = "queued"
RUNNING = "running"
REVIEW = "review"
IDLE = "idle"
FAILED = "failed"
CANCELLED = "cancelled"
OBSERVED = "observed"
STATUSES = (QUEUED, RUNNING, REVIEW, IDLE, FAILED, CANCELLED, OBSERVED)

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
    # set_archived() is the normal door; the column stays writable so a
    # migration or a repair script can move a whole set in one call.
    "archived_at",
)

MAX_MESSAGE_CHARS = 20000
MAX_EVENT_CHARS = 2000
MAX_TITLE_CHARS = 80
# A renamed title may be longer than the 80-char derived one: it is a human's
# own label, and the rail truncates it for display. The column is TEXT.
MAX_RENAME_CHARS = 200

# Which archived rows list_sessions() / GET /chats return.
ARCHIVED_MODES = ("hide", "only", "all")

# Statuses that are doing something right now. Only these rows get the live
# extras in list_sessions(); a board of 50 historical chats stays slim.
LIVE_STATUSES = (QUEUED, RUNNING)
# The per-card activity feed: the last few events, clipped for a single line.
RECENT_EVENT_LIMIT = 3
RECENT_EVENT_CHARS = 160

# Search: the longest term the HTTP layer accepts, and the width of the
# one-line excerpt a rail row can show under its title.
SEARCH_QUERY_CHARS = 200
SEARCH_SNIPPET_CHARS = 160


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


def _elapsed_seconds(row: dict, now: float) -> Optional[int]:
    """Age of the current turn in whole seconds, or None for a finished one.

    A queued turn has no start of its own: the runner records ``started_at``
    only when the process actually spawns, so a value left over from an earlier
    turn must not be reported as the current wait.
    """
    if row.get("status") not in LIVE_STATUSES:
        return None
    started = row.get("started_at")
    if not started or row.get("status") == QUEUED:
        return 0
    return max(0, int(now - started))


def _escape_like(term: str) -> str:
    """Escape LIKE wildcards so a literal ``%`` cannot match every row.

    SQLite treats ``%`` and ``_`` as wildcards and has no default escape
    character; every query that uses this must add ``ESCAPE '\\'``.
    """
    return (
        str(term)
        .replace("\\", "\\\\")
        .replace("%", "\\%")
        .replace("_", "\\_")
    )


def _snippet(text: Any, needle: str, limit: int = SEARCH_SNIPPET_CHARS) -> str:
    """One readable line around the first hit, ``…`` on a clipped edge."""
    body = " ".join(str(text or "").split())
    if not body:
        return ""
    low = body.lower()
    index = low.find(str(needle or "").lower())
    if index < 0 or len(body) <= limit:
        return body[:limit]
    room = max(0, limit - len(needle))
    start = max(0, index - room // 2)
    end = min(len(body), start + limit)
    start = max(0, end - limit)
    lead = start > 0
    tail = end < len(body)
    # Each marker costs a character, so shrink the window by that much: the
    # excerpt (markers included) is what has to fit the rail's one line.
    excess = int(lead) + int(tail)
    if excess:
        end = max(start + 1, end - excess)
        tail = end < len(body)
    piece = body[start:end]
    if lead:
        piece = "…" + piece.lstrip()
    if tail:
        piece = piece.rstrip() + "…"
    return piece


def _board_counts(conn: sqlite3.Connection, mode: str) -> tuple[dict[str, int], int]:
    """Status counts and the archived total for one visibility mode.

    Counts describe the board, not the current row filter: a search or a
    status tab must not restate "3 review" as "1 review". They do obey
    ``archived`` visibility, so a hidden chat never inflates a number the
    rail cannot show, and ``archived_count`` always reports the real total.
    """
    count_clause = ""
    if mode == "hide":
        count_clause = " WHERE archived_at IS NULL"
    elif mode == "only":
        count_clause = " WHERE archived_at IS NOT NULL"
    counts = {
        row["status"]: row["n"]
        for row in conn.execute(
            "SELECT status, COUNT(*) AS n FROM chat_sessions" + count_clause
            + " GROUP BY status"
        )
    }
    archived_count = int(
        conn.execute(
            "SELECT COUNT(*) AS n FROM chat_sessions WHERE archived_at IS NOT NULL"
        ).fetchone()["n"]
    )
    return counts, archived_count


class ChatStore:
    """SQLite-backed chat sessions, messages and events."""

    def __init__(self, path: str | Path):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(SCHEMA)
            self._migrate(conn)

    @staticmethod
    def _migrate(conn: sqlite3.Connection) -> None:
        """Bring a pre-existing chat_sessions table up to this schema.

        ``CREATE TABLE IF NOT EXISTS`` leaves an old table untouched, so a
        column added after the first install (``archived_at``) has to be
        altered in place. The archived index is created here rather than in
        :data:`SCHEMA`: it names the new column, and ``executescript`` would
        fail on an old table before the ALTER had a chance to run. Idempotent,
        and every open calls it, so an upgrade is just "open the store once".
        """
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(chat_sessions)")}
        if "archived_at" not in columns:
            conn.execute("ALTER TABLE chat_sessions ADD COLUMN archived_at REAL")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS chat_sessions_archived"
            " ON chat_sessions(archived_at)"
        )

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

    def rename_session(self, session_id: str, title: str) -> Optional[dict]:
        """Give a session a human title (trimmed, 1..200 chars).

        Whitespace is collapsed so a pasted multi-line label still reads as one
        line in the rail. ``ValueError`` for a title the store will not keep
        (the endpoint turns that into a 400); ``None`` for an unknown id.
        """
        text = " ".join(str(title if title is not None else "").split())
        if not text:
            raise ValueError("title is required")
        if len(text) > MAX_RENAME_CHARS:
            raise ValueError(f"title is too long (max {MAX_RENAME_CHARS} chars)")
        return self.update_session(session_id, title=text)

    def set_archived(self, session_id: str, archived: bool = True) -> Optional[dict]:
        """Archive or restore a session; returns the row, or None if unknown.

        Archiving stamps ``archived_at`` once (a second archive keeps the
        original moment) and deliberately does **not** touch ``updated_at``:
        the row keeps its place in the newest-first list instead of jumping to
        the top merely because it was filed away. Restoring clears the stamp.
        """
        if not self.exists(session_id):
            return None
        if archived:
            with self._connect() as conn:
                conn.execute(
                    "UPDATE chat_sessions SET archived_at = COALESCE(archived_at, ?)"
                    " WHERE id = ?",
                    (time.time(), session_id),
                )
        else:
            with self._connect() as conn:
                conn.execute(
                    "UPDATE chat_sessions SET archived_at = NULL WHERE id = ?", (session_id,)
                )
        return self.get_session(session_id, message_limit=0)

    def delete_session(self, session_id: str) -> bool:
        """Delete a session and its transcript in one transaction.

        Messages and events go with the row; ``gh_seen`` does **not**, so a
        GitHub comment that already produced a session is not replayed into a
        new one after its session is deleted. Files and worktrees are never
        touched here — this is a database delete, nothing more.
        """
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT 1 FROM chat_sessions WHERE id = ?", (session_id,)
            ).fetchone()
            if row is None:
                return False
            conn.execute("DELETE FROM chat_messages WHERE session_id = ?", (session_id,))
            conn.execute("DELETE FROM chat_events WHERE session_id = ?", (session_id,))
            conn.execute("DELETE FROM chat_sessions WHERE id = ?", (session_id,))
        return True

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

    def list_sessions(
        self,
        limit: int = 50,
        statuses: Optional[Sequence[str]] = None,
        archived: str = "hide",
    ) -> dict:
        """Recent sessions plus status counts for the live session grid.

        Live rows (queued/running) carry ``elapsed_seconds`` and the last
        ``RECENT_EVENT_LIMIT`` events, so a grid of cards can draw a per-card
        feed from one poll instead of a detail request per card. Historical rows
        stay slim on purpose. ``statuses`` filters the rows only; ``counts``
        describe the whole board minus archived rows, so the header still tells
        the truth.

        ``archived`` picks which rows are visible: ``hide`` (the default) never
        returns an archived session, ``only`` returns just those, ``all``
        returns both. Counts follow that visibility — an archived chat must not
        inflate "3 review" while its card cannot be seen — and
        ``archived_count`` always reports how many archived rows exist, so the
        rail's "Show archived" toggle has a number even while hiding them.
        """
        limit = max(1, int(limit))
        mode = str(archived or "hide").strip().lower()
        if mode not in ARCHIVED_MODES:
            raise ValueError(
                f"archived must be one of {', '.join(ARCHIVED_MODES)} (got {archived!r})"
            )
        wanted = [str(status).strip() for status in (statuses or []) if str(status).strip()]
        clauses: list[str] = []
        params: list[Any] = []
        if wanted:
            clauses.append("s.status IN (%s)" % ", ".join("?" for _ in wanted))
            params.extend(wanted)
        if mode == "hide":
            clauses.append("s.archived_at IS NULL")
        elif mode == "only":
            clauses.append("s.archived_at IS NOT NULL")
        clause = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT s.*, (SELECT COUNT(*) FROM chat_messages m WHERE m.session_id = s.id)"
                " AS message_count FROM chat_sessions s" + clause +
                " ORDER BY s.updated_at DESC LIMIT ?",
                params,
            ).fetchall()
            counts, archived_count = _board_counts(conn, mode)
            sessions = [dict(row) for row in rows]
            live_ids = [row["id"] for row in sessions if row["status"] in LIVE_STATUSES]
            feeds = self._recent_events(conn, live_ids)
        now = time.time()
        for session in sessions:
            session["elapsed_seconds"] = _elapsed_seconds(session, now)
            if session["id"] in feeds:
                session["recent_events"] = feeds[session["id"]]
        return {
            "sessions": sessions,
            "counts": counts,
            "running": counts.get(RUNNING, 0),
            "archived_count": archived_count,
        }

    def search_sessions(
        self,
        query: str,
        limit: int = 50,
        statuses: Optional[Sequence[str]] = None,
        archived: str = "hide",
    ) -> dict:
        """Sessions whose title, prompt or any message contains ``query``.

        Same board semantics as :meth:`list_sessions` — archived visibility,
        the optional status filter, newest-first order, and counts that
        describe the board rather than the hit list — so the rail can swap one
        call for the other without its header numbers changing meaning.

        Each row also carries ``match_field`` (``title`` / ``prompt`` /
        ``message``) and ``match_snippet``: why the chat matched, in one line.
        A blank query is exactly :meth:`list_sessions`, with no ``match_*``
        keys at all.

        Matching is SQLite ``LIKE``, which folds case for ASCII only, and the
        term is escaped, so searching ``100%`` looks for that literal.
        """
        term = str(query or "").strip()
        if not term:
            return self.list_sessions(limit=limit, statuses=statuses, archived=archived)
        limit = max(1, int(limit))
        mode = str(archived or "hide").strip().lower()
        if mode not in ARCHIVED_MODES:
            raise ValueError(
                f"archived must be one of {', '.join(ARCHIVED_MODES)} (got {archived!r})"
            )
        wanted = [str(status).strip() for status in (statuses or []) if str(status).strip()]
        pattern = "%" + _escape_like(term) + "%"
        clauses: list[str] = [
            "(s.title LIKE ? ESCAPE '\\' OR s.prompt LIKE ? ESCAPE '\\'"
            " OR EXISTS (SELECT 1 FROM chat_messages m WHERE m.session_id = s.id"
            " AND m.content LIKE ? ESCAPE '\\'))"
        ]
        params: list[Any] = [pattern, pattern, pattern]
        if wanted:
            clauses.append("s.status IN (%s)" % ", ".join("?" for _ in wanted))
            params.extend(wanted)
        if mode == "hide":
            clauses.append("s.archived_at IS NULL")
        elif mode == "only":
            clauses.append("s.archived_at IS NOT NULL")
        clause = " WHERE " + " AND ".join(clauses)
        params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT s.*, (SELECT COUNT(*) FROM chat_messages m WHERE m.session_id = s.id)"
                " AS message_count FROM chat_sessions s" + clause +
                " ORDER BY s.updated_at DESC LIMIT ?",
                params,
            ).fetchall()
            sessions = [dict(row) for row in rows]
            hits = self._matching_messages(conn, [row["id"] for row in sessions], pattern)
            counts, archived_count = _board_counts(conn, mode)
            live_ids = [row["id"] for row in sessions if row["status"] in LIVE_STATUSES]
            feeds = self._recent_events(conn, live_ids)
        needle = term.lower()
        now = time.time()
        for session in sessions:
            title = str(session.get("title") or "")
            prompt = str(session.get("prompt") or "")
            if needle in title.lower():
                field, text = "title", title
            elif needle in prompt.lower():
                field, text = "prompt", prompt
            else:
                field, text = "message", hits.get(session["id"], "")
            session["match_field"] = field
            session["match_snippet"] = _snippet(text, needle)
            session["elapsed_seconds"] = _elapsed_seconds(session, now)
            if session["id"] in feeds:
                session["recent_events"] = feeds[session["id"]]
        return {
            "sessions": sessions,
            "counts": counts,
            "running": counts.get(RUNNING, 0),
            "archived_count": archived_count,
        }

    def _matching_messages(
        self,
        conn: sqlite3.Connection,
        session_ids: Sequence[str],
        pattern: str,
    ) -> dict[str, str]:
        """Newest message that matches ``pattern`` for each session, one query."""
        ids = [str(session_id) for session_id in session_ids if session_id]
        if not ids:
            return {}
        rows = conn.execute(
            "SELECT session_id, content FROM chat_messages WHERE session_id IN (%s)"
            " AND content LIKE ? ESCAPE '\\' ORDER BY id DESC" % ", ".join("?" for _ in ids),
            [*ids, pattern],
        ).fetchall()
        hits: dict[str, str] = {}
        for row in rows:
            hits.setdefault(row["session_id"], row["content"] or "")
        return hits

    def _recent_events(
        self,
        conn: sqlite3.Connection,
        session_ids: Sequence[str],
        per_session: int = RECENT_EVENT_LIMIT,
    ) -> dict[str, list[dict]]:
        """Newest ``per_session`` events for every id, in one query.

        Grouping happens here, not in SQL, so the query stays a plain index
        scan: the results arrive newest-first and each bucket stops at
        ``per_session``.
        """
        ids = [str(session_id) for session_id in session_ids if session_id]
        if not ids:
            return {}
        rows = conn.execute(
            "SELECT session_id, kind, detail, created_at FROM chat_events"
            " WHERE session_id IN (%s) ORDER BY id DESC" % ", ".join("?" for _ in ids),
            ids,
        ).fetchall()
        feeds: dict[str, list[dict]] = {session_id: [] for session_id in ids}
        for row in rows:
            bucket = feeds[row["session_id"]]
            if len(bucket) >= per_session:
                continue
            bucket.append(
                {
                    "kind": row["kind"],
                    "detail": _clip(row["detail"], RECENT_EVENT_CHARS),
                    "created_at": row["created_at"],
                }
            )
        return feeds

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

    def last_user_message(self, session_id: str) -> Optional[dict]:
        """The newest user message, or None — exactly what a retry would resend.

        A retry deliberately reuses the stored text instead of appending a
        duplicate message, so this is a plain lookup rather than a scan of the
        capped transcript (a 200-message window could miss the prompt).
        """
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM chat_messages WHERE session_id = ? AND role = 'user'"
                " ORDER BY id DESC LIMIT 1",
                (session_id,),
            ).fetchone()
        return dict(row) if row is not None else None

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

    # -- state -----------------------------------------------------------
    #
    # Small key/value pairs the watcher keeps between polls (and restarts).
    # The `state` table already exists for the ticket/chat sequences.

    def get_state(self, key: str, default: str = "") -> str:
        with self._connect() as conn:
            row = conn.execute("SELECT value FROM state WHERE key = ?", (str(key),)).fetchone()
        return str(row["value"]) if row is not None else default

    def set_state(self, key: str, value: Any) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO state(key, value) VALUES(?, ?)"
                " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (str(key), str(value)),
            )

    # -- dedupe ----------------------------------------------------------
    #
    # The GitHub watcher has to remember which comments, PR heads and
    # baselines it already handled. The rows live in SQLite, not in memory, so
    # a restart does not replay every comment on a busy pull request.

    def mark_seen(self, key: str) -> None:
        """Record `key` as handled. Idempotent; empty keys are ignored."""
        key = str(key or "").strip()
        if not key:
            return
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO gh_seen(key, seen_at) VALUES(?, ?)"
                " ON CONFLICT(key) DO UPDATE SET seen_at = excluded.seen_at",
                (key, time.time()),
            )

    def is_seen(self, key: str) -> bool:
        key = str(key or "").strip()
        if not key:
            return False
        with self._connect() as conn:
            row = conn.execute("SELECT 1 FROM gh_seen WHERE key = ?", (key,)).fetchone()
        return row is not None

    def clear_seen(self, prefix: str = "") -> int:
        """Delete dedupe rows starting with `prefix` (all of them when empty)."""
        with self._connect() as conn:
            if prefix:
                cursor = conn.execute(
                    "DELETE FROM gh_seen WHERE substr(key, 1, ?) = ?",
                    (len(prefix), prefix),
                )
            else:
                cursor = conn.execute("DELETE FROM gh_seen")
        return int(cursor.rowcount or 0)
