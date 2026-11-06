"""The Router's judgement: should this message become work?

Deliberately deterministic. DeepSeek's prototype gates every action behind an
allowed requester and an "is this actually a request" check; a small rule set is
easy to audit, easy to test, and costs no tokens. An LLM assessor can be added
later behind the same ``assess()`` contract.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

ACCEPT = "accept"
OBSERVE = "observe"
IGNORE = "ignore"
NEEDS_HUMAN = "needs_human"

_SKIP_MARKERS = ("[skip-agent]", "[no-agent]", "@agent ignore", "/noagent")
_THANKS = re.compile(
    r"^(thanks|thank you|thx|ty|lgtm|looks good|nice|great|ok(ay)?|\+1|:?\+1:|👍)[\s.!]*$",
    re.IGNORECASE,
)
_DESIGN_QUESTION = re.compile(
    r"\b(should we|what do you think|which approach|your opinion|design|architecture|"
    r"why does|why is|how come|thoughts\?)\b",
    re.IGNORECASE,
)
_ACTION = re.compile(
    r"\b(fix|add|implement|update|change|refactor|rename|remove|delete|bump|upgrade|"
    r"migrate|test|cover|document|investigate|debug|reproduce|patch|handle|support|"
    r"make|ensure|check|review|write|create|split|move|clean\s?up)\b",
    re.IGNORECASE,
)
_CODE_HINT = re.compile(
    r"```|Traceback|(?:^|\s)\w*Error:|Exception|at [\w.]+\(|\.py:\d+|\.ts:\d+",
    re.IGNORECASE | re.MULTILINE,
)


@dataclass(frozen=True)
class Decision:
    verdict: str
    reason: str
    confidence: float


def assess(
    request: str,
    *,
    explicit: bool,
    allow_proactive: bool = False,
) -> Decision:
    """Classify one inbound message.

    ``explicit`` means a human addressed the agent directly (a local ask or an
    @mention). ``allow_proactive`` is the team's written rule that lets the
    agent pick up work nobody addressed to it.
    """
    text = (request or "").strip()
    if not text:
        return Decision(IGNORE, "empty message", 1.0)
    if len(text) < 8:
        return Decision(IGNORE, "too short to be a request", 0.9)

    lowered = text.lower()
    if any(marker in lowered for marker in _SKIP_MARKERS):
        return Decision(IGNORE, "opted out with a skip marker", 1.0)
    if _THANKS.match(text):
        return Decision(IGNORE, "acknowledgement, not a request", 0.95)

    has_action = bool(_ACTION.search(text))
    has_code = bool(_CODE_HINT.search(text))
    looks_like_question = bool(_DESIGN_QUESTION.search(text))

    if looks_like_question and not has_code:
        return Decision(NEEDS_HUMAN, "looks like a question or design decision", 0.7)

    if explicit:
        # A person addressed the agent on purpose; trust the intent, but keep
        # the reason visible in the panel.
        if has_action or has_code:
            return Decision(ACCEPT, "explicit request with an actionable verb", 0.9)
        return Decision(ACCEPT, "explicit request from an allowed person", 0.75)

    if not allow_proactive:
        return Decision(IGNORE, "not addressed to the agent (proactive work is off)", 0.9)

    if has_action or has_code:
        return Decision(ACCEPT, "matches the team's proactive rule", 0.7)
    return Decision(NEEDS_HUMAN, "no clear work to do", 0.5)
