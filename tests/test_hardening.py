"""Hardening tests: repo containment, redaction, caps, triage and the watchdog.

The runner is exercised with a fake Popen (no real ``hermes`` process), and the
dashboard backend is loaded standalone exactly the way the gateway loads it.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from sa_plugin import sa_chats, sa_runner

ROOT = Path(__file__).resolve().parents[1]


# -- fixtures ------------------------------------------------------------


def load_api():
    path = ROOT / "dashboard" / "plugin_api.py"
    spec = importlib.util.spec_from_file_location("superagent_hardening_api", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["superagent_hardening_api"] = module
    spec.loader.exec_module(module)
    return module


class FakeProcess:
    """Stands in for subprocess.Popen: canned stdout, no real child."""

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


def jsonl(*events):
    return [json.dumps(event) + "\n" for event in events] + ["session_id: local-ignored\n"]


def wait_until(predicate, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


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
    state = SimpleNamespace(calls=[], procs=[])

    def fake_popen(argv, **kwargs):
        state.calls.append({"argv": argv, "kwargs": kwargs})
        if not state.procs:
            raise AssertionError("no fake process queued for this launch")
        return state.procs.pop(0)

    monkeypatch.setattr(sa_runner.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(sa_runner, "_terminate_tree", lambda proc: proc.kill())
    state.install = lambda *processes: state.procs.extend(processes)
    return state


def status_of(chat, session_id):
    return chat.get_session(session_id, message_limit=0)["status"]


@pytest.fixture
def api(tmp_path, monkeypatch):
    module = load_api()
    monkeypatch.setattr(module, "_db_path", lambda: tmp_path / "superagent.db")
    return module


class RecordingRunner:
    """Records launches/stops/reaps; mirrors the store writes the real Runner does."""

    def __init__(self, store=None, fail=None):
        self.store = store
        self.fail = fail
        self.launches = []
        self.stopped = []

    def launch_turn(
        self, session_id, prompt, *, resume_session_id=None, model=None, provider=None, repo=None
    ):
        if self.fail:
            raise RuntimeError(self.fail)
        self.launches.append(
            {
                "session_id": session_id,
                "prompt": prompt,
                "resume_session_id": resume_session_id,
                "model": model,
                "provider": provider,
                "repo": repo,
            }
        )
        if self.store is not None:
            self.store.update_session(session_id, status="running", pid=4321)
        return {"session_id": session_id}

    def stop(self, session_id):
        self.stopped.append(session_id)
        if self.store is not None:
            self.store.update_session(
                session_id, status="cancelled", error="cancelled by user", pid=None
            )
        return True

    def reap(self):
        return []


@pytest.fixture
def chat_api(api, monkeypatch):
    runner = RecordingRunner(store=api.sa_chats.ChatStore(api._db_path()))
    monkeypatch.setattr(api, "_get_runner", lambda: runner)
    return api, runner


# -- repository containment ---------------------------------------------


def test_resolve_repo_path_rejects_a_missing_directory(tmp_path):
    with pytest.raises(ValueError) as excinfo:
        sa_runner.resolve_repo_path(str(tmp_path / "does-not-exist"))
    assert "does not exist" in str(excinfo.value)
    # Historically launch_turn raised RuntimeError; keep that contract too.
    assert isinstance(excinfo.value, RuntimeError)


def test_resolve_repo_path_rejects_a_file(tmp_path):
    path = tmp_path / "repo.txt"
    path.write_text("not a repo", encoding="utf-8")
    with pytest.raises(ValueError) as excinfo:
        sa_runner.resolve_repo_path(str(path))
    assert "not a directory" in str(excinfo.value)


def test_resolve_repo_path_rejects_dot_dot(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    with pytest.raises(ValueError) as excinfo:
        sa_runner.resolve_repo_path(str(root / ".." / "elsewhere"))
    assert ".." in str(excinfo.value)


def _make_dir_link(link: Path, target: Path) -> bool:
    """Create a directory symlink, falling back to a Windows junction."""
    try:
        os.symlink(target, link, target_is_directory=True)
        return True
    except (OSError, NotImplementedError):
        pass
    if os.name == "nt":
        import subprocess

        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True,
            text=True,
        )
        return result.returncode == 0 and link.is_dir()
    return False


def test_resolve_repo_path_rejects_a_symlink_escape(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    link = root / "link"
    if not _make_dir_link(link, outside):
        pytest.skip("this host cannot create directory links")
    with pytest.raises(ValueError) as excinfo:
        sa_runner.resolve_repo_path(str(link), root=str(root))
    assert "escapes" in str(excinfo.value)


def test_resolve_repo_path_accepts_subdirectories_and_the_root(tmp_path):
    root = tmp_path / "root"
    child = root / "repo"
    child.mkdir(parents=True)
    assert sa_runner.resolve_repo_path(str(child), root=str(root)) == str(child.resolve())
    assert sa_runner.resolve_repo_path(str(root), root=str(root)) == str(root.resolve())


def test_launch_passes_only_the_resolved_repo_to_argv(chat, popen, tmp_path):
    root = tmp_path / "root"
    child = root / "repo"
    child.mkdir(parents=True)
    popen.install(FakeProcess(jsonl({"type": "result", "session_id": "local-p"})))
    runner = make_runner(chat, repo=str(root))
    session = chat.create_session("work")

    runner.launch_turn(session["id"], "work", repo=str(child))
    assert wait_until(lambda: status_of(chat, session["id"]) == "review")

    call = popen.calls[0]
    assert call["argv"][call["argv"].index("--in") + 1] == str(child.resolve())
    assert call["kwargs"]["cwd"] == str(child.resolve())


def test_launch_rejects_a_repo_outside_the_configured_root(chat, popen, tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    runner = make_runner(chat, repo=str(root))
    session = chat.create_session("work")

    with pytest.raises(ValueError) as excinfo:
        runner.launch_turn(session["id"], "work", repo=str(outside))
    assert "escapes" in str(excinfo.value)
    assert popen.calls == []  # nothing was ever spawned


def test_create_chat_maps_bad_repos_to_400(chat_api, tmp_path, monkeypatch):
    from fastapi import HTTPException

    api, runner = chat_api
    with pytest.raises(HTTPException) as missing:
        api.create_chat({"prompt": "fix it", "repo": str(tmp_path / "gone")})
    assert missing.value.status_code == 400
    assert "does not exist" in missing.value.detail

    with pytest.raises(HTTPException) as traversal:
        api.create_chat({"prompt": "fix it", "repo": str(tmp_path / "gone" / ".." / "x")})
    assert traversal.value.status_code == 400
    assert ".." in traversal.value.detail

    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    monkeypatch.setenv("SUPERAGENT_REPO", str(root))
    with pytest.raises(HTTPException) as escaped:
        api.create_chat({"prompt": "fix it", "repo": str(outside)})
    assert escaped.value.status_code == 400
    assert "escapes" in escaped.value.detail
    assert runner.launches == []


def test_post_message_maps_a_deleted_repo_to_400(chat_api, tmp_path):
    from fastapi import HTTPException

    api, _ = chat_api
    repo = tmp_path / "repo"
    repo.mkdir()
    created = api.create_chat({"prompt": "first", "repo": str(repo)})["session"]
    store = api.sa_chats.ChatStore(api._db_path())
    store.update_session(created["id"], status="idle", hermes_session_id="local-1")
    repo.rmdir()

    with pytest.raises(HTTPException) as excinfo:
        api.post_chat_message(created["id"], {"content": "and now?"})
    assert excinfo.value.status_code == 400
    assert "does not exist" in excinfo.value.detail


# -- prompt and response caps -------------------------------------------


def test_prompt_cap_is_20k_everywhere(chat, popen, chat_api):
    api, _ = chat_api
    assert sa_runner.MAX_PROMPT_CHARS == 20000 == api.MAX_PROMPT_CHARS
    assert api.MAX_CONTENT_CHARS == 20000

    runner = make_runner(chat)
    session = chat.create_session("work")
    with pytest.raises(ValueError) as excinfo:
        runner.launch_turn(session["id"], "x" * 20001)
    assert "too long" in str(excinfo.value)
    assert popen.calls == []

    from fastapi import HTTPException

    assert api.create_chat({"prompt": "x" * 20000})["ok"] is True
    with pytest.raises(HTTPException) as excinfo:
        api.create_chat({"prompt": "x" * 20001})
    assert excinfo.value.status_code == 400


def test_assistant_text_is_stored_capped_at_20k(chat, popen):
    popen.install(
        FakeProcess(jsonl({"type": "text", "text": "y" * 30000}, {"type": "result", "session_id": "local-big"}))
    )
    runner = make_runner(chat)
    session = chat.create_session("big")
    runner.launch_turn(session["id"], "big")
    assert wait_until(lambda: status_of(chat, session["id"]) == "review")

    messages = chat.get_messages(session["id"])
    assert len(messages) == 1
    assert len(messages[0]["content"]) == sa_runner.MAX_MESSAGE_CHARS == 20000


def test_event_detail_is_stored_capped_at_2k(chat, popen):
    popen.install(
        FakeProcess(jsonl({"type": "error", "text": "e" * 5000}, {"type": "result", "session_id": "local-e"}))
    )
    runner = make_runner(chat)
    session = chat.create_session("noisy")
    runner.launch_turn(session["id"], "noisy")
    assert wait_until(lambda: status_of(chat, session["id"]) == "review")

    error = next(event for event in chat.get_events(session["id"]) if event["kind"] == "error")
    assert len(error["detail"]) == sa_runner.MAX_EVENT_CHARS == 2000


# -- secret redaction ----------------------------------------------------


def test_redact_secrets_covers_token_shapes():
    secrets = [
        "ghp_" + "a" * 24,
        "gho_" + "b" * 24,
        "github_pat_" + "c" * 30,
        "sk-" + "d" * 24,
        "xoxb-" + "1234567890-abcdef",
    ]
    for secret in secrets:
        cleaned = sa_runner.redact_secrets(f"token {secret} leaked")
        assert secret not in cleaned
        assert "[redacted]" in cleaned
    assert sa_runner.redact_secrets("nothing to hide") == "nothing to hide"


def test_redact_secrets_covers_authorization_headers():
    text = (
        'Authorization: Bearer abcdef1234567890 and '
        '{"Authorization": "Bearer sk-abcdef123456789"}'
    )
    cleaned = sa_runner.redact_secrets(text)
    assert "abcdef1234567890" not in cleaned
    assert "sk-abcdef123456789" not in cleaned
    assert cleaned.lower().count("authorization") == 2


def test_runner_never_persists_secrets(chat, popen):
    ghp = "ghp_" + "A" * 24
    pat = "github_pat_" + "B" * 30
    slack = "xoxb-" + "1234567890-" + "C" * 10
    sk = "sk-" + "D" * 24
    popen.install(
        FakeProcess(
            jsonl(
                {"type": "text", "text": f"using {ghp} now"},
                {"type": "tool", "tool": "http", "args": {"Authorization": f"Bearer {pat}"}},
                {"type": "error", "text": f"bad key {sk} and {slack}"},
                {"type": "result", "session_id": "local-r"},
            ),
            returncode=1,
            stderr=[f"fatal: Authorization: Bearer {ghp}\n"],
        )
    )
    runner = make_runner(chat)
    session = chat.create_session("leak something")
    runner.launch_turn(session["id"], "leak something")
    assert wait_until(lambda: status_of(chat, session["id"]) == "failed")

    blob = json.dumps(chat.get_session(session["id"]))
    for secret in (ghp, pat, slack, sk):
        assert secret not in blob
    assert "[redacted]" in blob


def test_api_scrubs_chat_prompts_and_tickets(chat_api, api):
    ghp = "ghp_" + "Z" * 24
    chat_api_module, runner = chat_api
    created = chat_api_module.create_chat(
        {"prompt": f"deploy with {ghp}", "title": f"key {ghp}"}
    )["session"]
    assert ghp not in created["prompt"]
    assert ghp not in created["title"]
    assert ghp not in runner.launches[0]["prompt"]

    ticket = api.ask_ticket({"request": f"Fix the parser crash with token {ghp}"})["ticket"]
    detail = api.get_ticket(ticket["id"])
    assert ghp not in json.dumps(detail)


# -- POST /ask triage ----------------------------------------------------


def test_ask_skip_marker_is_ignored(api):
    ticket = api.ask_ticket({"request": "[skip-agent] just thinking about the parser"})["ticket"]
    assert ticket["status"] == "ignored"
    detail = api.get_ticket(ticket["id"])
    assert detail["verdict"] == "ignore"
    assert "opted out" in detail["reason"]
    assessed = next(event for event in detail["events"] if event["kind"] == "assessed")
    assert assessed["detail"].startswith("ignore:")


def test_ask_acknowledgement_is_ignored(api):
    ticket = api.ask_ticket({"request": "thank you!"})["ticket"]
    assert ticket["status"] == "ignored"
    detail = api.get_ticket(ticket["id"])
    assert detail["verdict"] == "ignore"
    assert "acknowledgement" in detail["reason"]


def test_ask_design_question_is_blocked(api):
    ticket = api.ask_ticket({"request": "Should we split the parser into two modules?"})["ticket"]
    assert ticket["status"] == "blocked"
    detail = api.get_ticket(ticket["id"])
    assert detail["verdict"] == "needs_human"
    assert "question" in detail["reason"]
    assessed = next(event for event in detail["events"] if event["kind"] == "assessed")
    assert assessed["detail"].startswith("needs_human:")


def test_ask_actionable_request_stays_observed(api):
    ticket = api.ask_ticket(
        {"request": "Fix the crash in parser.py when the file is empty"}
    )["ticket"]
    assert ticket["status"] == "observed"
    detail = api.get_ticket(ticket["id"])
    assert detail["verdict"] == "accept"
    assert "actionable" in detail["reason"]


def test_ask_keeps_validation_and_brief_shape(api):
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as excinfo:
        api.ask_ticket({"request": "  "})
    assert excinfo.value.status_code == 400
    body = api.ask_ticket({"request": "Fix the crash in parser.py"})
    assert set(body["ticket"]) >= {"id", "status", "request", "reason", "updated_at"}


# -- runaway guard -------------------------------------------------------


def test_turn_timeout_defaults_and_env_override(chat, monkeypatch):
    monkeypatch.delenv("SUPERAGENT_TURN_TIMEOUT_SECONDS", raising=False)
    assert make_runner(chat).turn_timeout_seconds == 1800
    monkeypatch.setenv("SUPERAGENT_TURN_TIMEOUT_SECONDS", "7")
    assert make_runner(chat).turn_timeout_seconds == 7
    monkeypatch.setenv("SUPERAGENT_TURN_TIMEOUT_SECONDS", "nonsense")
    assert make_runner(chat).turn_timeout_seconds == 1800


def test_reap_cancels_an_expired_turn_with_a_reason_event(chat):
    runner = make_runner(chat)
    session = chat.create_session("stuck")
    chat.update_session(session["id"], status="running", started_at=time.time() - 100)

    expired = runner.reap(now=time.time(), timeout_seconds=10)
    assert [row["id"] for row in expired] == [session["id"]]

    detail = chat.get_session(session["id"])
    assert detail["status"] == "cancelled"
    assert "timed out" in detail["error"]
    cancelled = next(event for event in detail["events"] if event["kind"] == "cancelled")
    assert "timed out" in cancelled["detail"]


def test_reap_leaves_recent_turns_alone(chat):
    runner = make_runner(chat)
    session = chat.create_session("fresh")
    chat.update_session(session["id"], status="running", started_at=time.time())

    assert runner.reap() == []
    assert status_of(chat, session["id"]) == "running"


def test_reap_kills_a_live_process_and_keeps_the_timeout_reason(chat, popen):
    block = threading.Event()
    proc = FakeProcess(jsonl({"type": "system", "subtype": "init"}), block=block)
    popen.install(proc)
    runner = make_runner(chat)
    session = chat.create_session("runaway")

    runner.launch_turn(session["id"], "runaway")
    assert wait_until(lambda: status_of(chat, session["id"]) == "running")
    # Age both the process clock and the persisted row past the guard.
    runner._started[session["id"]] = time.time() - 999
    chat.update_session(session["id"], started_at=time.time() - 999)

    expired = runner.reap(timeout_seconds=10)
    assert proc.killed
    assert [row["id"] for row in expired] == [session["id"]]
    assert wait_until(lambda: status_of(chat, session["id"]) == "cancelled")

    detail = chat.get_session(session["id"])
    assert "timed out" in (detail["error"] or "")
    assert any("timed out" in event["detail"] for event in detail["events"])


def test_list_chats_reaps_a_runaway_turn(chat_api, api, monkeypatch):
    module, _ = chat_api
    store = module.sa_chats.ChatStore(module._db_path())
    session = store.create_session("stuck")
    store.update_session(session["id"], status="running", started_at=time.time() - 9999)
    runner = sa_runner.Runner(store, hermes_bin="hermes", turn_timeout_seconds=10)
    monkeypatch.setattr(module, "_get_runner", lambda: runner)

    body = api.list_chats()
    row = next(item for item in body["sessions"] if item["id"] == session["id"])
    assert row["status"] == "cancelled"
    assert "timed out" in (row["last_event"] or "")
