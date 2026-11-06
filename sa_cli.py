"""`hermes superagent ...` and the text views shared with `/superagent`."""

from __future__ import annotations

import argparse
import json
import os
import threading
import time
from pathlib import Path
from typing import Any

try:
    from . import sa_chats, sa_engine, sa_runner
except ImportError:  # top-level import (root __init__ loaded as `__init__` by pytest)
    import sa_chats  # type: ignore[no-redef]
    import sa_engine  # type: ignore[no-redef]
    import sa_runner  # type: ignore[no-redef]

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
# implementation this CLI drives): same prompt cap, same preset defaults.
MAX_PROMPT_CHARS = 8000
DEFAULT_MODEL_PRESETS = [
    "deepseek-v4.1-flash",
    "qwen/qwen3.8-27b",
    "z-ai/glm-5.3-flash",
]

# One Runner per database path, like the dashboard singleton: a CLI session
# and an in-process dashboard share worker slots instead of racing.
_RUNNERS: dict[str, Any] = {}
_RUNNERS_LOCK = threading.Lock()


USAGE = (
    "usage: hermes superagent "
    "<status|tick|ask <request>|run <ticket-id>|show <ticket-id|session-id>|"
    "chat <prompt>|sessions|stop <session-id>|models|doctor> [--json]"
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


def _as_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    if isinstance(value, (list, tuple)):
        return [str(part).strip() for part in value if str(part).strip()]
    return []


def models_payload(ctx: Any) -> dict:
    """Resolved default model/provider plus presets (same keys as GET /models)."""
    model = _env("SUPERAGENT_DEFAULT_MODEL") or str(_config(ctx, "default_model", "") or "").strip()
    provider = _env("SUPERAGENT_DEFAULT_PROVIDER") or str(
        _config(ctx, "default_provider", "") or ""
    ).strip()
    presets = _env_list("SUPERAGENT_MODEL_PRESETS", DEFAULT_MODEL_PRESETS)
    if not os.environ.get("SUPERAGENT_MODEL_PRESETS"):
        configured = _as_list(_config(ctx, "model_presets", []))
        if configured:
            presets = configured
    return {"default_model": model, "default_provider": provider, "models": presets}


def render_models(data: dict) -> str:
    lines = [
        f"default model:    {data.get('default_model') or '(unset)'}",
        f"default provider: {data.get('default_provider') or '(unset)'}",
        "",
        "presets:",
    ]
    presets = data.get("models") or []
    if presets:
        lines.extend(f"  - {name}" for name in presets)
    else:
        lines.append("  (none)")
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
    lines = [f"Chat sessions   total={len(rows)}   running={data.get('running', 0)}"]
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
    model = (getattr(args, "model", "") or "").strip() or defaults["default_model"]
    provider = (getattr(args, "provider", "") or "").strip() or defaults["default_provider"]
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


def _list_sessions(args: argparse.Namespace, engine: Any, want_json: bool) -> int:
    try:
        limit = int(getattr(args, "limit", 20) or 20)
    except (TypeError, ValueError):
        limit = 20
    limit = max(1, min(limit, 200))
    try:
        store = _chat_store(engine)
        data = store.list_sessions(limit=limit)
        payload = {
            "db_exists": Path(str(store.path)).is_file(),
            "sessions": _enrich_sessions(data),
            "counts": data.get("counts") or {},
            "running": data.get("running", 0),
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
    _add_json_flag(sessions_p)
    stop_p = subs.add_parser("stop", help="Cancel a queued or running chat turn")
    stop_p.add_argument("session_id", help="Chat session id (e.g. S-0001)")
    _add_json_flag(stop_p)
    models_p = subs.add_parser("models", help="Show the default model, provider and presets")
    _add_json_flag(models_p)
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

    if command == "stop":
        session_id = _session_id_arg(getattr(args, "session_id", ""))
        if not session_id:
            print(USAGE)
            return 2
        return _stop_session(session_id, engine, want_json)

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
