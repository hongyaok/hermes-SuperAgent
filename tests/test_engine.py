import time


def make_settings(sa, **overrides):
    base = dict(mode="observe", max_workers=2, max_runtime_seconds=60)
    base.update(overrides)
    return sa.sa_engine.Settings(**base)


def test_observe_mode_records_without_launching(sa, store, fake_executor):
    engine = sa.sa_engine.Engine(store, make_settings(sa), executor=fake_executor)
    ticket = engine.ask("Fix the crash in parser.py")
    assert ticket["status"] == sa.sa_engine.OBSERVED
    assert fake_executor.launched == []


def test_auto_mode_launches_then_reviews(sa, store, fake_executor):
    engine = sa.sa_engine.Engine(store, make_settings(sa, mode="auto"), executor=fake_executor)
    ticket = engine.ask("Fix the crash in parser.py")
    assert ticket["status"] == sa.sa_engine.QUEUED

    engine.tick()
    assert store.get(ticket["id"])["status"] == sa.sa_engine.RUNNING
    assert len(fake_executor.launched) == 1

    fake_executor.state = "SUCCEEDED"
    engine.tick()
    finished = store.get(ticket["id"])
    assert finished["status"] == sa.sa_engine.REVIEW
    assert finished["result"] == "worker summary"


def test_capacity_limits_parallel_workers(sa, store, fake_executor):
    engine = sa.sa_engine.Engine(store, make_settings(sa, mode="auto", max_workers=1), executor=fake_executor)
    first = engine.ask("Fix the parser crash")
    second = engine.ask("Add a regression test for the parser")

    engine.tick()
    assert store.get(first["id"])["status"] == sa.sa_engine.RUNNING
    assert store.get(second["id"])["status"] == sa.sa_engine.QUEUED
    assert len(fake_executor.launched) == 1


def test_promote_starts_an_observed_ticket(sa, store, fake_executor):
    engine = sa.sa_engine.Engine(store, make_settings(sa), executor=fake_executor)
    ticket = engine.ask("Fix the parser crash")
    assert ticket["status"] == sa.sa_engine.OBSERVED

    promoted = engine.promote(ticket["id"])
    assert promoted["status"] == sa.sa_engine.RUNNING
    assert promoted["launched"] is True


def test_watchdog_cancels_a_stuck_worker(sa, store, fake_executor):
    engine = sa.sa_engine.Engine(store, make_settings(sa, mode="auto"), executor=fake_executor)
    ticket = engine.ask("Fix the parser crash")
    engine.tick()
    store.update(ticket["id"], started_at=time.time() - 120)

    engine.tick()
    assert store.get(ticket["id"])["status"] == sa.sa_engine.FAILED
    assert fake_executor.cancelled


def test_launch_failure_keeps_the_ticket_queued(sa, store, fake_executor):
    fake_executor.error = "No active Hermes parent session is available."
    engine = sa.sa_engine.Engine(store, make_settings(sa, mode="auto"), executor=fake_executor)
    ticket = engine.ask("Fix the parser crash")
    engine.tick()
    kept = store.get(ticket["id"])
    assert kept["status"] == sa.sa_engine.QUEUED
    assert "parent session" in kept["error"]


def test_github_requests_are_deduped(sa, store, fake_executor):
    class FakeGitHub:
        def __init__(self, inbound):
            self.inbound = inbound
            self.replies = []

        def poll(self, since=None, limit=30):
            return list(self.inbound), "cursor-1"

        def reply(self, issue_number, body):
            self.replies.append((issue_number, body))

    inbound = sa.sa_sources.Inbound(
        source="github",
        source_ref="github:42",
        request="Fix the parser crash",
        requester="alice",
        explicit=True,
        repo="o/r",
        issue_number=7,
    )
    engine = sa.sa_engine.Engine(store, make_settings(sa), executor=fake_executor, github=FakeGitHub([inbound]))

    engine.tick()
    engine.tick()
    assert len(store.list()) == 1
    assert store.list()[0]["source"] == "github"


def test_manual_promotion_in_observe_mode_launches_on_next_tick_and_allows_rerun(sa, store, fake_executor):
    fake_executor.error = "No active Hermes parent session is available."
    engine = sa.sa_engine.Engine(store, make_settings(sa, mode="observe"), executor=fake_executor)
    ticket = engine.ask("Fix the parser crash")
    assert ticket["status"] == sa.sa_engine.OBSERVED

    # Initial manual promotion fails outside an agent turn, leaving ticket queued with mode="manual".
    first_try = engine.promote(ticket["id"])
    assert first_try["status"] == sa.sa_engine.QUEUED
    assert first_try["mode"] == "manual"
    assert first_try["launched"] is False

    # Re-promoting an already-queued ticket does not raise RuntimeError.
    second_try = engine.promote(ticket["id"])
    assert second_try["status"] == sa.sa_engine.QUEUED
    assert second_try["launched"] is False

    # Next tick inside an agent turn launches the manual ticket even in observe mode.
    fake_executor.error = None
    tick = engine.tick()
    assert tick.launched == [ticket["id"]]
    assert store.get(ticket["id"])["status"] == sa.sa_engine.RUNNING


def test_doctor_checks_github_source_token(sa, store, fake_executor):
    github = sa.sa_sources.GitHubSource(repo="owner/repo", token="ghp_env_token", agent_login="hermes-bot")
    cfg = make_settings(sa, github_repo="owner/repo", github_agent_login="hermes-bot", github_token="")
    engine = sa.sa_engine.Engine(store, cfg, executor=fake_executor, github=github)
    assert engine.doctor()["github_configured"] is True

