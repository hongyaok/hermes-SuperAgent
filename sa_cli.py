"""`hermes superagent ...` and the text views shared with `/superagent`."""

from __future__ import annotations

import argparse
import json
import os
import threading
import time
from math import isfinite
from pathlib import Path
from typing import Any

try:
    from . import sa_chats, sa_diff, sa_engine, sa_github, sa_runner, sa_settings, sa_watcher
except ImportError:  # top-level import (root __init__ loaded as `__init__` by pytest)
    import sa_chats  # type: ignore[no-redef]
    import sa_diff  # type: ignore[no-redef]
    import sa_engine  # type: ignore[no-redef]
    import sa_github  # type: ignore[no-redef]
    import sa_runner  # type: ignore[no-redef]
    import sa_settings  # type: ignore[no-redef]
    import sa_watcher  # type: ignore[no-redef]

STATUS_ICONS = {
    "observed": "·",
    "queued": "…",
    "running": "▶",
    "review": "◆",
    "blocked": "!",
    "failed": "×",
    "done": "✓",
    "ignored": "–",
}

# Mirrors dashboard/plugin_api.py (the chat routes are the reference
# implementation this CLI drives): same prompt cap, same preset semantics.
MAX_PROMPT_CHARS = 8000
# Same comment cap the dashboard enforces; GitHub truncates silently past 65 536.
MAX_COMMENT_CHARS = 20000

# One Runner per database path, like the dashboard singleton: a CLI session
# and an in-process dashboard share worker slots instead of racing.
_RUNNERS: dict[str, Any] = {}
_RUNNERS_LOCK = threading.Lock()


USAGE = (
    "usage: hermes superagent "
    "<status|tick|ask <request>|run <ticket-id>|show <ticket-id|session-id>|"
    "chat <prompt>|sessions|stop <session-id>|diff <session-id>|"
    "comment <session-id> [--body TEXT]|models|settings|"
    "github <status|test|connect|disconnect|prs|poll|work <number>>|doctor> [--json]"
)

GITHUB_ACTIONS = ("status", "test", "connect", "disconnect", "prs", "poll", "work")
GITHUB_WATCH_MODES = ("observe", "auto")
GITHUB_USAGE = (
    "usage: hermes superagent github "
    "<status|test|connect|disconnect|prs|poll|work <number>> "
    "[--repo owner/name] [--token TOKEN] [--allowed alice,bob] "
    "[--watch-mode observe|auto] [--request \"...\"] [--json]"
)


def render_status(data: dict) -> str:
    mode = data.get("mode") if isinstance(data, dict) else None
    running = data.get("running", 0) if isinstance(data, dict) else 0
    queued = data.get("queued", 0) if isinstance(data, dict) else 0
    lines = [
        f"Superagent   mode={mode}   running={running}   queued={queued}"
    ]
    attention = data.get("attention") or [] if isinstance(data, dict) else []
    lines.append("")
    if attention:
        lines.append("Needs you:")
        for ticket in attention:
            if not isinstance(ticket, dict):
                continue
            icon = STATUS_ICONS.get(str(ticket.get("status")), "?")
            tid = ticket.get("id", "?")
            status = str(ticket.get("status", "?"))
            requester = ticket.get("requester") or "-"
            request = str(ticket.get("request") or "")
            lines.append(f"  {icon} {tid}  {status:<9} {requester:<12} {request}")
            if ticket.get("reason"):
                lines.append(f"      {ticket['reason']}")
            if ticket.get("error"):
                lines.append(f"      error: {ticket['error']}")
    else:
        lines.append("Nothing needs you.")

    tickets = data.get("tickets") or [] if isinstance(data, dict) else []
    if tickets:
        lines.append("")
        lines.append("Recent tickets:")
        for ticket in tickets[:12]:
            if not isinstance(ticket, dict):
                continue
            icon = STATUS_ICONS.get(str(ticket.get("status")), "?")
            tid = ticket.get("id", "?")
            status = str(ticket.get("status", "?"))
            request = str(ticket.get("request") or "")
            lines.append(f"  {icon} {tid}  {status:<9} {request[:90]}")
    return "\n".join(lines)


def render_ticket(ticket: dict) -> str:
    if not isinstance(ticket, dict):
        return str(ticket)
    tid = ticket.get("id", "?")
    status = ticket.get("status", "?")
    lines = [
        f"{tid}  {status}",
        f"  request:   {ticket.get('request') or ''}",
        f"  requester: {ticket.get('requester') or '-'} via {ticket.get('source') or '-'}",
        f"  decision:  {ticket.get('verdict') or '-'} ({ticket.get('reason') or '-'})",
    ]
    if ticket.get("result"):
        lines.append(f"  result:    {str(ticket['result'])[:800]}")
    if ticket.get("error"):
        lines.append(f"  error:     {ticket['error']}")
    events = ticket.get("events") or []
    if events:
        lines.append("  history:")
        for event in reversed(events):
            if not isinstance(event, dict):
                continue
            lines.append(f"    - {event.get('kind', '?')}: {(event.get('detail') or '')[:160]}")
    return "\n".join(lines)


def _env(name: str) -> str:
    return (os.environ.get(name) or "").strip()


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = _env(name)
    if not raw:
        return default
    return raw.lower() in ("1", "true", "yes", "on")


def _env_list(name: str, default: list[str]) -> list[str]:
    raw = os.environ.get(name)
    if not raw:
        return list(default)
    items = [part.strip() for part in raw.split(",") if part.strip()]
    return items or list(default)


def _config(ctx: Any, key: str, default: Any) -> Any:
    try:
        return ctx.get_config(key, default=default)
    except Exception:
        return default


def models_payload(ctx: Any) -> dict:
    """Resolved default model/provider plus presets (same keys as GET /models)."""
    model = _env("SUPERAGENT_DEFAULT_MODEL") or str(_config(ctx, "default_model", "") or "").strip()
    provider = _env("SUPERAGENT_DEFAULT_PROVIDER") or str(
        _config(ctx, "default_provider", "") or ""
    ).strip()
    presets: list[dict[str, str]] = []
    env_presets = os.environ.get("SUPERAGENT_MODEL_PRESETS")
    if env_presets:
        presets = sa_settings.normalize_presets(env_presets)
    if not presets:
        configured = _config(ctx, "model_presets", [])
        presets = sa_settings.normalize_presets(configured) if configured else []
    if not presets:
        # The desktop settings panel writes settings.json; `models` and
        # `chat --preset` must see the same catalog the panel shows.
        try:
            presets = sa_settings.normalize_presets(
                sa_settings.load_settings().get("model_presets")
            )
        except Exception:
            presets = []
    # No presets configured is a valid answer: printing a hardcoded list would
    # advertise models this machine may have no credentials for. The flat list
    # still names the configured default so `models` is never empty for nothing.
    return {
        "default_model": model,
        "default_provider": provider,
        "models": list(dict.fromkeys(
            [preset["model"] for preset in presets] + ([model] if model else [])
        )),
        "presets": presets,
    }


def preset_label(preset: dict) -> str:
    """`provider: model` for one preset (or just the model)."""
    name = str((preset or {}).get("model") or "")
    provider = str((preset or {}).get("provider") or "")
    return f"{provider}: {name}" if provider and name else (name or provider)


def find_preset(presets: list[dict], name: str) -> dict | None:
    for preset in presets:
        if str((preset or {}).get("name") or "") == name:
            return preset
    return None


def render_models(data: dict) -> str:
    lines = [
        f"default model:    {data.get('default_model') or '(unset)'}",
        f"default provider: {data.get('default_provider') or '(unset)'}",
        "",
        "presets:",
    ]
    presets = data.get("presets") or []
    if presets:
        for preset in presets:
            name = str(preset.get("name") or "")
            label = preset_label(preset)
            lines.append(f"  - {name} -> {label}")
    else:
        lines.append("  (none configured; use the model picker or set model_presets)")
    return "\n".join(lines)


def render_settings(data: dict) -> str:
    """Human view of the merged settings; the token is only ever `set`/`unset`."""
    def line(label: str, value: Any, empty: str = "(unset)") -> str:
        if isinstance(value, (list, tuple)):
            text = ", ".join(str(item) for item in value) if value else "(none)"
        else:
            text = "" if value is None else str(value).strip()
            text = text or empty
        return f"  {label:<20} {text}"

    presets = data.get("model_presets") or []
    lines = ["Superagent settings"]
    for key, label in (
        ("mode", "mode"),
        ("repo_path", "repo path"),
        ("default_model", "default model"),
        ("default_provider", "default provider"),
        ("max_workers", "max workers"),
        ("worker_yolo", "worker yolo"),
        ("chat_max_turns", "chat max turns"),
        ("github_repo", "GitHub repo"),
        ("github_agent_login", "GitHub agent login"),
        ("github_allowed_users", "GitHub allowed"),
        ("github_watch_mode", "GitHub watch mode"),
        ("github_watch_new_prs", "GitHub new PRs"),
        ("github_poll_seconds", "GitHub poll (s)"),
    ):
        lines.append(line(label, data.get(key)))
    lines.append(line("GitHub token", "set (value never shown)" if data.get("github_token_set") else "not set"))
    if presets:
        lines.append("  model presets:")
        for preset in presets:
            name = str(preset.get("name") or "")
            label = preset_label(preset)
            lines.append(f"    - {name} -> {label}")
    else:
        lines.append("  model presets:      (none configured)")
    return "\n".join(lines)


def run_settings(want_json: bool) -> int:
    """`hermes superagent settings`: print merged settings, never the token."""
    try:
        data = sa_settings.public_settings()
    except Exception as exc:
        print(f"error: settings failed: {exc}")
        return 1
    if want_json:
        print(json.dumps(data, indent=2, default=str))
    else:
        print(render_settings(data))
    return 0


# -- GitHub CLI ------------------------------------------------------------


def _format_age(seconds: Any) -> str:
    """Compact age from a number of seconds: 'now' / '4m' / '3h' / '2d'."""
    try:
        delta = float(seconds)
    except (TypeError, ValueError):
        return ""
    if not isfinite(delta):
        return ""
    if delta < 45:
        return "now"
    if delta < 3600:
        return f"{max(1, round(delta / 60))}m"
    if delta < 86400:
        return f"{round(delta / 3600)}h"
    return f"{round(delta / 86400)}d"


def _iso_age(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        from datetime import datetime, timezone

        stamp = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        return _format_age(time.time() - stamp.timestamp())
    except (TypeError, ValueError):
        return ""


def _clean_allowed(value: Any) -> list[str]:
    """Strip, drop empties/@handles and dedupe case-insensitively."""
    if isinstance(value, str):
        parts: list[Any] = value.split(",")
    elif isinstance(value, (list, tuple, set)):
        parts = list(value)
    else:
        raise ValueError("allowed users must be a list or a comma-separated string")
    cleaned: list[str] = []
    seen: set[str] = set()
    for part in parts:
        name = str(part if part is not None else "").strip()
        if name.startswith("@"):
            name = name[1:].strip()
        if not name or name.lower() in seen:
            continue
        seen.add(name.lower())
        cleaned.append(name)
    return cleaned


def _mask(text: Any, token: str = "") -> str:
    value = "" if text is None else str(text)
    return value.replace(token, "[redacted]") if token else value


def render_github_status(data: dict) -> str:
    if not isinstance(data, dict):
        return str(data)
    repo = str(data.get("repo") or "").strip()
    lines = [f"GitHub   {repo or 'not connected'}"]
    if not data.get("configured"):
        lines.append("  status:    not connected (a repository and token are required)")
        lines.append(
            "  connect:   hermes superagent github connect --repo owner/name"
            " --token <token> --allowed you"
        )
        if data.get("last_error"):
            lines.append(f"  error:     {data['last_error']}")
        return "\n".join(lines)
    open_prs = data.get("open_prs") or []
    allowed = data.get("allowed_users") or []
    lines += [
        f"  login:     {data.get('login') or '-'}",
        f"  watching:  {'yes' if data.get('watching') else 'no'}"
        f"   mode={data.get('watch_mode') or 'observe'}"
        f"   poll={data.get('poll_seconds') or '-'}s",
        f"  open PRs:  {len(open_prs)}   sessions created: {data.get('sessions_created') or 0}",
        f"  allowed:   {', '.join(str(name) for name in allowed) if allowed else '(nobody)'}",
    ]
    if data.get("last_poll"):
        lines.append(f"  last poll: {_stamp(data['last_poll'])}")
    if data.get("last_error"):
        lines.append(f"  error:     {data['last_error']}")
    return "\n".join(lines)


def render_github_account(data: dict) -> str:
    """Masked result of `github test` / `github connect` (never the token)."""
    if not isinstance(data, dict):
        return str(data)
    repo = str(data.get("repo") or data.get("full_name") or "")
    privacy = "private" if data.get("private") else "public"
    lines = [
        f"GitHub   {repo}   connected as {data.get('login') or '-'}",
        f"  repo:      {data.get('full_name') or repo} ({privacy},"
        f" default {data.get('default_branch') or '-'})",
        f"  open PRs:  {data.get('open_pr_count', 0)}",
    ]
    if data.get("token_set"):
        lines.append("  token:     saved (value never shown)")
    if data.get("watch_mode"):
        lines.append(f"  mode:      {data['watch_mode']}   poll={data.get('poll_seconds') or '-'}s")
    allowed = data.get("allowed_users") or []
    if data.get("allowed_users") is not None:
        lines.append(f"  allowed:   {', '.join(str(name) for name in allowed) if allowed else '(nobody)'}")
    return "\n".join(lines)


def render_github_prs(data: dict) -> str:
    if not isinstance(data, dict):
        return str(data)
    if not data.get("configured"):
        return (
            "GitHub   not connected.\n"
            "  connect: hermes superagent github connect --repo owner/name --token <token>"
        )
    prs = [pr for pr in (data.get("prs") or []) if isinstance(pr, dict)]
    lines = [f"Open pull requests ({len(prs)})"]
    if not prs:
        lines.append("  (none)")
        return "\n".join(lines)
    for pr in prs:
        title = " ".join(str(pr.get("title") or "").split())
        if len(title) > 66:
            title = title[:65] + "…"
        meta = "   ".join(
            part
            for part in (
                str(pr.get("author") or "-"),
                _iso_age(pr.get("updated_at")) or "-",
                str(pr.get("html_url") or ""),
            )
            if part
        )
        lines.append(f"  #{str(pr.get('number') or '?'):<5} {title}")
        lines.append(f"         {meta}")
    return "\n".join(lines)


def render_github_poll(data: dict) -> str:
    if not isinstance(data, dict):
        return str(data)
    if not data.get("configured"):
        return "GitHub   not connected; nothing was polled."
    created = data.get("created") or []
    lines = [
        f"Polled {data.get('polled', 0)} open pull request(s);"
        f" created {len(created)} session(s)"
    ]
    if created:
        lines.append("  new: " + ", ".join(str(item) for item in created))
    for error in data.get("errors") or []:
        lines.append(f"  error: {error}")
    return "\n".join(lines)


def _db_path() -> Path:
    """Plugin database path (mirrors dashboard/plugin_api.py and sa_engine)."""
    try:
        from hermes_constants import get_hermes_home

        home = Path(get_hermes_home())
    except Exception:
        home = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")
    return home / "plugin-data" / "superagent" / "superagent.db"


def _chat_store(engine: Any) -> Any:
    """ChatStore on the same database the ticket engine already opened."""
    path = getattr(getattr(engine, "store", None), "path", "") or str(_db_path())
    return sa_chats.ChatStore(path)


def _get_runner(store: Any) -> Any:
    """Lazy Runner singleton keyed by database path (patched in tests)."""
    key = str(getattr(store, "path", "") or "")
    with _RUNNERS_LOCK:
        runner = _RUNNERS.get(key)
        if runner is None:
            runner = sa_runner.Runner(
                store,
                hermes_bin=os.environ.get("SUPERAGENT_HERMES_BIN") or None,
                max_workers=_env_int("SUPERAGENT_MAX_WORKERS", 2),
                toolsets=tuple(_env_list("SUPERAGENT_WORKER_TOOLSETS", ["file", "terminal"])),
                yolo=_env_bool("SUPERAGENT_WORKER_YOLO", True),
                model=_env("SUPERAGENT_DEFAULT_MODEL"),
                provider=_env("SUPERAGENT_DEFAULT_PROVIDER"),
                repo=_env("SUPERAGENT_REPO"),
                max_turns=_env_int("SUPERAGENT_CHAT_MAX_TURNS", 120),
            )
            _RUNNERS[key] = runner
        return runner


def _session_id_arg(value: Any) -> str:
    session_id = str(value or "").strip()
    if not session_id or "/" in session_id or "\\" in session_id or session_id in (".", ".."):
        return ""
    return session_id


def _elapsed_seconds(row: dict, now: float | None = None) -> float | None:
    if not isinstance(row, dict):
        return None
    now = time.time() if now is None else now
    started = row.get("started_at") or row.get("created_at")
    finished = row.get("finished_at")
    try:
        if finished and started:
            return max(0.0, float(finished) - float(started))
        if row.get("duration_ms") is not None:
            return max(0.0, float(row["duration_ms"]) / 1000.0)
        status = str(row.get("status") or "")
        if started and status in (sa_chats.QUEUED, sa_chats.RUNNING):
            return max(0.0, now - float(started))
        if started and row.get("updated_at"):
            return max(0.0, float(row["updated_at"]) - float(started))
    except (TypeError, ValueError):
        return None
    return None


def _format_elapsed(seconds: float | None) -> str:
    if seconds is None:
        return "-"
    total = int(max(0.0, seconds))
    if total < 60:
        return f"{total}s"
    minutes, secs = divmod(total, 60)
    if minutes < 60:
        return f"{minutes}m{secs:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m"


def _stamp(value: Any) -> str:
    try:
        return time.strftime("%H:%M:%S", time.localtime(float(value)))
    except (TypeError, ValueError, OSError):
        return "--:--:--"


def _clip_text(value: Any, limit: int = 4000) -> str:
    text = "" if value is None else str(value)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _indent(text: str, prefix: str = "    ") -> str:
    lines = str(text).splitlines() or [""]
    return "\n".join(prefix + line for line in lines)


def _enrich_sessions(data: dict, now: float | None = None) -> list[dict]:
    """Session rows newest first, with computed elapsed_seconds/elapsed."""
    now = time.time() if now is None else now
    rows = [dict(row) for row in (data.get("sessions") or []) if isinstance(row, dict)]
    rows.sort(
        key=lambda row: (row.get("updated_at") or 0, row.get("created_at") or 0), reverse=True
    )
    for row in rows:
        seconds = _elapsed_seconds(row, now)
        row["elapsed_seconds"] = seconds
        row["elapsed"] = _format_elapsed(seconds)
    return rows


def render_sessions(data: dict) -> str:
    rows = data.get("sessions") or []
    counts = data.get("counts") or {}
    archived = int(data.get("archived_count") or 0)
    header = f"Chat sessions   total={len(rows)}   running={data.get('running', 0)}"
    if archived:
        # Hidden by default; the count is what tells a reader they exist.
        header += f"   archived={archived} (hidden; --all shows them)"
    lines = [header]
    if counts:
        lines.append("  " + "  ".join(f"{key}={value}" for key, value in sorted(counts.items())))
    lines.append("")
    if not rows:
        lines.append('No chat sessions yet. Start one with: hermes superagent chat "<prompt>"')
        return "\n".join(lines)
    lines.append(
        f"  {'id':<7} {'status':<10} {'model':<22} {'elapsed':>8} {'tokens':>8}  title"
    )
    for row in rows:
        session_id = str(row.get("id") or "?")
        status = str(row.get("status") or "?")
        model = str(row.get("model") or "-")
        if len(model) > 22:
            model = model[:21] + "…"
        elapsed = str(row.get("elapsed") or "-")
        tokens = row.get("tokens")
        tokens_text = "-" if tokens is None else str(tokens)
        title = " ".join(str(row.get("title") or row.get("prompt") or "").split())
        if len(title) > 60:
            title = title[:59] + "…"
        lines.append(
            f"  {session_id:<7} {status:<10} {model:<22} {elapsed:>8} {tokens_text:>8}  {title}"
        )
    return "\n".join(lines)


def render_search(data: dict) -> str:
    """One block per hit: where it was, what it says, and why it matched."""
    rows = data.get("sessions") or []
    term = str(data.get("query") or "")
    if not rows:
        return f"No chats match '{term}'."
    lines = [
        f"Search '{term}'   {len(rows)} match{'es' if len(rows) != 1 else ''}",
        "",
    ]
    for row in rows:
        session_id = str(row.get("id") or "?")
        status = str(row.get("status") or "?")
        age = _format_age(
            time.time() - float(row.get("updated_at") or row.get("created_at") or 0)
        )
        title = " ".join(str(row.get("title") or row.get("prompt") or "").split())
        snippet = " ".join(str(row.get("match_snippet") or "").split())
        lines.append(f"  {session_id}  {status:<9} {age:>4}  {title}")
        if snippet:
            lines.append(f"      {snippet}")
    return "\n".join(lines)


def render_chat(session: dict, db_path: str = "") -> str:
    if not isinstance(session, dict):
        return str(session)
    session_id = session.get("id", "?")
    status = str(session.get("status", "?"))
    title = str(session.get("title") or "")
    model = str(session.get("model") or "-")
    provider = str(session.get("provider") or "")
    lines = [f"{session_id}  {status}" + (f"  {title}" if title else "")]
    lines.append(f"  model:      {model}" + (f" ({provider})" if provider else ""))
    if session.get("repo"):
        lines.append(f"  repo:       {session['repo']}")
    lines.append(f"  transcript: {db_path or '-'}")
    lines.append(f"  follow:     hermes superagent show {session_id}")
    return "\n".join(lines)


def render_session(session: dict) -> str:
    if not isinstance(session, dict):
        return str(session)
    session_id = session.get("id", "?")
    status = str(session.get("status", "?"))
    title = session.get("title") or sa_chats.derive_title(str(session.get("prompt") or ""))
    lines = [f"{session_id}  {status}  {title}"]
    model = session.get("model") or "-"
    provider = session.get("provider") or ""
    lines.append(f"  model:     {model}" + (f" ({provider})" if provider else ""))
    if session.get("repo"):
        lines.append(f"  repo:      {session['repo']}")
    if session.get("hermes_session_id"):
        lines.append(f"  hermes:    {session['hermes_session_id']}")
    elapsed = _format_elapsed(_elapsed_seconds(session))
    tokens = session.get("tokens")
    pid = session.get("pid")
    lines.append(
        f"  elapsed:   {elapsed}   tokens: {tokens if tokens is not None else '-'}"
        f"   pid: {pid if pid is not None else '-'}"
    )
    if session.get("last_event"):
        lines.append(f"  last:      {session['last_event']}")
    if session.get("error"):
        lines.append(f"  error:     {session['error']}")
    lines.append(f"  source:    {session.get('source') or '-'}")

    messages = session.get("messages") or []
    lines.append("")
    lines.append(f"messages ({len(messages)}):")
    if not messages:
        lines.append("  (none)")
    for message in messages:
        if not isinstance(message, dict):
            continue
        lines.append(f"  [{_stamp(message.get('created_at'))}] {message.get('role') or '?'}:")
        lines.append(_indent(_clip_text(message.get("content"))))

    events = session.get("events") or []
    lines.append("")
    lines.append(f"events ({len(events)}):")
    if not events:
        lines.append("  (none)")
    for event in events:
        if not isinstance(event, dict):
            continue
        lines.append(
            f"  [{_stamp(event.get('created_at'))}] {str(event.get('kind') or '?'):<10} "
            f"{_clip_text(event.get('detail'), 240)}"
        )
    return "\n".join(lines)


def _add_json_flag(parser: argparse.ArgumentParser) -> None:
    # SUPPRESS so a top-level `--json status` is not overwritten by the
    # subcommand default; `status --json` still sets it.
    parser.add_argument(
        "--json", action="store_true", default=argparse.SUPPRESS, help="Print raw JSON"
    )


def _launch_chat(args: argparse.Namespace, ctx: Any, engine: Any, want_json: bool) -> int:
    """`chat PROMPT`: create a session and start one headless worker turn."""
    parts = getattr(args, "prompt", None) or []
    prompt = " ".join(parts).strip() if isinstance(parts, (list, tuple)) else str(parts).strip()
    if not prompt:
        print(USAGE)
        return 2
    if len(prompt) > MAX_PROMPT_CHARS:
        print(f"error: prompt is too long (max {MAX_PROMPT_CHARS} chars)")
        return 2

    defaults = models_payload(ctx)
    preset_name = (getattr(args, "preset", "") or "").strip()
    preset = None
    if preset_name:
        preset = find_preset(defaults.get("presets") or [], preset_name)
        if preset is None:
            available = ", ".join(
                str(item.get("name") or "") for item in defaults.get("presets") or []
            ) or "(none configured)"
            print(f"error: unknown preset '{preset_name}'. Available: {available}")
            return 2
    # Explicit flags win, then the named preset, then the configured default.
    model = (
        (getattr(args, "model", "") or "").strip()
        or str((preset or {}).get("model") or "")
        or defaults["default_model"]
    )
    provider = (
        (getattr(args, "provider", "") or "").strip()
        or str((preset or {}).get("provider") or "")
        or defaults["default_provider"]
    )
    repo = (
        (getattr(args, "repo", "") or "").strip()
        or _env("SUPERAGENT_REPO")
        or str(_config(ctx, "repo_path", "") or "").strip()
    )
    title = (getattr(args, "title", "") or "").strip()

    try:
        store = _chat_store(engine)
        session = store.create_session(
            prompt, title=title, model=model, provider=provider, repo=repo, source="cli"
        )
        session_id = session["id"]
        if preset is not None:
            label = preset_label(preset)
            store.add_event(session_id, "preset", f"{preset_name} -> {label}")
    except Exception as exc:
        print(f"error: chat failed: {exc}")
        return 1

    detach = bool(getattr(args, "detach", False))
    try:
        _get_runner(store).launch_turn(
            session_id, prompt, model=model, provider=provider, repo=repo, block=not detach
        )
    except KeyboardInterrupt:
        # Ctrl-C during a blocking turn: cancel the worker, keep the session.
        try:
            _get_runner(store).stop(session_id)
        except Exception:
            pass
        print(f"\n{session_id} cancelled (interrupted)")
        return 1
    except Exception as exc:  # missing binary, bad repo, ...
        message = str(exc) or exc.__class__.__name__
        try:
            store.update_session(
                session_id, status=sa_chats.FAILED, error=message,
                finished_at=time.time(), pid=None,
            )
            store.add_event(session_id, "failed", message)
            failed = store.get_session(session_id, message_limit=0)
        except Exception:
            failed = {"id": session_id, "status": sa_chats.FAILED, "error": message}
        if want_json:
            print(json.dumps({
                "ok": False,
                "launched": False,
                "error": message,
                "db_path": str(getattr(store, "path", "")),
                "session": failed,
            }, indent=2, default=str))
        else:
            print(f"error: cannot launch worker for {session_id}: {message}")
        return 1

    try:
        if detach:
            store.add_event(session_id, "launched", "headless hermes chat worker")
            fresh = store.get_session(session_id, message_limit=0) or session
        else:
            # The turn already ran on this thread: show what it produced.
            fresh = store.get_session(session_id, message_limit=50) or session
    except Exception:
        fresh = session
    if want_json:
        print(json.dumps({
            "ok": True,
            "launched": True,
            "detached": detach,
            "session": fresh,
            "db_path": str(getattr(store, "path", "")),
            "transcript": f"hermes superagent show {session_id}",
        }, indent=2, default=str))
    elif detach:
        print(render_chat(fresh, str(getattr(store, "path", ""))))
    else:
        # Blocking turn: header (transcript path, follow hint) + what it produced.
        print(render_chat(fresh, str(getattr(store, "path", ""))))
        print()
        print(render_session(fresh))
    return 0


def _retry_chat(args: argparse.Namespace, engine: Any, want_json: bool) -> int:
    """`retry SESSION`: relaunch the last prompt of a failed/cancelled chat.

    The prompt is not appended again — it is already in the transcript — so a
    retry cannot turn one question into two. A live turn or a reviewable result
    is refused instead of being silently repeated.
    """
    session_id = _session_id_arg(getattr(args, "session_id", ""))
    if not session_id:
        print(USAGE)
        return 2
    try:
        store = _chat_store(engine)
        session = store.get_session(session_id, message_limit=0)
    except Exception as exc:
        print(f"error: retry failed: {exc}")
        return 1
    if session is None:
        print(f"error: unknown chat session {session_id}")
        return 1
    status = session.get("status")
    if status in (sa_chats.QUEUED, sa_chats.RUNNING):
        print(f"error: session {session_id} is {status}; nothing to retry")
        return 1
    if status not in (sa_chats.FAILED, sa_chats.CANCELLED):
        print(
            f"error: session {session_id} is {status};"
            " retry only a failed or cancelled chat"
        )
        return 1
    message = store.last_user_message(session_id)
    prompt = str((message or {}).get("content") or "").strip()
    if not prompt:
        print("error: no user message to retry")
        return 1

    model = str(session.get("model") or "")
    provider = str(session.get("provider") or "")
    repo = str(session.get("repo") or "")
    resume_id = str(session.get("hermes_session_id") or "").strip()
    detach = bool(getattr(args, "detach", False))
    db_path = str(getattr(store, "path", ""))
    try:
        store.update_session(
            session_id, error=None, tokens=None, duration_ms=None,
            last_event=None, finished_at=None,
        )
    except Exception as exc:
        print(f"error: retry failed: {exc}")
        return 1

    try:
        _get_runner(store).launch_turn(
            session_id,
            prompt,
            resume_session_id=resume_id or None,
            model=model,
            provider=provider,
            repo=repo,
            block=not detach,
        )
    except KeyboardInterrupt:
        try:
            _get_runner(store).stop(session_id)
        except Exception:
            pass
        print(f"\n{session_id} cancelled (interrupted)")
        return 1
    except Exception as exc:
        detail = str(exc) or exc.__class__.__name__
        try:
            store.update_session(
                session_id, status=sa_chats.FAILED, error=detail,
                finished_at=time.time(), pid=None,
            )
            store.add_event(session_id, "failed", detail)
            failed = store.get_session(session_id, message_limit=0)
        except Exception:
            failed = {"id": session_id, "status": sa_chats.FAILED, "error": detail}
        if want_json:
            print(json.dumps({
                "ok": False,
                "launched": False,
                "error": detail,
                "db_path": db_path,
                "session": failed,
            }, indent=2, default=str))
        else:
            print(f"error: cannot retry {session_id}: {detail}")
        return 1

    store.add_event(
        session_id,
        "retry",
        f"retry after {status} (resumed {resume_id})" if resume_id
        else f"retry after {status} (fresh session)",
    )
    fresh = store.get_session(session_id, message_limit=(0 if detach else 50)) or session
    if want_json:
        print(json.dumps({
            "ok": True,
            "launched": True,
            "detached": detach,
            "resumed": bool(resume_id),
            "session": fresh,
            "db_path": db_path,
            "transcript": f"hermes superagent show {session_id}",
        }, indent=2, default=str))
    else:
        print(render_chat(fresh, db_path))
        if not detach:
            print()
            print(render_session(fresh))
    return 0


def _list_sessions(args: argparse.Namespace, engine: Any, want_json: bool) -> int:
    try:
        limit = int(getattr(args, "limit", 20) or 20)
    except (TypeError, ValueError):
        limit = 20
    limit = max(1, min(limit, 200))
    archived = "all" if getattr(args, "show_archived", False) else "hide"
    try:
        store = _chat_store(engine)
        data = store.list_sessions(limit=limit, archived=archived)
        payload = {
            "db_exists": Path(str(store.path)).is_file(),
            "sessions": _enrich_sessions(data),
            "counts": data.get("counts") or {},
            "running": data.get("running", 0),
            "archived_count": data.get("archived_count", 0),
        }
    except Exception as exc:
        print(f"error: sessions failed: {exc}")
        return 1
    print(
        json.dumps(payload, indent=2, default=str)
        if want_json
        else render_sessions(payload)
    )
    return 0


def _search_chats(args: argparse.Namespace, engine: Any, want_json: bool) -> int:
    """Search the chat store; a blank term or bad mode is a usage error."""
    term = " ".join(getattr(args, "query", []) or []).strip()
    if not term:
        print("error: search needs a query")
        return 2
    if len(term) > sa_chats.SEARCH_QUERY_CHARS:
        print(
            f"error: query too long (max {sa_chats.SEARCH_QUERY_CHARS} characters)"
        )
        return 2
    archived = str(getattr(args, "archived", "hide") or "hide").strip().lower()
    if archived not in sa_chats.ARCHIVED_MODES:
        print(f"error: --archived must be one of {', '.join(sa_chats.ARCHIVED_MODES)}")
        return 2
    try:
        limit = int(getattr(args, "limit", 20) or 20)
    except (TypeError, ValueError):
        limit = 20
    limit = max(1, min(limit, 200))
    try:
        store = _chat_store(engine)
        data = store.search_sessions(term, limit=limit, archived=archived)
        payload = {
            "query": term,
            "db_exists": Path(str(store.path)).is_file(),
            "sessions": _enrich_sessions(data),
            "counts": data.get("counts") or {},
            "running": data.get("running", 0),
            "archived_count": data.get("archived_count", 0),
        }
    except Exception as exc:
        print(f"error: search failed: {exc}")
        return 1
    print(
        json.dumps(payload, indent=2, default=str)
        if want_json
        else render_search(payload)
    )
    return 0


def _show_session(session_id: str, engine: Any, want_json: bool) -> int:
    try:
        session = _chat_store(engine).get_session(session_id)
    except Exception as exc:
        print(f"error: show failed: {exc}")
        return 1
    if session is None:
        print(f"error: unknown chat session {session_id}")
        return 1
    print(
        json.dumps({"db_exists": True, "session": session}, indent=2, default=str)
        if want_json
        else render_session(session)
    )
    return 0


def _stop_session(session_id: str, engine: Any, want_json: bool) -> int:
    try:
        store = _chat_store(engine)
        session = store.get_session(session_id, message_limit=0)
    except Exception as exc:
        print(f"error: stop failed: {exc}")
        return 1
    if session is None:
        print(f"error: unknown chat session {session_id}")
        return 1

    status = str(session.get("status") or "")
    if status not in (sa_chats.QUEUED, sa_chats.RUNNING):
        message = f"session {session_id} is {status}; nothing to stop"
        if want_json:
            print(json.dumps(
                {"ok": False, "stopped": False, "error": message, "session": session},
                indent=2, default=str,
            ))
        else:
            print(f"{message}. Use `hermes superagent show {session_id}` to inspect it.")
        return 1

    error = ""
    try:
        stopped = bool(_get_runner(store).stop(session_id))
    except Exception as exc:
        error = str(exc) or exc.__class__.__name__
        stopped = False
        try:
            store.update_session(
                session_id, status=sa_chats.CANCELLED, error=error,
                finished_at=time.time(), pid=None,
            )
            store.add_event(session_id, "cancelled", error)
        except Exception:
            pass

    try:
        fresh = store.get_session(session_id, message_limit=0) or session
    except Exception:
        fresh = session
    if error:
        if want_json:
            print(json.dumps(
                {"ok": False, "stopped": False, "error": error, "session": fresh},
                indent=2, default=str,
            ))
        else:
            print(f"error: stop failed: {error}")
        return 1
    if want_json:
        print(json.dumps({"ok": True, "stopped": stopped, "session": fresh}, indent=2, default=str))
    else:
        print(f"cancelled {session_id} ({'worker stopped' if stopped else 'no live process'})")
    return 0


def _session_store() -> Any:
    """ChatStore on the plugin database (diff/comment need no ticket engine)."""
    return sa_chats.ChatStore(_db_path())


def _github_store() -> Any:
    """ChatStore on the plugin database (settings and sessions share it)."""
    return _session_store()


# -- session handoff: diff + comment ---------------------------------------
#
# Both commands read the same SQLite rows the dashboard uses and need no ticket
# engine: `hermes superagent diff S-0001` works before anyone opened the page.
# `comment` posts exactly one issue comment — it never commits or pushes.

COMMENT_USAGE = (
    "usage: hermes superagent comment <session-id> [--body TEXT | --body-file PATH] [--json]"
)


def render_diff(diff: dict, session_id: str = "") -> str:
    """Text view: header, stat, the file list, then the patch."""
    if not isinstance(diff, dict):
        return str(diff)
    if not diff.get("ok"):
        return f"error: {diff.get('error') or 'could not read the diff'}"
    files = diff.get("files") or []
    lines = [f"{session_id}  {diff.get('repo') or '-'}".strip()]
    label = f"  files:     {len(files)}"
    if diff.get("truncated"):
        label += "   (patch truncated)"
    lines.append(label)
    stat = str(diff.get("stat") or "").strip()
    if stat:
        lines += ["", _indent(stat, "  ")]
    if files:
        lines.append("")
        width = max(len(str(item.get("status") or "")) for item in files)
        for item in files:
            lines.append(
                f"  {str(item.get('status') or ''):<{width}}  {item.get('path') or '?'}"
            )
    lines.append("")
    patch = str(diff.get("patch") or "").strip()
    lines.append(patch if patch else "(no changes)")
    return "\n".join(lines)


def _diff_session(session_id: str, want_json: bool) -> int:
    """`diff <id>`: what this session changed in its repository."""
    try:
        session = _session_store().get_session(session_id, message_limit=0)
    except Exception as exc:
        print(f"error: diff failed: {exc}")
        return 1
    if session is None:
        print(f"error: unknown chat session {session_id}")
        return 1
    repo = str(session.get("repo") or "").strip()
    if not repo:
        message = f"session {session_id} has no repository; start a chat with --repo"
        print(
            json.dumps({"ok": False, "session_id": session_id, "error": message}, indent=2)
            if want_json
            else f"error: {message}"
        )
        return 1

    diff = sa_diff.collect_diff(repo)
    if want_json:
        print(json.dumps(
            {"ok": bool(diff.get("ok")), "session_id": session_id, "diff": diff},
            indent=2, default=str,
        ))
    else:
        print(render_diff(diff, session_id))
    return 0 if diff.get("ok") else 1


def _comment_session(args: Any, want_json: bool) -> int:
    """`comment <id>`: post the summary (or --body) to the session's PR."""
    session_id = _session_id_arg(getattr(args, "session_id", ""))
    if not session_id:
        print(COMMENT_USAGE)
        return 2
    body_text = str(getattr(args, "body", "") or "")
    body_file = str(getattr(args, "body_file", "") or "").strip()
    if body_text.strip() and body_file:
        print("error: use --body or --body-file, not both")
        return 2
    if body_file:
        try:
            body_text = Path(body_file).expanduser().read_text(encoding="utf-8")
        except OSError as exc:
            print(f"error: cannot read {body_file}: {exc}")
            return 2
        if not body_text.strip():
            print(f"error: {body_file} is empty")
            return 2
    if len(body_text) > MAX_COMMENT_CHARS:
        print(f"error: comment is too long (max {MAX_COMMENT_CHARS} chars)")
        return 2

    try:
        store = _session_store()
        session = store.get_session(session_id)
    except Exception as exc:
        print(f"error: comment failed: {exc}")
        return 1
    if session is None:
        print(f"error: unknown chat session {session_id}")
        return 1
    declared = session.get("pr_number")
    if not declared:
        print(f"error: session {session_id} has no pull request; nothing to comment on")
        return 1
    try:
        pr_number = int(declared)
    except (TypeError, ValueError):
        print("error: the session's pull request number is invalid")
        return 1

    settings = sa_settings.load_settings()
    repo = str(session.get("github_repo") or settings.get("github_repo") or "").strip()
    token = str(settings.get("github_token") or "").strip()
    if not sa_settings.valid_repo_name(repo):
        print("error: no GitHub repository is configured (see `hermes superagent github connect`)")
        return 1
    if not token:
        print("error: a GitHub token is required (`hermes superagent github connect --token ...`)")
        return 1

    diff = sa_diff.collect_diff(str(session.get("repo") or ""))
    text = body_text.strip() or sa_diff.default_comment_body(
        session, diff if diff.get("ok") else None
    )
    try:
        posted = sa_github.GitHubClient(token, repo).post_comment(pr_number, text)
    except sa_github.GitHubError as exc:
        print(f"error: {_mask(exc, token)}")
        return 1
    except Exception as exc:
        print(f"error: comment failed: {_mask(exc, token)}")
        return 1

    url = str((posted or {}).get("html_url") or "")
    try:
        store.add_event(
            session_id, "commented", f"PR #{pr_number}: {url}" if url else f"PR #{pr_number}"
        )
    except Exception:
        pass  # the comment is already public; a local audit write must not fail it

    if want_json:
        print(json.dumps({
            "ok": True,
            "url": url,
            "pr_number": pr_number,
            "github_repo": repo,
            "body_preview": text if len(text) <= 280 else text[:279] + "…",
        }, indent=2, default=str))
    else:
        print(f"commented on {repo}#{pr_number}" + (f"\n  {url}" if url else ""))
    return 0


def _github_watcher(store: Any = None) -> Any:
    """A watcher over the current settings (no HTTP layer, no background thread)."""
    store = store if store is not None else _github_store()
    return sa_watcher.GitHubWatcher(store, _get_runner(store), sa_settings.load_settings)


def _clear_github_baseline(store: Any, repo: str) -> None:
    """Forget dedupe state so a fresh connect starts a new baseline."""
    if not repo:
        return
    for prefix in (f"gh_baseline:{repo}", f"gh_comment:{repo}:", f"gh_head:{repo}:"):
        try:
            store.clear_seen(prefix)
        except Exception:
            pass


def _github_credentials(args: Any) -> tuple[str, str, str]:
    """(repo, token, explicit token) from flags, falling back to settings."""
    settings = sa_settings.load_settings()
    repo = str(getattr(args, "repo", "") or "").strip() or str(settings.get("github_repo") or "").strip()
    supplied = str(getattr(args, "token", "") or "").strip()
    token = supplied or str(settings.get("github_token") or "").strip()
    return repo, token, supplied


def run_github(args: Any, want_json: bool = False) -> int:
    """`hermes superagent github ...`. Returns a process exit code."""
    action = str(getattr(args, "action", "") or "status").strip().lower()
    if action not in GITHUB_ACTIONS:
        print(GITHUB_USAGE)
        return 2

    watch_mode = str(getattr(args, "watch_mode", "") or "").strip().lower()
    if watch_mode and watch_mode not in GITHUB_WATCH_MODES:
        print("error: --watch-mode must be observe or auto")
        return 2
    raw_allowed = getattr(args, "allowed", None)
    try:
        allowed = None if raw_allowed is None else _clean_allowed(raw_allowed)
    except ValueError as exc:
        print(f"error: {exc}")
        return 2

    if action == "status":
        try:
            data = _github_watcher().status()
        except Exception as exc:
            print(f"error: github status failed: {exc}")
            return 1
        print(json.dumps(data, indent=2, default=str) if want_json else render_github_status(data))
        return 0

    if action == "prs":
        try:
            status = _github_watcher().status()
        except Exception as exc:
            print(f"error: github prs failed: {exc}")
            return 1
        data = {"configured": bool(status.get("configured")), "prs": status.get("open_prs") or []}
        print(json.dumps(data, indent=2, default=str) if want_json else render_github_prs(data))
        return 0

    if action == "poll":
        try:
            summary = _github_watcher().poll_once()
        except Exception as exc:
            print(f"error: github poll failed: {exc}")
            return 1
        data = {
            "configured": bool(summary.get("configured")),
            "polled": int(summary.get("polled") or 0),
            "created": list(summary.get("created") or []),
            "errors": [str(item) for item in (summary.get("errors") or [])],
        }
        print(json.dumps(data, indent=2, default=str) if want_json else render_github_poll(data))
        return 0

    if action == "disconnect":
        try:
            sa_settings.save_settings({"github_repo": "", "github_token": ""})
        except (ValueError, OSError) as exc:
            print(f"error: could not save settings: {exc}")
            return 1
        print(json.dumps({"ok": True, "configured": False}) if want_json else "GitHub   disconnected")
        return 0

    if action == "test":
        repo, token, _ = _github_credentials(args)
        if not sa_settings.valid_repo_name(repo):
            print("error: --repo owner/name is required")
            return 2
        if not token:
            print("error: a GitHub token is required (--token or saved settings)")
            return 1
        try:
            client = sa_github.GitHubClient(token, repo)
            login = str((client.me() or {}).get("login") or "")
            info = client.repo_info()
            try:
                open_pr_count = len(client.list_open_prs(limit=100))
            except sa_github.GitHubError:
                open_pr_count = int(info.get("open_issues") or 0)
        except sa_github.GitHubError as exc:
            print(f"error: {_mask(exc, token)}")
            return 1
        except Exception as exc:
            print(f"error: github test failed: {_mask(exc, token)}")
            return 1
        data = {
            "ok": True,
            "repo": repo,
            "login": login,
            "full_name": info.get("full_name") or repo,
            "default_branch": info.get("default_branch") or "",
            "private": bool(info.get("private")),
            "open_pr_count": open_pr_count,
            "token_set": True,
        }
        print(json.dumps(data, indent=2, default=str) if want_json else render_github_account(data))
        return 0

    if action == "connect":
        repo, token, supplied = _github_credentials(args)
        if not sa_settings.valid_repo_name(repo):
            print("error: --repo owner/name is required")
            return 2
        if not token:
            print("error: a GitHub token is required (--token or saved settings)")
            return 1
        try:
            client = sa_github.GitHubClient(token, repo)
            login = str((client.me() or {}).get("login") or "")
            info = client.repo_info()
        except sa_github.GitHubError as exc:
            print(f"error: {_mask(exc, token)}")
            return 1
        except Exception as exc:
            print(f"error: github connect failed: {_mask(exc, token)}")
            return 1

        patch: dict[str, Any] = {"github_repo": repo}
        if supplied:
            patch["github_token"] = supplied
        if login:
            patch["github_agent_login"] = login
        if allowed is not None:
            patch["github_allowed_users"] = allowed
        if watch_mode:
            patch["github_watch_mode"] = watch_mode
        try:
            sa_settings.save_settings(patch)
        except (ValueError, OSError) as exc:
            print(f"error: could not save settings: {exc}")
            return 1
        _clear_github_baseline(_github_store(), repo)

        saved = sa_settings.load_settings()
        data = {
            "ok": True,
            "configured": True,
            "repo": repo,
            "login": login,
            "full_name": info.get("full_name") or repo,
            "default_branch": info.get("default_branch") or "",
            "private": bool(info.get("private")),
            "open_pr_count": int(info.get("open_issues") or 0),
            "token_set": True,
            "watch_mode": saved.get("github_watch_mode") or "observe",
            "allowed_users": saved.get("github_allowed_users") or [],
            "poll_seconds": saved.get("github_poll_seconds"),
        }
        print(json.dumps(data, indent=2, default=str) if want_json else render_github_account(data))
        return 0

    # action == "work"
    raw_number = getattr(args, "number", None)
    try:
        number = int(str(raw_number).strip()) if raw_number is not None else 0
    except (TypeError, ValueError):
        number = 0
    if number <= 0:
        print(GITHUB_USAGE)
        return 2

    settings = sa_settings.load_settings()
    repo = str(getattr(args, "repo", "") or "").strip() or str(settings.get("github_repo") or "").strip()
    token = str(getattr(args, "token", "") or "").strip() or str(settings.get("github_token") or "").strip()
    if not repo or not token:
        print("error: GitHub is not connected (run `hermes superagent github connect`)")
        return 1

    try:
        client = sa_github.GitHubClient(token, repo)
        pr = client.get_pr(number)
    except sa_github.GitHubError as exc:
        print(f"error: {_mask(exc, token)}")
        return 1
    except Exception as exc:
        print(f"error: github work failed: {_mask(exc, token)}")
        return 1

    repo_path = str(settings.get("repo_path") or "").strip()
    checkout = ""
    warning = ""
    if repo_path:
        workspace = str(sa_settings.data_dir() / "workspaces")
        try:
            info = sa_github.prepare_pr_checkout(repo_path, repo, number, workspace, token)
            checkout = str(info.get("path") or "")
        except sa_github.GitHubError as exc:
            warning = _mask(exc, token)

    request_text = str(getattr(args, "request", "") or "").strip()
    comment = {"author": "you", "body": request_text} if request_text else None
    prompt = sa_github.build_pr_prompt(pr, comment=comment, checkout=checkout)
    session_repo = checkout
    if not session_repo and repo_path and Path(repo_path).expanduser().is_dir():
        session_repo = repo_path
    model = str(settings.get("default_model") or "")
    provider = str(settings.get("default_provider") or "")

    try:
        store = _github_store()
        session = store.create_session(
            prompt,
            title=f"PR #{number}: {pr.get('title') or ''}".strip(),
            model=model,
            provider=provider,
            repo=session_repo,
            source="github_pr",
            github_repo=repo,
            pr_number=number,
            pr_url=str(pr.get("html_url") or ""),
            status=sa_chats.QUEUED,
        )
        session_id = str(session["id"])
        store.add_event(session_id, "github", f"CLI work on PR #{number}")
        if warning:
            store.add_event(session_id, "github", f"checkout failed: {warning}")
    except Exception as exc:
        print(f"error: could not create the session: {_mask(exc, token)}")
        return 1

    try:
        _get_runner(store).launch_turn(
            session_id,
            prompt,
            model=model or None,
            provider=provider or None,
            repo=session_repo,
            block=True,
        )
    except KeyboardInterrupt:
        try:
            _get_runner(store).stop(session_id)
        except Exception:
            pass
        print(f"\n{session_id} cancelled (interrupted)")
        return 1
    except Exception as exc:
        message = _mask(str(exc) or exc.__class__.__name__, token)
        try:
            store.update_session(
                session_id, status=sa_chats.FAILED, error=message,
                finished_at=time.time(), pid=None,
            )
            store.add_event(session_id, "failed", message)
        except Exception:
            pass
        if want_json:
            print(json.dumps(
                {"ok": False, "launched": False, "error": message, "session_id": session_id},
                indent=2, default=str,
            ))
        else:
            print(f"error: cannot launch worker for {session_id}: {message}")
        return 1

    try:
        fresh = store.get_session(session_id, message_limit=50) or session
    except Exception:
        fresh = session
    if want_json:
        print(json.dumps({
            "ok": True,
            "launched": True,
            "session": fresh,
            "checkout": checkout,
            "warning": warning,
            "transcript": f"hermes superagent show {session_id}",
        }, indent=2, default=str))
    else:
        print(render_chat(fresh, str(getattr(store, "path", ""))))
        print(f"  work on:   PR #{number} in {repo}")
        if checkout:
            print(f"  checkout:  {checkout}")
        if warning:
            print(f"  warning:   {warning}")
    return 0


def setup_cli(subparser: argparse.ArgumentParser) -> None:
    subparser.add_argument("--json", action="store_true", help="Print raw JSON")
    subs = subparser.add_subparsers(dest="superagent_command")
    status_p = subs.add_parser("status", help="Show tickets and what needs a human")
    _add_json_flag(status_p)
    tick_p = subs.add_parser(
        "tick", help="Run one routing pass (queues work; workers start from a Hermes turn)"
    )
    _add_json_flag(tick_p)
    ask = subs.add_parser("ask", help="Record a request as a ticket")
    ask.add_argument("request", nargs="+", help="The work to do")
    _add_json_flag(ask)
    run_p = subs.add_parser("run", help="Approve a ticket and start its worker")
    run_p.add_argument("ticket_id", help="Ticket id (e.g. T-0001)")
    _add_json_flag(run_p)
    show_p = subs.add_parser(
        "show", help="Show a ticket, or a chat session with its messages and events"
    )
    show_p.add_argument("ticket_id", help="Ticket id (T-0001) or chat session id (S-0001)")
    _add_json_flag(show_p)
    chat_p = subs.add_parser(
        "chat", help="Start a chat session and launch a headless worker turn"
    )
    chat_p.add_argument("prompt", nargs="+", help="What the worker should do")
    chat_p.add_argument("--model", default="", help="Model override for this session")
    chat_p.add_argument("--provider", default="", help="Provider override for this session")
    chat_p.add_argument(
        "--preset", default="", help="Named model preset (see `hermes superagent models`)"
    )
    chat_p.add_argument("--repo", default="", help="Repository the worker may edit")
    chat_p.add_argument("--title", default="", help="Session title (default: first prompt line)")
    chat_p.add_argument(
        "--detach",
        action="store_true",
        help="Launch the turn and return immediately instead of waiting for it",
    )
    _add_json_flag(chat_p)
    sessions_p = subs.add_parser("sessions", help="List chat sessions, newest first")
    sessions_p.add_argument("--limit", type=int, default=20, help="How many sessions to show")
    sessions_p.add_argument(
        "--all", dest="show_archived", action="store_true",
        help="Include archived sessions (hidden by default)",
    )
    _add_json_flag(sessions_p)
    search_p = subs.add_parser(
        "search", help="Search chat titles, prompts and transcripts"
    )
    search_p.add_argument("query", nargs="+", help="Text to look for")
    search_p.add_argument("--limit", type=int, default=20, help="How many matches to show")
    search_p.add_argument(
        "--archived", default="hide", choices=("hide", "only", "all"),
        help="Archived visibility: hide (default), only or all",
    )
    _add_json_flag(search_p)
    stop_p = subs.add_parser("stop", help="Cancel a queued or running chat turn")
    stop_p.add_argument("session_id", help="Chat session id (e.g. S-0001)")
    _add_json_flag(stop_p)
    retry_p = subs.add_parser(
        "retry", help="Re-run the last prompt of a failed or cancelled chat"
    )
    retry_p.add_argument("session_id", help="Chat session id (e.g. S-0001)")
    retry_p.add_argument(
        "--detach", action="store_true",
        help="Launch the turn and return instead of waiting for it",
    )
    _add_json_flag(retry_p)
    diff_p = subs.add_parser(
        "diff", help="Show git status, stat and patch for a chat session's repository"
    )
    diff_p.add_argument("session_id", help="Chat session id (e.g. S-0001)")
    _add_json_flag(diff_p)
    comment_p = subs.add_parser(
        "comment", help="Post a finished session's summary to its pull request"
    )
    comment_p.add_argument("session_id", help="Chat session id (e.g. S-0001)")
    comment_p.add_argument(
        "--body", default="", help="Comment text (default: generated summary with diff totals)"
    )
    comment_p.add_argument(
        "--body-file", dest="body_file", default="", help="Read the comment text from a file"
    )
    _add_json_flag(comment_p)
    models_p = subs.add_parser("models", help="Show the default model, provider and presets")
    _add_json_flag(models_p)
    settings_p = subs.add_parser("settings", help="Show merged settings (read-only)")
    _add_json_flag(settings_p)
    github_p = subs.add_parser(
        "github", help="Connect GitHub and work on pull requests from the terminal"
    )
    github_p.add_argument(
        "action",
        nargs="?",
        default="status",
        help="status|test|connect|disconnect|prs|poll|work",
    )
    github_p.add_argument("number", nargs="?", default=None, help="Pull request number for `work`")
    github_p.add_argument("--repo", default="", help="Repository as owner/name")
    github_p.add_argument("--token", default="", help="GitHub token (stored, never echoed)")
    github_p.add_argument(
        "--allowed", default=None, help="Comma-separated GitHub logins allowed to request work"
    )
    github_p.add_argument(
        "--watch-mode", dest="watch_mode", default="", help="observe or auto"
    )
    github_p.add_argument("--request", default="", help="Extra instruction for `work`")
    _add_json_flag(github_p)
    doctor_p = subs.add_parser("doctor", help="Check configuration and worker availability")
    _add_json_flag(doctor_p)
    subparser.set_defaults(func=run)


def run(args: argparse.Namespace, ctx: Any) -> int:
    command = getattr(args, "superagent_command", None) or "status"
    want_json = bool(getattr(args, "json", False))

    # `models` only reads settings; it must work even if the engine cannot start.
    if command == "models":
        data = models_payload(ctx)
        print(json.dumps(data, indent=2) if want_json else render_models(data))
        return 0

    # `settings` is read-only and likewise needs no ticket engine.
    if command == "settings":
        return run_settings(want_json)

    # `github` talks to sa_settings/sa_github/GitHubWatcher directly: it needs
    # no ticket engine (and no HTTP layer), so it also works before anyone has
    # started the dashboard.
    if command == "github":
        return run_github(args, want_json)

    # `diff`/`comment` read the chat tables directly for the same reason: the
    # handoff commands must work from a plain terminal.
    if command == "diff":
        session_id = _session_id_arg(getattr(args, "session_id", ""))
        if not session_id:
            print(USAGE)
            return 2
        return _diff_session(session_id, want_json)

    if command == "comment":
        return _comment_session(args, want_json)

    try:
        engine = sa_engine.build_engine(ctx)
    except Exception as exc:
        print(f"error: cannot start superagent: {exc}")
        return 1

    if command == "status":
        try:
            data = engine.status()
        except Exception as exc:
            print(f"error: status failed: {exc}")
            return 1
        print(json.dumps(data, indent=2) if want_json else render_status(data))
        return 0

    if command == "tick":
        try:
            result = engine.tick(launch=False)
        except Exception as exc:
            print(f"error: tick failed: {exc}")
            return 1
        try:
            data = {"tick": result.as_dict(), "status": engine.status()}
        except Exception as exc:
            print(f"error: tick failed: {exc}")
            return 1
        if want_json:
            print(json.dumps(data, indent=2, default=str))
        else:
            print(render_status(data["status"]))
            print("")
            print(
                f"polled={result.polled} created={len(result.created)} "
                f"launched={len(result.launched)} finished={len(result.finished)}"
            )
            for warning in result.warnings:
                print(f"note: {warning}")
        return 0

    if command == "chat":
        return _launch_chat(args, ctx, engine, want_json)

    if command == "sessions":
        return _list_sessions(args, engine, want_json)

    if command == "search":
        return _search_chats(args, engine, want_json)

    if command == "stop":
        session_id = _session_id_arg(getattr(args, "session_id", ""))
        if not session_id:
            print(USAGE)
            return 2
        return _stop_session(session_id, engine, want_json)

    if command == "retry":
        return _retry_chat(args, engine, want_json)

    if command == "ask":
        parts = getattr(args, "request", None)
        request = " ".join(parts).strip() if parts else ""
        if not request:
            print(USAGE)
            return 2
        try:
            ticket = engine.ask(request, requester="cli")
        except Exception as exc:
            print(f"error: ask failed: {exc}")
            return 1
        try:
            full = engine.status(ticket["id"])
        except Exception:
            full = ticket
        print(json.dumps(full, indent=2, default=str) if want_json else render_ticket(full))
        return 0

    if command == "run":
        ticket_id = (getattr(args, "ticket_id", None) or "").strip()
        if not ticket_id:
            print(USAGE)
            return 2
        try:
            ticket = engine.promote(ticket_id)
        except KeyError as exc:
            print(f"error: {exc}")
            return 1
        except RuntimeError as exc:
            print(f"error: {exc}")
            return 1
        except Exception as exc:
            print(f"error: run failed: {exc}")
            return 1
        if want_json:
            print(json.dumps(ticket, indent=2, default=str))
        else:
            try:
                full = engine.status(ticket["id"])
            except Exception:
                full = ticket
            print(render_ticket(full))
            if ticket.get("launched"):
                print("worker launched")
            else:
                print(f"queued ({ticket.get('error') or 'starts on the next tick'})")
        return 0

    if command == "show":
        ticket_id = (getattr(args, "ticket_id", None) or "").strip()
        if not ticket_id:
            print(USAGE)
            return 2
        if ticket_id.upper().startswith("S-"):
            session_id = _session_id_arg(ticket_id)
            if not session_id:
                print(USAGE)
                return 2
            return _show_session(session_id, engine, want_json)
        try:
            data = engine.status(ticket_id)
        except KeyError as exc:
            print(f"error: {exc}")
            return 1
        except Exception as exc:
            print(f"error: show failed: {exc}")
            return 1
        print(json.dumps(data, indent=2, default=str) if want_json else render_ticket(data))
        return 0

    if command == "doctor":
        try:
            checks = engine.doctor()
        except Exception as exc:
            print(f"error: doctor failed: {exc}")
            return 1
        print(json.dumps(checks, indent=2) if want_json else "\n".join(f"{k}: {v}" for k, v in checks.items()))
        return 0

    print(USAGE)
    return 2
