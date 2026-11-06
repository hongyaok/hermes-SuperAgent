"""Worker execution, driven by Hermes subagents.

Each ticket becomes one delegated subagent: its own context, its own terminal
session, and (when ``delegation.worktree_isolation`` is on) its own git
worktree. The plugin never runs the work itself; it only launches, watches and
collects. That mirrors DeepSeek's router/worker split: the router stays thin,
the workers do the work in isolation.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

TERMINAL_STATES = {"SUCCEEDED", "FAILED", "INTERRUPTED", "CANCELLED"}


@dataclass(frozen=True)
class WorkerUpdate:
    state: str
    summary: Optional[str] = None
    error: Optional[str] = None


def build_worker_prompt(*, request: str, requester: str, source: str, ticket_id: str, repo_path: str) -> tuple[str, str]:
    """Return ``(goal, context)`` for one worker subagent."""
    goal = (
        f"Complete Superagent ticket {ticket_id}: {request}\n\n"
        "Work until you can hand back a reviewable result or one concrete blocker."
    )
    context = f"""You are one worker for a Superagent ticket. One ticket, one job.

Requested by: {requester} (via {source})
Ticket: {ticket_id}
Repository: {repo_path or "the current workspace"}

Rules:
- Work only inside the repository. Do not touch files outside it.
- Make the smallest change that satisfies the request; follow the repository's existing style.
- Run the relevant tests or checks. If there are none, say what you checked instead.
- Do NOT commit, push, merge, or open a pull request. Leave changes in the working tree.
- If the request needs a design decision, stop and ask one concrete question rather than guessing.
- Finish with a report of at most 200 words: what changed, files touched, checks run, and anything unresolved.
"""
    return goal, context


class SubagentExecutor:
    """Thin adapter over ``PluginContext.subagent_lifecycle``."""

    name = "subagent"

    def __init__(self, lifecycle: Any, allowed_toolsets: tuple[str, ...] = ("file", "terminal")):
        self.lifecycle = lifecycle
        self.allowed_toolsets = tuple(allowed_toolsets or ("file", "terminal"))

    def available(self) -> tuple[bool, str]:
        if self.lifecycle is None:
            return False, "subagent lifecycle is unavailable in this process"
        return True, ""

    def launch(self, *, goal: str, context: str, correlation_id: str) -> dict:
        """Launch a worker and return the serializable handle."""
        from agent.subagent_lifecycle import SubagentLaunchRequest

        handle = self.lifecycle.launch(
            SubagentLaunchRequest(
                goal=goal,
                context=context,
                role="leaf",
                allowed_toolsets=self.allowed_toolsets,
                correlation_id=correlation_id,
            )
        )
        return handle.to_dict()

    def poll(self, handle: dict) -> WorkerUpdate:
        from agent.subagent_lifecycle import SubagentHandle

        parsed = SubagentHandle.from_dict(handle)
        status = self.lifecycle.status(parsed)
        state = str(getattr(status.state, "value", status.state))
        if state not in TERMINAL_STATES:
            return WorkerUpdate(state)

        result = self.lifecycle.result(parsed)
        summary = getattr(result, "summary", None)
        error = getattr(result, "error_message", None)
        if summary:
            summary = str(summary)[-4000:]
        return WorkerUpdate(state, summary=summary, error=error)

    def cancel(self, handle: dict) -> None:
        from agent.subagent_lifecycle import SubagentHandle

        parsed = SubagentHandle.from_dict(handle)
        self.lifecycle.cancel(parsed, reason="superagent watchdog")
