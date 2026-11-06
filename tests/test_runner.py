"""Tests for the headless chat runner (sa_runner.py).

No real `hermes` process is ever spawned: a fake Popen replays stream-json
lines, which keeps the suite fast and hermetic.
"""

from __future__ import annotations

import json
import threading
import time
from types import SimpleNamespace

import pytest

from sa_plugin import sa_chats, sa_runner


class FakeProcess:
    """Stands in for subprocess.Popen: canned stdout, no real child.

    ``block`` keeps stdout open until the test releases it, which is how the
    stop() and worker-cap tests observe a turn that is still running.
    """

    def __init__(self, lines=(), *, returncode=0, stderr=(), block=None, pid=4242):
        self.pid = pid
        self.returncode = returncode
        self.killed = False
        self.closed = False
        self._lines = list(lines)
        self._block = block
        self._done = threading.Event()
        self.stdout = self._stdout()
        self.stderr = list(stderr)

    def _stdout(self):
        try:
            for line in self._lines:
                yield line
            if self._block is not None:
                self._block.wait(5)
        finally:
            self.closed = True
            self._done.set()

    def poll(self):
        return self.returncode if self._done.is_set() else None

    def wait(self, timeout=None):
        self._done.wait(timeout)
        return self.returncode

    def kill(self):
        self.killed = True
        self.returncode = -9
        if self._block is not None:
            self._block.set()

    terminate = kill


def wait_until(predicate, timeout=5.0):
    """Poll ``predicate`` until it is true (keeps tests fast and stable)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def jsonl(*events):
    """Stream-json lines plus the plain summary line the CLI prints last."""
    return [json.dumps(event) + "\n" for event in events] + ["session_id: local-ignored\n"]


def make_runner(chat, **overrides):
    options = {
        "hermes_bin": "hermes",
        "max_workers": 1,
        "toolsets": ("file", "terminal"),
        "yolo": True,
        "max_turns": 7,
    }
    options.update(overrides)
    return sa_runner.Runner(chat, **options)


@pytest.fixture
def popen(monkeypatch):
    """Replace Popen with a queue of fake processes and record every argv."""
    state = SimpleNamespace(calls=[], procs=[])

    def install(*processes):
        state.procs.extend(processes)

    def fake_popen(argv, **kwargs):
        state.calls.append({"argv": argv, "kwargs": kwargs})
        if not state.procs:
            raise AssertionError("no fake process queued for this launch")
        return state.procs.pop(0)

    monkeypatch.setattr(sa_runner.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(sa_runner, "_terminate_tree", lambda proc: proc.kill())
    state.install = install
    return state


def status_of(chat, session_id):
    return chat.get_session(session_id, message_limit=0)["status"]


# -- parse_event ---------------------------------------------------------


def test_parse_event_reads_json_objects():
    assert sa_runner.parse_event('{"type": "text", "text": "OK"}\n') == {
        "type": "text",
        "text": "OK",
    }


def test_parse_event_ignores_junk_lines():
    for line in ["", "   ", "session_id: local-abc", "42", "[1, 2]", "{not json}", "null"]:
        assert sa_runner.parse_event(line) is None
    assert sa_runner.parse_event(None) is None  # type: ignore[arg-type]


# -- prompts -------------------------------------------------------------


def test_build_prompt_states_the_worker_rules():
    prompt = sa_runner.build_prompt(
        "Fix the parser crash", repo="C:/repo", session_id="S-0001", source="desktop"
    )
    assert "Fix the parser crash" in prompt
    assert "C:/repo" in prompt
    assert "S-0001" in prompt
    assert "Do NOT commit, push" in prompt
    assert "200 words" in prompt
    assert "Work only inside the repository" in prompt


# -- command construction ------------------------------------------------


def test_launch_builds_the_headless_command(chat, popen, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    popen.install(FakeProcess(jsonl({"type": "result", "session_id": "local-1", "exit_code": 0})))
    runner = make_runner(
        chat, model="deepseek-v4.1-flash", provider="opencode-go", repo=str(repo), max_turns=9
    )
    session = chat.create_session("fix it", repo=str(repo))

    runner.launch_turn(session["id"], "fix it")
    assert wait_until(lambda: status_of(chat, session["id"]) == "review")

    argv = popen.calls[0]["argv"]
    assert argv[:2] == ["hermes", "chat"]
    assert argv[argv.index("-q") + 1].startswith("You are one Superagent worker session")
    assert argv[argv.index("--source") + 1] == "tool"  # gateway-supported tag
    assert argv[argv.index("-t") + 1] == "file,terminal"
    assert argv[argv.index("--format") + 1] == "stream-json"
    assert argv[argv.index("--max-turns") + 1] == "9"
    assert argv[argv.index("-m") + 1] == "deepseek-v4.1-flash"
    assert argv[argv.index("--provider") + 1] == "opencode-go"
    assert argv[argv.index("--in") + 1] == str(repo)
    assert "--yolo" in argv and "-Q" in argv
    assert "--resume" not in argv

    kwargs = popen.calls[0]["kwargs"]
    assert kwargs["text"] is True
    assert kwargs["encoding"] == "utf-8"
    assert kwargs["errors"] == "replace"
    assert kwargs["stdin"] == sa_runner.subprocess.DEVNULL
    assert kwargs["cwd"] == str(repo)
    assert "shell" not in kwargs


def test_resume_turn_sends_the_raw_message(chat, popen):
    popen.install(FakeProcess(jsonl({"type": "result", "session_id": "local-2"})))
    runner = make_runner(chat)
    session = chat.create_session("first")
    chat.update_session(session["id"], hermes_session_id="local-2")

    runner.launch_turn(session["id"], "what next?", resume_session_id="local-2")
    assert wait_until(lambda: status_of(chat, session["id"]) == "review")

    argv = popen.calls[0]["argv"]
    assert argv[argv.index("-q") + 1] == "what next?"
    assert argv[-2:] == ["--resume", "local-2"]


def test_yolo_can_be_disabled(chat, popen):
    popen.install(FakeProcess(jsonl({"type": "result", "session_id": "local-3"})))
    runner = make_runner(chat, yolo=False)
    session = chat.create_session("work")
    runner.launch_turn(session["id"], "work")
    assert wait_until(lambda: status_of(chat, session["id"]) == "review")
    assert "--yolo" not in popen.calls[0]["argv"]


def test_source_tag_is_configurable(chat, popen, monkeypatch):
    monkeypatch.setenv("SUPERAGENT_CHAT_SOURCE", "cli")
    popen.install(FakeProcess(jsonl({"type": "result", "session_id": "local-4"})))
    runner = make_runner(chat)
    assert runner.source_tag == "cli"
    session = chat.create_session("work")
    runner.launch_turn(session["id"], "work")
    assert wait_until(lambda: status_of(chat, session["id"]) == "review")
    argv = popen.calls[0]["argv"]
    assert argv[argv.index("--source") + 1] == "cli"


def test_missing_hermes_binary_raises(chat, monkeypatch):
    monkeypatch.delenv("SUPERAGENT_HERMES_BIN", raising=False)
    monkeypatch.setattr(sa_runner.shutil, "which", lambda name: None)
    runner = sa_runner.Runner(chat, hermes_bin="")
    session = chat.create_session("work")
    with pytest.raises(RuntimeError) as excinfo:
        runner.launch_turn(session["id"], "work")
    assert "hermes" in str(excinfo.value)


def test_missing_repo_raises(chat, tmp_path):
    runner = make_runner(chat, repo=str(tmp_path / "does-not-exist"))
    session = chat.create_session("work")
    with pytest.raises(RuntimeError) as excinfo:
        runner.launch_turn(session["id"], "work")
    assert "repository path" in str(excinfo.value)


# -- turn bookkeeping ----------------------------------------------------


def test_turn_records_messages_events_and_metrics(chat, popen):
    popen.install(
        FakeProcess(
            jsonl(
                {"type": "system", "subtype": "init", "model": "deepseek-v4.1-flash", "session_id": ""},
                {"type": "text", "text": "Working on it"},
                {"type": "tool", "tool": "read_file", "args": {"path": "sa_store.py"}},
                {
                    "type": "result",
                    "session_id": "local-9",
                    "exit_code": 0,
                    "tokens": {"input": 5, "output": 7, "total": 12},
                    "duration_ms": 34,
                },
            )
        )
    )
    runner = make_runner(chat)
    session = chat.create_session("fix it")

    runner.launch_turn(session["id"], "fix it")
    assert wait_until(lambda: status_of(chat, session["id"]) == "review")

    detail = chat.get_session(session["id"])
    assert detail["status"] == "review"
    assert detail["hermes_session_id"] == "local-9"
    assert detail["tokens"] == 12
    assert detail["duration_ms"] == 34
    assert detail["pid"] is None
    assert detail["started_at"] and detail["finished_at"]
    assert [message["content"] for message in detail["messages"]] == ["Working on it"]
    kinds = [event["kind"] for event in detail["events"]]
    assert "tool" in kinds and "result" in kinds and "review" in kinds
    tool = next(event for event in detail["events"] if event["kind"] == "tool")
    assert tool["detail"].startswith("read_file:")
    assert runner.active() == []


def test_failed_turn_records_stderr(chat, popen):
    popen.install(
        FakeProcess(jsonl({"type": "text", "text": "starting"}), returncode=2, stderr=["boom: no such flag\n"])
    )
    runner = make_runner(chat)
    session = chat.create_session("fix it")

    runner.launch_turn(session["id"], "fix it")
    assert wait_until(lambda: status_of(chat, session["id"]) == "failed")

    detail = chat.get_session(session["id"], message_limit=0)
    assert "boom" in detail["error"]
    assert detail["pid"] is None


def test_unknown_event_shapes_are_ignored(chat, popen):
    popen.install(
        FakeProcess(
            jsonl(
                {"type": "tool"},
                {"type": "text"},
                {"type": "assistant", "message": {"content": [{"text": "a"}, {"text": "b"}]}},
                {"type": "result"},
                {"type": "mystery", "payload": {"x": 1}},
                {"foo": "bar"},
                "not json at all\n",
            )
        )
    )
    runner = make_runner(chat)
    session = chat.create_session("fix it")

    runner.launch_turn(session["id"], "fix it")
    assert wait_until(lambda: status_of(chat, session["id"]) == "review")
    assert [m["content"] for m in chat.get_messages(session["id"])] == ["a\nb"]


def test_text_deltas_become_one_assistant_message(chat, popen):
    popen.install(
        FakeProcess(
            jsonl(
                {"type": "text", "text": "Hel"},
                {"type": "text", "text": "lo "},
                {"type": "text", "text": "there"},
                {"type": "result", "session_id": "local-5", "text": "Hello there", "exit_code": 0},
            )
        )
    )
    runner = make_runner(chat)
    session = chat.create_session("greet me")

    runner.launch_turn(session["id"], "greet me")
    assert wait_until(lambda: status_of(chat, session["id"]) == "review")
    assert [m["content"] for m in chat.get_messages(session["id"])] == ["Hello there"]


def test_tool_use_and_tool_result_events_are_recorded(chat, popen):
    popen.install(
        FakeProcess(
            jsonl(
                {"type": "tool_use", "name": "read_file", "input": {"path": "sa_store.py"}},
                {"type": "tool_result", "name": "read_file", "is_error": True, "output": "nope"},
                {"type": "result", "session_id": "local-6"},
            )
        )
    )
    runner = make_runner(chat)
    session = chat.create_session("read a file")

    runner.launch_turn(session["id"], "read a file")
    assert wait_until(lambda: status_of(chat, session["id"]) == "review")
    events = chat.get_events(session["id"])
    tool = next(event for event in events if event["kind"] == "tool")
    assert tool["detail"].startswith("read_file:")
    assert "sa_store.py" in tool["detail"]
    assert any(event["kind"] == "tool_error" for event in events)


def test_session_id_is_recovered_from_stderr(chat, popen):
    popen.install(
        FakeProcess(
            jsonl({"type": "result"}),
            returncode=1,
            stderr=["Error: boom\n", "\nsession_id: local-abc\n"],
        )
    )
    runner = make_runner(chat)
    session = chat.create_session("fail")

    runner.launch_turn(session["id"], "fail")
    assert wait_until(lambda: status_of(chat, session["id"]) == "failed")
    detail = chat.get_session(session["id"], message_limit=0)
    assert detail["hermes_session_id"] == "local-abc"
    assert detail["error"] == "Error: boom"


def test_result_error_wins_over_stderr_boilerplate(chat, popen):
    popen.install(
        FakeProcess(
            jsonl(
                {
                    "type": "result",
                    "session_id": "local-7",
                    "exit_code": 1,
                    "error": "max turns reached",
                }
            ),
            returncode=1,
            stderr=["Session: local-7\n", "\nsession_id: local-7\n"],
        )
    )
    runner = make_runner(chat)
    session = chat.create_session("fail")

    runner.launch_turn(session["id"], "fail")
    assert wait_until(lambda: status_of(chat, session["id"]) == "failed")
    detail = chat.get_session(session["id"], message_limit=0)
    assert detail["error"] == "max turns reached"
    assert detail["hermes_session_id"] == "local-7"


def test_stop_marks_the_session_cancelled(chat, popen):
    block = threading.Event()
    proc = FakeProcess(jsonl({"type": "system", "subtype": "init"}), block=block)
    popen.install(proc)
    runner = make_runner(chat)
    session = chat.create_session("long job")

    runner.launch_turn(session["id"], "long job")
    assert wait_until(lambda: status_of(chat, session["id"]) == "running")
    assert len(runner.active()) == 1

    assert runner.stop(session["id"]) is True
    assert wait_until(lambda: proc.closed)
    assert status_of(chat, session["id"]) == "cancelled"
    assert proc.killed
    assert runner.active() == []


def test_stop_on_an_unknown_process_still_marks_cancelled(chat, popen):
    runner = make_runner(chat)
    session = chat.create_session("idle one")
    chat.update_session(session["id"], status="running")
    assert runner.stop(session["id"]) is False
    assert status_of(chat, session["id"]) == "cancelled"


def test_worker_cap_limits_parallel_turns(chat, popen):
    first_block = threading.Event()
    first = FakeProcess(jsonl({"type": "text", "text": "first"}), block=first_block)
    second = FakeProcess(jsonl({"type": "text", "text": "second"}))
    popen.install(first, second)
    runner = make_runner(chat, max_workers=1)

    a = chat.create_session("a")
    b = chat.create_session("b")
    runner.launch_turn(a["id"], "a")
    assert wait_until(lambda: len(runner.active()) == 1)

    runner.launch_turn(b["id"], "b")
    time.sleep(0.05)
    assert status_of(chat, b["id"]) == "queued"
    assert len(runner.active()) == 1
    assert len(popen.calls) == 1  # b has not started a process yet

    first_block.set()
    assert wait_until(lambda: status_of(chat, a["id"]) == "review")
    assert wait_until(lambda: status_of(chat, b["id"]) == "review")


def test_runner_accepts_a_plain_store(tmp_path, sa):
    store = sa.sa_store.Store(tmp_path / "superagent.db")
    runner = sa_runner.Runner(store, hermes_bin="hermes")
    session = runner.chat.create_session("hi")
    assert sa_chats.ChatStore(tmp_path / "superagent.db").get_session(session["id"]) is not None


def test_blocking_launch_runs_the_turn_before_returning(chat, popen, tmp_path):
    """block=True executes inline: the session is finished when the call returns.

    This is what keeps `hermes superagent chat` from launching into a daemon
    thread that dies with the CLI process.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    popen.install(FakeProcess(jsonl(
        {"type": "text", "text": "pong"},
        {"type": "result", "session_id": "local-1", "exit_code": 0, "usage": {"total_tokens": 12}},
    )))
    runner = make_runner(chat, repo=str(repo))
    session = chat.create_session("say pong", repo=str(repo))

    result = runner.launch_turn(session["id"], "say pong", block=True)

    assert result["blocked"] is True
    assert status_of(chat, session["id"]) == "review"  # finished, not queued
    detail = chat.get_session(session["id"], message_limit=10)
    assert any(m["role"] == "assistant" and "pong" in (m["content"] or "") for m in detail["messages"])
