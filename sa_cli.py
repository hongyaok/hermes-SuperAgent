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


def render_status(data: dict) -> str:
    lines = [
        f"Superagent   mode={data.get('mode')}   running={data.get('running', 0)}   queued={data.get('queued', 0)}"
    ]
    attention = data.get("attention") or []
    lines.append("")
    if attention:
        lines.append("Needs you:")
        for ticket in attention:
            icon = STATUS_ICONS.get(ticket["status"], "?")
            lines.append(
                f"  {icon} {ticket['id']}  {ticket['status']:<9} {ticket['requester'] or '-':<12} {ticket['request']}"
            )
            if ticket.get("reason"):
                lines.append(f"      {ticket['reason']}")
            if ticket.get("error"):
                lines.append(f"      error: {ticket['error']}")
    else:
        lines.append("Nothing needs you.")

    tickets = data.get("tickets") or []
    if tickets:
        lines.append("")
        lines.append("Recent tickets:")
        for ticket in tickets[:12]:
            icon = STATUS_ICONS.get(ticket["status"], "?")
            lines.append(f"  {icon} {ticket['id']}  {ticket['status']:<9} {ticket['request'][:90]}")
    return "\n".join(lines)


def render_ticket(ticket: dict) -> str:
    lines = [
        f"{ticket['id']}  {ticket['status']}",
        f"  request:   {ticket['request']}",
        f"  requester: {ticket['requester'] or '-'} via {ticket['source']}",
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
            lines.append(f"    - {event['kind']}: {(event['detail'] or '')[:160]}")
    return "\n".join(lines)


def setup_cli(subparser: argparse.ArgumentParser) -> None:
    subparser.add_argument("--json", action="store_true", help="Print raw JSON")
    subs = subparser.add_subparsers(dest="superagent_command")
    subs.add_parser("status", help="Show tickets and what needs a human")
    subs.add_parser("tick", help="Run one routing pass (queues work; workers start from a Hermes turn)")
    ask = subs.add_parser("ask", help="Record a request as a ticket")
    ask.add_argument("request", nargs="+", help="The work to do")
    subs.add_parser("doctor", help="Check configuration and worker availability")
    subparser.set_defaults(func=run)


def run(args: argparse.Namespace, ctx: Any) -> int:
    command = getattr(args, "superagent_command", None) or "status"
    engine = sa_engine.build_engine(ctx)

    if command == "status":
        data = engine.status()
        print(json.dumps(data, indent=2) if getattr(args, "json", False) else render_status(data))
        return 0

    if command == "tick":
        result = engine.tick(launch=False)
        data = {"tick": result.as_dict(), "status": engine.status()}
        if getattr(args, "json", False):
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
        request = " ".join(args.request).strip()
        ticket = engine.ask(request, requester="cli")
        print(json.dumps(ticket, indent=2, default=str) if getattr(args, "json", False) else render_ticket(engine.status(ticket["id"])))
        return 0

    if command == "doctor":
        checks = engine.doctor()
        print(json.dumps(checks, indent=2) if getattr(args, "json", False) else "\n".join(f"{k}: {v}" for k, v in checks.items()))
        return 0

    print("usage: hermes superagent <status|tick|ask|doctor>")
    return 2
