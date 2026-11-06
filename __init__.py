"""Superagent — an attention-frugal router/worker layer for Hermes.

The plugin is deliberately small: sources produce requests, a rule-based router
decides what to do with them, SQLite holds the tickets, and Hermes subagents do
the work. Nothing here runs a model of its own.

Design notes live in DESIGN.md; the operating rules live in
skills/superagent-operator/SKILL.md.
"""

from __future__ import annotations

import logging
from functools import partial
from pathlib import Path
from typing import Any

try:  # normal: loaded as hermes_plugins.superagent (or sa_plugin in tests)
    from . import sa_chats, sa_cli, sa_engine, sa_schemas, sa_tools
except ImportError:  # top-level import (pytest imports root __init__.py as `__init__`)
    import sa_chats  # type: ignore[no-redef]
    import sa_cli  # type: ignore[no-redef]
    import sa_engine  # type: ignore[no-redef]
    import sa_schemas  # type: ignore[no-redef]
    import sa_tools  # type: ignore[no-redef]

logger = logging.getLogger(__name__)

TOOLSET = "superagent"


def register(ctx: Any) -> None:
    for schema, handler in (
        (sa_schemas.ASK, sa_tools.superagent_ask),
        (sa_schemas.TICK, sa_tools.superagent_tick),
        (sa_schemas.STATUS, sa_tools.superagent_status),
        (sa_schemas.RUN, sa_tools.superagent_run),
    ):
        ctx.register_tool(
            name=schema["name"],
            toolset=TOOLSET,
            schema=schema,
            handler=partial(handler, ctx=ctx),
        )

    ctx.register_command(
        "superagent",
        handler=lambda raw, **kwargs: slash_command(raw, ctx),
        description="Superagent tickets: status, tick, ask, run",
        args_hint="[status|tick|ask <request>|run <id>]",
    )

    ctx.register_cli_command(
        name="superagent",
        help="Router/worker tickets for coding work",
        setup_fn=sa_cli.setup_cli,
        handler_fn=lambda args: sa_cli.run(args, ctx),
    )

    ctx.register_automation_blueprint(
        "watch",
        title="Superagent watch",
        description=(
            "Every 5 minutes, poll the configured sources, route new requests, and launch "
            "worker subagents for accepted tickets. Reports only what needs a human."
        ),
        schedule_template="*/5 * * * *",
        prompt_template=(
            "Call the superagent_tick tool with wait=true. Then report ONLY the tickets that need "
            "a human (blocked, review, failed or observed). If there are none, reply with one line: "
            "'Superagent: nothing needs you.'"
        ),
        category="general",
        tags=("superagent", "agents", "github"),
    )

    skills_dir = Path(__file__).parent / "skills"
    if skills_dir.is_dir():
        for child in sorted(skills_dir.iterdir()):
            skill_md = child / "SKILL.md"
            if child.is_dir() and skill_md.exists():
                ctx.register_skill(child.name, skill_md)


def slash_command(raw_args: str, ctx: Any) -> str:
    parts = (raw_args or "").strip().split(maxsplit=1)
    sub = parts[0].lower() if parts and parts[0] else "status"
    rest = parts[1].strip() if len(parts) > 1 else ""
    engine = sa_engine.build_engine(ctx)

    if sub in ("status", "list"):
        return sa_cli.render_status(engine.status())

    if sub in ("tick", "poll"):
        result = engine.tick(launch=False)
        lines = [
            f"polled={result.polled} created={len(result.created)} "
            f"finished={len(result.finished)} waiting={result.waiting}"
        ]
        lines.extend(f"note: {warning}" for warning in result.warnings)
        if result.created:
            lines.append("new: " + ", ".join(result.created))
        return "\n".join(lines)

    if sub == "ask":
        if not rest:
            return "Usage: /superagent ask <what to do>"
        ticket = engine.ask(rest, requester="slash")
        return f"{ticket['id']} recorded as {ticket['status']} — {ticket['reason']}"

    if sub in ("run", "approve"):
        if not rest:
            return "Usage: /superagent run <ticket-id>"
        try:
            ticket = engine.promote(rest)
        except Exception as exc:
            return f"Cannot run {rest}: {exc}"
        note = "worker launched" if ticket.get("launched") else f"queued ({ticket.get('error') or 'starts on the next tick'})"
        return f"{ticket['id']} {ticket['status']} — {note}"

    if sub == "help":
        return "Usage: /superagent [status|tick|ask <request>|run <id>]"

    return f"Unknown subcommand '{sub}'. Try /superagent help."
