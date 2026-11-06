"""`hermes superagent ...` and the text views shared with `/superagent`."""

from __future__ import annotations

import argparse
import json
from typing import Any

try:
    from . import sa_engine
except ImportError:  # top-level import (root __init__ loaded as `__init__` by pytest)
    import sa_engine  # type: ignore[no-redef]

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


USAGE = (
    "usage: hermes superagent "
    "<status|tick|ask <request>|run <ticket-id>|show <ticket-id>|doctor> [--json]"
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


def _add_json_flag(parser: argparse.ArgumentParser) -> None:
    # SUPPRESS so a top-level `--json status` is not overwritten by the
    # subcommand default; `status --json` still sets it.
    parser.add_argument(
        "--json", action="store_true", default=argparse.SUPPRESS, help="Print raw JSON"
    )


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
    show_p = subs.add_parser("show", help="Show full detail and history for one ticket")
    show_p.add_argument("ticket_id", help="Ticket id (e.g. T-0001)")
    _add_json_flag(show_p)
    doctor_p = subs.add_parser("doctor", help="Check configuration and worker availability")
    _add_json_flag(doctor_p)
    subparser.set_defaults(func=run)


def run(args: argparse.Namespace, ctx: Any) -> int:
    command = getattr(args, "superagent_command", None) or "status"
    want_json = bool(getattr(args, "json", False))
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
