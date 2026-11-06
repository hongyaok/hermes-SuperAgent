"""Tool handlers. Every handler returns a JSON string, never raises."""

from __future__ import annotations

import json
from typing import Any

try:
    from . import sa_engine
except ImportError:  # top-level import (root __init__ loaded as `__init__` by pytest)
    import sa_engine  # type: ignore[no-redef]


def _engine(ctx: Any) -> sa_engine.Engine:
    return sa_engine.build_engine(ctx)


def superagent_ask(args: dict, ctx: Any = None, **kwargs: Any) -> str:
    try:
        if ctx is None:
            return json.dumps({"error": "plugin context unavailable"})
        request = str(args.get("request") or "").strip()
        if not request:
            return json.dumps({"error": "request is required"})
        ticket = _engine(ctx).ask(request, requester=kwargs.get("platform") or "operator")
        return json.dumps({"ticket": sa_engine.Engine._ticket_brief(ticket), "ok": True})
    except Exception as exc:
        return json.dumps({"error": str(exc)})


def superagent_tick(args: dict, ctx: Any = None, **kwargs: Any) -> str:
    try:
        if ctx is None:
            return json.dumps({"error": "plugin context unavailable"})
        engine = _engine(ctx)
        result = engine.tick(wait=bool(args.get("wait", False)))
        status = engine.status()
        return json.dumps({"tick": result.as_dict(), "status": status}, default=str)
    except Exception as exc:
        return json.dumps({"error": str(exc)})


def superagent_status(args: dict, ctx: Any = None, **kwargs: Any) -> str:
    try:
        if ctx is None:
            return json.dumps({"error": "plugin context unavailable"})
        ticket_id = str(args.get("ticket_id") or "").strip() or None
        return json.dumps(_engine(ctx).status(ticket_id), default=str)
    except Exception as exc:
        return json.dumps({"error": str(exc)})


def superagent_run(args: dict, ctx: Any = None, **kwargs: Any) -> str:
    try:
        if ctx is None:
            return json.dumps({"error": "plugin context unavailable"})
        ticket_id = str(args.get("ticket_id") or "").strip()
        if not ticket_id:
            return json.dumps({"error": "ticket_id is required"})
        ticket = _engine(ctx).promote(ticket_id, wait=bool(args.get("wait", False)))
        return json.dumps(
            {
                "ok": True,
                "launched": bool(ticket.get("launched")),
                "ticket": sa_engine.Engine._ticket_brief(ticket),
            },
            default=str,
        )
    except Exception as exc:
        return json.dumps({"error": str(exc)})
