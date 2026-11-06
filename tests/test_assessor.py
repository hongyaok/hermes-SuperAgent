def test_acknowledgement_is_ignored(sa):
    decision = sa.sa_assessor.assess("thanks!", explicit=True)
    assert decision.verdict == sa.sa_assessor.IGNORE


def test_skip_marker_wins(sa):
    decision = sa.sa_assessor.assess("@agent ignore this, just thinking aloud", explicit=True)
    assert decision.verdict == sa.sa_assessor.IGNORE


def test_design_question_needs_a_human(sa):
    decision = sa.sa_assessor.assess("Should we split the parser into two modules?", explicit=True)
    assert decision.verdict == sa.sa_assessor.NEEDS_HUMAN


def test_explicit_actionable_request_is_accepted(sa):
    decision = sa.sa_assessor.assess("Fix the crash in parser.py when the file is empty", explicit=True)
    assert decision.verdict == sa.sa_assessor.ACCEPT
    assert "actionable" in decision.reason


def test_unaddressed_work_is_ignored_when_proactive_is_off(sa):
    decision = sa.sa_assessor.assess("we should fix the parser crash", explicit=False, allow_proactive=False)
    assert decision.verdict == sa.sa_assessor.IGNORE


def test_unaddressed_work_is_accepted_when_proactive_is_on(sa):
    decision = sa.sa_assessor.assess("we should fix the parser crash", explicit=False, allow_proactive=True)
    assert decision.verdict == sa.sa_assessor.ACCEPT


def test_empty_and_short_messages_are_ignored(sa):
    assert sa.sa_assessor.assess("", explicit=True).verdict == sa.sa_assessor.IGNORE
    assert sa.sa_assessor.assess("ok", explicit=True).verdict == sa.sa_assessor.IGNORE


def test_stack_trace_counts_as_actionable(sa):
    trace = "Traceback (most recent call last):\n  File \"x.py\", line 3\nValueError: boom"
    assert sa.sa_assessor.assess(trace, explicit=True).verdict == sa.sa_assessor.ACCEPT
