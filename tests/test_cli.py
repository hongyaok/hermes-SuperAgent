"""CLI tests: run/show plus status/tick/ask/doctor, JSON flags, usage and renders."""

from __future__ import annotations

import argparse
import json
import types

import pytest


def _engine(sa, store, fake_executor, **overrides):
    base = dict(mode="observe", max_workers=2, max_runtime_seconds=60)
    base.update(overrides)
    return sa.sa_engine.Engine(
        store, sa.sa_engine.Settings(**base), executor=fake_executor
    )


def _patch_engine(sa, monkeypatch, engine):
    monkeypatch.setattr(sa.sa_engine, "build_engine", lambda ctx: engine)
    # sa_cli imported sa_engine as a module object; ensure it sees the patch.
    try:
        monkeypatch.setattr(sa.sa_cli.sa_engine, "build_engine", lambda ctx: engine)
    except Exception:
        pass


def _args(**kwargs):
    base = dict(superagent_command="status", json=False)
    base.update(kwargs)
    return types.SimpleNamespace(**base)


def test_run_promotes_observed_ticket(sa, store, fake_executor, monkeypatch, capsys):
    engine = _engine(sa, store, fake_executor)
    _patch_engine(sa, monkeypatch, engine)
    ticket = engine.ask("Fix the parser crash")

    rc = sa.sa_cli.run(_args(superagent_command="run", ticket_id=ticket["id"]), object())
    out = capsys.readouterr().out
    assert rc == 0
    assert ticket["id"] in out
    assert store.get(ticket["id"])["status"] in ("queued", "running")


def test_run_json_includes_launched_flag(sa, store, fake_executor, monkeypatch, capsys):
    engine = _engine(sa, store, fake_executor)
    _patch_engine(sa, monkeypatch, engine)
    ticket = engine.ask("Fix the parser crash")

    rc = sa.sa_cli.run(
        _args(superagent_command="run", ticket_id=ticket["id"], json=True), object()
    )
    out = capsys.readouterr().out
    assert rc == 0
    data = json.loads(out)
    assert data["id"] == ticket["id"]
    assert "launched" in data


def test_run_unknown_ticket_is_error(sa, store, fake_executor, monkeypatch, capsys):
    engine = _engine(sa, store, fake_executor)
    _patch_engine(sa, monkeypatch, engine)

    rc = sa.sa_cli.run(_args(superagent_command="run", ticket_id="T-9999"), object())
    out = capsys.readouterr().out
    assert rc == 1
    assert "error" in out.lower()


def test_show_prints_full_detail_with_events(
    sa, store, fake_executor, monkeypatch, capsys
):
    engine = _engine(sa, store, fake_executor)
    _patch_engine(sa, monkeypatch, engine)
    ticket = engine.ask("Add regression test for parser")
    store.add_event(ticket["id"], "note", "extra history line")

    rc = sa.sa_cli.run(_args(superagent_command="show", ticket_id=ticket["id"]), object())
    out = capsys.readouterr().out
    assert rc == 0
    assert ticket["id"] in out
    assert "Add regression test" in out
    assert "history" in out
    assert "extra history line" in out


def test_status_json_shape(sa, store, fake_executor, monkeypatch, capsys):
    engine = _engine(sa, store, fake_executor)
    _patch_engine(sa, monkeypatch, engine)
    engine.ask("Fix the parser crash")

    rc = sa.sa_cli.run(_args(superagent_command="status", json=True), object())
    out = capsys.readouterr().out
    assert rc == 0
    data = json.loads(out)
    for key in ("mode", "counts", "attention", "tickets", "running", "queued"):
        assert key in data


def test_ask_records_ticket(sa, store, fake_executor, monkeypatch, capsys):
    engine = _engine(sa, store, fake_executor)
    _patch_engine(sa, monkeypatch, engine)

    rc = sa.sa_cli.run(
        _args(superagent_command="ask", request=["Fix", "the", "crash"]), object()
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert "Fix the crash" in out
    assert len(store.list()) == 1


def test_tick_queues_without_launch(sa, store, fake_executor, monkeypatch, capsys):
    engine = _engine(sa, store, fake_executor, mode="auto")
    _patch_engine(sa, monkeypatch, engine)
    ticket = engine.ask("Fix the parser crash")
    assert store.get(ticket["id"])["status"] == "queued"

    rc = sa.sa_cli.run(_args(superagent_command="tick"), object())
    out = capsys.readouterr().out
    assert rc == 0
    # CLI tick never launches workers directly.
    assert fake_executor.launched == []
    assert "workers are launched only" in out


def test_doctor_keys(sa, store, fake_executor, monkeypatch, capsys):
    engine = _engine(sa, store, fake_executor)
    _patch_engine(sa, monkeypatch, engine)

    rc = sa.sa_cli.run(_args(superagent_command="doctor"), object())
    out = capsys.readouterr().out
    assert rc == 0
    for key in (
        "mode",
        "repo_path",
        "repo_exists",
        "github_repo",
        "github_configured",
        "max_workers",
        "worker_toolsets",
        "workers_available",
        "workers_reason",
    ):
        assert key in out

    rc = sa.sa_cli.run(_args(superagent_command="doctor", json=True), object())
    data = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert "workers_available" in data


def test_usage_error_unknown_command(sa, store, fake_executor, monkeypatch, capsys):
    engine = _engine(sa, store, fake_executor)
    _patch_engine(sa, monkeypatch, engine)

    rc = sa.sa_cli.run(_args(superagent_command="bogus"), object())
    out = capsys.readouterr().out
    assert rc == 2
    assert "usage" in out.lower()


def test_render_robustness(sa):
    # Empty / partial payloads must not raise; output stays backward compatible.
    empty = sa.sa_cli.render_status({})
    assert "Superagent" in empty
    assert "Nothing needs you." in empty

    partial = sa.sa_cli.render_status(
        {"mode": "observe", "running": 0, "queued": 1,
         "attention": [{"id": "T-1", "status": "observed"}],
         "tickets": [{"id": "T-1", "status": "observed"}]}
    )
    assert "T-1" in partial

    minimal = sa.sa_cli.render_ticket({"id": "T-1", "status": "observed"})
    assert "T-1" in minimal
    assert "observed" in minimal

    full = sa.sa_cli.render_ticket(
        {"id": "T-1", "status": "observed", "request": "do work",
         "requester": "cli", "source": "local", "verdict": "v", "reason": "r",
         "events": [{"kind": "created", "detail": "hello"}]}
    )
    assert "do work" in full
    assert "created" in full


def test_setup_cli_registers_run_show_and_json(sa):
    parser = argparse.ArgumentParser(prog="hermes superagent")
    sa.sa_cli.setup_cli(parser)
    # run/show accept a ticket id and --json in either position.
    ns = parser.parse_args(["run", "T-0001"])
    assert ns.ticket_id == "T-0001"
    ns = parser.parse_args(["show", "T-0002", "--json"])
    assert ns.ticket_id == "T-0002" and ns.json is True
    ns = parser.parse_args(["--json", "status"])
    assert ns.json is True and ns.superagent_command == "status"


# -- v0.2 chat session commands -----------------------------------------


class FakeRunner:
    """Records launches/stops; never spawns a hermes process."""

    def __init__(self, *, fail=None, killed=True):
        self.fail = fail
        self.killed = killed
        self.launched = []
        self.stopped = []

    def launch_turn(self, session_id, prompt, **kwargs):
        if self.fail:
            raise RuntimeError(self.fail)
        self.launched.append({"session_id": session_id, "prompt": prompt, **kwargs})
        return {"session_id": session_id, "resume": False, "command": "hermes"}

    def stop(self, session_id):
        self.stopped.append(session_id)
        return self.killed


def _patch_runner(sa, monkeypatch, runner):
    monkeypatch.setattr(sa.sa_cli, "_get_runner", lambda store: runner)
    return runner


def _chat_args(**kwargs):
    base = dict(
        superagent_command="chat", json=False, prompt=[],
        model="", provider="", repo="", title="",
    )
    base.update(kwargs)
    return types.SimpleNamespace(**base)


def _clear_chat_env(monkeypatch):
    for name in (
        "SUPERAGENT_DEFAULT_MODEL",
        "SUPERAGENT_DEFAULT_PROVIDER",
        "SUPERAGENT_REPO",
        "SUPERAGENT_MODEL_PRESETS",
    ):
        monkeypatch.delenv(name, raising=False)


def test_chat_creates_session_and_launches_turn(
    sa, store, fake_executor, monkeypatch, capsys
):
    _clear_chat_env(monkeypatch)
    engine = _engine(sa, store, fake_executor)
    _patch_engine(sa, monkeypatch, engine)
    runner = _patch_runner(sa, monkeypatch, FakeRunner())

    rc = sa.sa_cli.run(_chat_args(prompt=["Fix", "the", "parser"]), object())
    out = capsys.readouterr().out
    assert rc == 0
    assert "S-0001" in out
    assert "queued" in out
    assert store.path in out  # transcript location
    assert "hermes superagent show S-0001" in out
    assert runner.launched == [
        {"session_id": "S-0001", "prompt": "Fix the parser",
         "model": "", "provider": "", "repo": "", "block": True}
    ]
    session = sa.sa_chats.ChatStore(store.path).get_session("S-0001", message_limit=0)
    assert session["title"] == "Fix the parser"
    assert session["source"] == "cli"


def test_chat_json_shape(sa, store, fake_executor, monkeypatch, capsys):
    _clear_chat_env(monkeypatch)
    engine = _engine(sa, store, fake_executor)
    _patch_engine(sa, monkeypatch, engine)
    _patch_runner(sa, monkeypatch, FakeRunner())

    rc = sa.sa_cli.run(_chat_args(prompt=["Add", "tests"], json=True), object())
    out = capsys.readouterr().out
    assert rc == 0
    data = json.loads(out)
    assert data["ok"] is True and data["launched"] is True
    assert data["session"]["id"] == "S-0001"
    assert data["session"]["status"] in ("queued", "running")
    assert data["db_path"] == store.path
    assert data["transcript"].endswith("show S-0001")


def test_chat_launch_failure_is_runtime_error(
    sa, store, fake_executor, monkeypatch, capsys
):
    _clear_chat_env(monkeypatch)
    engine = _engine(sa, store, fake_executor)
    _patch_engine(sa, monkeypatch, engine)
    _patch_runner(sa, monkeypatch, FakeRunner(fail="hermes executable not found"))

    rc = sa.sa_cli.run(_chat_args(prompt=["Fix", "it"]), object())
    out = capsys.readouterr().out
    assert rc == 1
    assert "error" in out.lower()
    assert "hermes executable not found" in out
    session = sa.sa_chats.ChatStore(store.path).get_session("S-0001", message_limit=0)
    assert session["status"] == "failed"
    assert "hermes executable not found" in session["error"]


def test_chat_without_prompt_is_usage_error(
    sa, store, fake_executor, monkeypatch, capsys
):
    engine = _engine(sa, store, fake_executor)
    _patch_engine(sa, monkeypatch, engine)

    rc = sa.sa_cli.run(_chat_args(prompt=[]), object())
    out = capsys.readouterr().out
    assert rc == 2
    assert "usage" in out.lower()


def test_sessions_lists_newest_first_with_all_columns(
    sa, store, fake_executor, monkeypatch, capsys
):
    engine = _engine(sa, store, fake_executor)
    _patch_engine(sa, monkeypatch, engine)
    chat = sa.sa_chats.ChatStore(store.path)
    first = chat.create_session("first task", model="model-one")
    second = chat.create_session("second task", model="model-two")
    chat.update_session(
        second["id"], status="running", tokens=1200, started_at=1000.0, finished_at=1012.0
    )

    rc = sa.sa_cli.run(_args(superagent_command="sessions"), object())
    out = capsys.readouterr().out
    assert rc == 0
    assert out.index(second["id"]) < out.index(first["id"])  # newest first
    for column in ("id", "status", "model", "elapsed", "tokens", "title"):
        assert column in out
    assert "second task" in out and "model-two" in out
    assert "1200" in out


def test_sessions_json_shape(sa, store, fake_executor, monkeypatch, capsys):
    engine = _engine(sa, store, fake_executor)
    _patch_engine(sa, monkeypatch, engine)
    chat = sa.sa_chats.ChatStore(store.path)
    session = chat.create_session("json task", model="m")
    chat.update_session(session["id"], status="running", tokens=42)

    rc = sa.sa_cli.run(_args(superagent_command="sessions", json=True), object())
    out = capsys.readouterr().out
    assert rc == 0
    data = json.loads(out)
    for key in ("db_exists", "sessions", "counts", "running"):
        assert key in data
    assert data["running"] == 1
    assert data["counts"]["running"] == 1
    row = data["sessions"][0]
    for key in ("id", "status", "model", "elapsed", "elapsed_seconds", "tokens", "title"):
        assert key in row
    assert row["id"] == session["id"]
    assert row["tokens"] == 42


def test_sessions_hide_archived_rows_unless_asked(sa, store, fake_executor, monkeypatch, capsys):
    engine = _engine(sa, store, fake_executor)
    _patch_engine(sa, monkeypatch, engine)
    chat = sa.sa_chats.ChatStore(store.path)
    visible = chat.create_session("visible task")
    filed = chat.create_session("filed task")
    chat.set_archived(filed["id"])

    rc = sa.sa_cli.run(_args(superagent_command="sessions", limit=20), object())
    out = capsys.readouterr().out
    assert rc == 0
    assert visible["id"] in out and filed["id"] not in out
    assert "archived=1" in out and "--all" in out

    rc = sa.sa_cli.run(
        _args(superagent_command="sessions", limit=20, show_archived=True), object()
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert visible["id"] in out and filed["id"] in out


def test_sessions_json_reports_the_archived_count(sa, store, fake_executor, monkeypatch, capsys):
    engine = _engine(sa, store, fake_executor)
    _patch_engine(sa, monkeypatch, engine)
    chat = sa.sa_chats.ChatStore(store.path)
    filed = chat.create_session("filed task")
    chat.set_archived(filed["id"])

    rc = sa.sa_cli.run(_args(superagent_command="sessions", json=True), object())
    data = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert data["sessions"] == []
    assert data["archived_count"] == 1


def test_show_session_detail_has_messages_and_events(
    sa, store, fake_executor, monkeypatch, capsys
):
    engine = _engine(sa, store, fake_executor)
    _patch_engine(sa, monkeypatch, engine)
    chat = sa.sa_chats.ChatStore(store.path)
    session = chat.create_session("Fix parser", model="m", provider="p")
    chat.add_message(session["id"], "user", "hello there")
    chat.add_message(session["id"], "assistant", "working on it")
    chat.add_event(session["id"], "tool", "read_file: sa_cli.py")

    rc = sa.sa_cli.run(_args(superagent_command="show", ticket_id=session["id"]), object())
    out = capsys.readouterr().out
    assert rc == 0
    assert session["id"] in out
    assert "messages (2)" in out
    assert "hello there" in out and "working on it" in out
    assert "events (" in out
    assert "read_file: sa_cli.py" in out


def test_show_session_json_includes_messages_and_events(
    sa, store, fake_executor, monkeypatch, capsys
):
    engine = _engine(sa, store, fake_executor)
    _patch_engine(sa, monkeypatch, engine)
    chat = sa.sa_chats.ChatStore(store.path)
    session = chat.create_session("Fix parser")
    chat.add_message(session["id"], "user", "hi")
    chat.add_event(session["id"], "queued", "waiting")

    rc = sa.sa_cli.run(
        _args(superagent_command="show", ticket_id=session["id"], json=True), object()
    )
    out = capsys.readouterr().out
    assert rc == 0
    data = json.loads(out)
    assert data["db_exists"] is True
    detail = data["session"]
    assert detail["id"] == session["id"]
    assert [m["role"] for m in detail["messages"]] == ["user"]
    kinds = [event["kind"] for event in detail["events"]]
    assert "created" in kinds and "queued" in kinds


def test_show_unknown_session_is_error(
    sa, store, fake_executor, monkeypatch, capsys
):
    engine = _engine(sa, store, fake_executor)
    _patch_engine(sa, monkeypatch, engine)

    rc = sa.sa_cli.run(_args(superagent_command="show", ticket_id="S-9999"), object())
    out = capsys.readouterr().out
    assert rc == 1
    assert "unknown chat session S-9999" in out


def test_stop_cancels_running_session(sa, store, fake_executor, monkeypatch, capsys):
    engine = _engine(sa, store, fake_executor)
    _patch_engine(sa, monkeypatch, engine)
    chat = sa.sa_chats.ChatStore(store.path)
    session = chat.create_session("long job")
    runner = _patch_runner(sa, monkeypatch, FakeRunner(killed=True))

    rc = sa.sa_cli.run(_args(superagent_command="stop", session_id=session["id"]), object())
    out = capsys.readouterr().out
    assert rc == 0
    assert runner.stopped == [session["id"]]
    assert f"cancelled {session['id']}" in out
    assert "worker stopped" in out


def test_stop_not_running_exits_1_with_message(
    sa, store, fake_executor, monkeypatch, capsys
):
    engine = _engine(sa, store, fake_executor)
    _patch_engine(sa, monkeypatch, engine)
    chat = sa.sa_chats.ChatStore(store.path)
    session = chat.create_session("done job")
    chat.update_session(session["id"], status="review")
    runner = _patch_runner(sa, monkeypatch, FakeRunner())

    rc = sa.sa_cli.run(_args(superagent_command="stop", session_id=session["id"]), object())
    out = capsys.readouterr().out
    assert rc == 1
    assert "nothing to stop" in out
    assert "review" in out
    assert runner.stopped == []


def test_stop_json_when_nothing_running(sa, store, fake_executor, monkeypatch, capsys):
    engine = _engine(sa, store, fake_executor)
    _patch_engine(sa, monkeypatch, engine)
    chat = sa.sa_chats.ChatStore(store.path)
    session = chat.create_session("done job")
    chat.update_session(session["id"], status="review")
    _patch_runner(sa, monkeypatch, FakeRunner())

    rc = sa.sa_cli.run(
        _args(superagent_command="stop", session_id=session["id"], json=True), object()
    )
    out = capsys.readouterr().out
    assert rc == 1
    data = json.loads(out)
    assert data["ok"] is False
    assert data["stopped"] is False
    assert "nothing to stop" in data["error"]
    assert data["session"]["status"] == "review"


def test_stop_invalid_session_id_is_usage_error(
    sa, store, fake_executor, monkeypatch, capsys
):
    engine = _engine(sa, store, fake_executor)
    _patch_engine(sa, monkeypatch, engine)

    rc = sa.sa_cli.run(
        _args(superagent_command="stop", session_id="../superagent.db"), object()
    )
    out = capsys.readouterr().out
    assert rc == 2
    assert "usage" in out.lower()


def test_models_text_uses_env_defaults_and_presets(sa, monkeypatch, capsys):
    monkeypatch.setenv("SUPERAGENT_DEFAULT_MODEL", "deepseek-v4.1-flash")
    monkeypatch.setenv("SUPERAGENT_DEFAULT_PROVIDER", "opencode-go")
    monkeypatch.setenv("SUPERAGENT_MODEL_PRESETS", "fast-model,strong-model")

    rc = sa.sa_cli.run(_args(superagent_command="models"), object())
    out = capsys.readouterr().out
    assert rc == 0
    assert "deepseek-v4.1-flash" in out
    assert "opencode-go" in out
    assert "- fast-model" in out and "- strong-model" in out


def test_models_json_shape(sa, monkeypatch, capsys):
    monkeypatch.setenv("SUPERAGENT_DEFAULT_MODEL", "m1")
    monkeypatch.setenv("SUPERAGENT_DEFAULT_PROVIDER", "p1")
    monkeypatch.setenv("SUPERAGENT_MODEL_PRESETS", "a,b")

    rc = sa.sa_cli.run(_args(superagent_command="models", json=True), object())
    data = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert data["default_model"] == "m1"
    assert data["default_provider"] == "p1"
    assert data["models"] == ["a", "b", "m1"]
    # Presets keep the historical flat list working but add the named view.
    assert data["presets"] == [
        {"name": "a", "model": "a", "provider": ""},
        {"name": "b", "model": "b", "provider": ""},
    ]


def test_chat_session_commands_accept_json_in_both_positions(sa):
    parser = argparse.ArgumentParser(prog="hermes superagent")
    sa.sa_cli.setup_cli(parser)

    ns = parser.parse_args(["--json", "chat", "hello", "world"])
    assert ns.superagent_command == "chat"
    assert ns.json is True
    assert ns.prompt == ["hello", "world"]

    ns = parser.parse_args(["chat", "hello", "--json"])
    assert ns.superagent_command == "chat" and ns.json is True and ns.prompt == ["hello"]

    ns = parser.parse_args(["sessions", "--json"])
    assert ns.superagent_command == "sessions" and ns.json is True

    ns = parser.parse_args(["--json", "stop", "S-0001"])
    assert ns.superagent_command == "stop" and ns.session_id == "S-0001" and ns.json is True

    ns = parser.parse_args(["models", "--json"])
    assert ns.superagent_command == "models" and ns.json is True


def test_chat_flags_override_defaults(sa, store, fake_executor, monkeypatch, capsys):
    _clear_chat_env(monkeypatch)
    engine = _engine(sa, store, fake_executor)
    _patch_engine(sa, monkeypatch, engine)
    runner = _patch_runner(sa, monkeypatch, FakeRunner())

    rc = sa.sa_cli.run(
        _chat_args(
            prompt=["work"], model="m1", provider="p1", title="Custom title"
        ),
        object(),
    )
    assert rc == 0
    capsys.readouterr()
    call = runner.launched[0]
    assert call["model"] == "m1"
    assert call["provider"] == "p1"
    session = sa.sa_chats.ChatStore(store.path).get_session(call["session_id"], message_limit=0)
    assert session["title"] == "Custom title"
    assert session["model"] == "m1"
    assert session["provider"] == "p1"


def test_get_runner_is_singleton_per_database(sa, chat, monkeypatch):
    monkeypatch.setenv("SUPERAGENT_HERMES_BIN", "C:/fake/hermes.exe")
    first = sa.sa_cli._get_runner(chat)
    second = sa.sa_cli._get_runner(chat)
    assert first is second
    assert isinstance(first, sa.sa_cli.sa_runner.Runner)
    assert first.chat.path == chat.path
    # Resolving the binary must not spawn anything.
    assert first.binary() == "C:/fake/hermes.exe"


def test_models_falls_back_to_plugin_config(sa, monkeypatch, capsys):
    _clear_chat_env(monkeypatch)

    class Ctx:
        config = {
            "default_model": "from-config",
            "default_provider": "cfg-provider",
            "model_presets": ["x-model", "y-model"],
        }

        def get_config(self, key, default=None):
            return self.config.get(key, default)

    rc = sa.sa_cli.run(_args(superagent_command="models", json=True), Ctx())
    data = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert data["default_model"] == "from-config"
    assert data["default_provider"] == "cfg-provider"
    assert data["models"] == ["x-model", "y-model", "from-config"]


def test_chat_blocks_by_default_so_the_turn_actually_runs(
    sa, store, fake_executor, monkeypatch, capsys
):
    """A CLI process is short-lived: a detached daemon thread dies with it.

    Regression: `chat` used to call launch_turn() without block, so the worker
    thread was killed when the command returned and the session stayed queued
    forever with no error.
    """
    _clear_chat_env(monkeypatch)
    engine = _engine(sa, store, fake_executor)
    _patch_engine(sa, monkeypatch, engine)
    runner = _patch_runner(sa, monkeypatch, FakeRunner())

    rc = sa.sa_cli.run(_chat_args(prompt=["Fix", "it"]), object())
    assert rc == 0
    assert runner.launched[0]["block"] is True


def test_chat_detach_returns_without_waiting(
    sa, store, fake_executor, monkeypatch, capsys
):
    _clear_chat_env(monkeypatch)
    engine = _engine(sa, store, fake_executor)
    _patch_engine(sa, monkeypatch, engine)
    runner = _patch_runner(sa, monkeypatch, FakeRunner())

    rc = sa.sa_cli.run(_chat_args(prompt=["Fix", "it"], detach=True), object())
    out = capsys.readouterr().out
    assert rc == 0
    assert runner.launched[0]["block"] is False
    assert "queued" in out
    assert "follow:" in out  # detached output is the short header, not a transcript


# -- model presets + settings command (v0.4) -------------------------------


@pytest.fixture
def cli_settings_home(sa, tmp_path, monkeypatch):
    """Point the CLI's settings loader at a throwaway Hermes home."""
    home = tmp_path / "hermes"
    home.mkdir()
    monkeypatch.setattr(sa.sa_settings, "_hermes_home", lambda: home)
    monkeypatch.setattr(sa.sa_settings, "config_path", lambda: home / "config.yaml")
    for name in sa.sa_settings.ENV_VARS.values():
        monkeypatch.delenv(name, raising=False)
    sa.sa_settings.clear_cache()
    yield home
    sa.sa_settings.clear_cache()


def test_models_text_lists_named_presets(sa, monkeypatch, capsys, cli_settings_home):
    sa.sa_settings.save_settings(
        {"model_presets": ["fast=opencode-go:qwen/qwen3.8-27b", {"name": "strong", "model": "glm"}]}
    )

    rc = sa.sa_cli.run(_args(superagent_command="models"), object())
    out = capsys.readouterr().out
    assert rc == 0
    assert "fast -> opencode-go: qwen/qwen3.8-27b" in out
    assert "strong -> glm" in out


def test_chat_preset_resolves_model_and_provider(
    sa, store, fake_executor, monkeypatch, capsys, cli_settings_home
):
    sa.sa_settings.save_settings({"model_presets": ["fast=opencode-go:qwen/qwen3.8-27b"]})
    engine = _engine(sa, store, fake_executor)
    _patch_engine(sa, monkeypatch, engine)
    runner = _patch_runner(sa, monkeypatch, FakeRunner())

    rc = sa.sa_cli.run(_chat_args(prompt=["Fix", "it"], preset="fast"), object())
    assert rc == 0
    capsys.readouterr()
    call = runner.launched[0]
    assert call["model"] == "qwen/qwen3.8-27b"
    assert call["provider"] == "opencode-go"
    session = sa.sa_chats.ChatStore(store.path).get_session(call["session_id"])
    assert session["model"] == "qwen/qwen3.8-27b"
    assert session["provider"] == "opencode-go"
    assert "preset" in [event["kind"] for event in session["events"]]


def test_chat_preset_keeps_explicit_flags_wins(
    sa, store, fake_executor, monkeypatch, capsys, cli_settings_home
):
    sa.sa_settings.save_settings({"model_presets": ["fast=opencode-go:qwen/qwen3.8-27b"]})
    engine = _engine(sa, store, fake_executor)
    _patch_engine(sa, monkeypatch, engine)
    runner = _patch_runner(sa, monkeypatch, FakeRunner())

    rc = sa.sa_cli.run(
        _chat_args(
            prompt=["Fix", "it"], preset="fast", model="explicit", provider="explicit-provider"
        ),
        object(),
    )
    assert rc == 0
    capsys.readouterr()
    call = runner.launched[0]
    assert call["model"] == "explicit"
    assert call["provider"] == "explicit-provider"


def test_chat_unknown_preset_exits_2_with_available_names(
    sa, store, fake_executor, monkeypatch, capsys, cli_settings_home
):
    sa.sa_settings.save_settings({"model_presets": ["fast=one", "strong=two"]})
    engine = _engine(sa, store, fake_executor)
    _patch_engine(sa, monkeypatch, engine)
    runner = _patch_runner(sa, monkeypatch, FakeRunner())

    rc = sa.sa_cli.run(_chat_args(prompt=["Fix", "it"], preset="missing"), object())
    out = capsys.readouterr().out
    assert rc == 2
    assert "unknown preset 'missing'" in out
    assert "fast" in out and "strong" in out
    assert runner.launched == []
    assert sa.sa_chats.ChatStore(store.path).list_sessions()["sessions"] == []


def test_settings_text_prints_merged_values_and_masks_the_token(
    sa, capsys, cli_settings_home
):
    sa.sa_settings.save_settings(
        {
            "mode": "auto",
            "github_token": "ghp_secret_value",
            "github_repo": "owner/name",
            "model_presets": ["fast=opencode-go:qwen/qwen3.8-27b"],
        }
    )

    rc = sa.sa_cli.run(_args(superagent_command="settings"), object())
    out = capsys.readouterr().out
    assert rc == 0
    assert "Superagent settings" in out
    assert "mode" in out and "auto" in out
    assert "owner/name" in out
    assert "fast -> opencode-go: qwen/qwen3.8-27b" in out
    assert "ghp_secret_value" not in out
    assert "GitHub token" in out and "set (value never shown)" in out


def test_settings_json_never_includes_the_token(sa, capsys, cli_settings_home):
    sa.sa_settings.save_settings({"github_token": "ghp_secret_value"})

    rc = sa.sa_cli.run(_args(superagent_command="settings", json=True), object())
    out = capsys.readouterr().out
    data = json.loads(out)
    assert rc == 0
    assert data["github_token_set"] is True
    assert "github_token" not in data
    assert "ghp_secret_value" not in out
    assert data["max_workers"] == 2


def test_settings_command_needs_no_engine(sa, monkeypatch, capsys, cli_settings_home):
    def boom(ctx):
        raise RuntimeError("engine should not start")

    monkeypatch.setattr(sa.sa_cli.sa_engine, "build_engine", boom)
    rc = sa.sa_cli.run(_args(superagent_command="settings"), object())
    assert rc == 0
    assert "Superagent settings" in capsys.readouterr().out


def test_parser_registers_preset_and_settings(sa):
    parser = argparse.ArgumentParser(prog="hermes superagent")
    sa.sa_cli.setup_cli(parser)
    ns = parser.parse_args(["chat", "hello", "--preset", "fast"])
    assert ns.preset == "fast"
    ns = parser.parse_args(["settings", "--json"])
    assert ns.superagent_command == "settings" and ns.json is True


# -- search ----------------------------------------------------------------


def test_search_prints_each_hit_and_its_snippet(
    sa, store, fake_executor, monkeypatch, capsys
):
    engine = _engine(sa, store, fake_executor)
    _patch_engine(sa, monkeypatch, engine)
    chat = sa.sa_chats.ChatStore(store.path)
    hit = chat.create_session("Fix the postgres migration")
    chat.add_message(hit["id"], "assistant", "the rollback never ran")
    chat.create_session("Add a regression test")

    rc = sa.sa_cli.run(
        _args(superagent_command="search", query=["rollback"], limit=20, archived="hide"),
        object(),
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert "Search 'rollback'" in out
    assert hit["id"] in out
    assert "the rollback never ran" in out
    assert "regression" not in out


def test_search_json_carries_the_term_and_match_field(
    sa, store, fake_executor, monkeypatch, capsys
):
    engine = _engine(sa, store, fake_executor)
    _patch_engine(sa, monkeypatch, engine)
    chat = sa.sa_chats.ChatStore(store.path)
    hit = chat.create_session("Fix the postgres migration")
    chat.add_message(hit["id"], "assistant", "the rollback never ran")

    rc = sa.sa_cli.run(
        _args(
            superagent_command="search", query=["rollback"], limit=20,
            archived="hide", json=True,
        ),
        object(),
    )
    data = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert data["query"] == "rollback"
    assert data["sessions"][0]["id"] == hit["id"]
    assert data["sessions"][0]["match_field"] == "message"


def test_search_without_hits_prints_one_line(
    sa, store, fake_executor, monkeypatch, capsys
):
    engine = _engine(sa, store, fake_executor)
    _patch_engine(sa, monkeypatch, engine)
    sa.sa_chats.ChatStore(store.path).create_session("something unrelated")

    rc = sa.sa_cli.run(
        _args(superagent_command="search", query=["zzz"], limit=20, archived="hide"),
        object(),
    )
    assert rc == 0
    assert capsys.readouterr().out.strip() == "No chats match 'zzz'."


def test_search_needs_a_query(sa, store, fake_executor, monkeypatch, capsys):
    engine = _engine(sa, store, fake_executor)
    _patch_engine(sa, monkeypatch, engine)

    rc = sa.sa_cli.run(
        _args(superagent_command="search", query=[], limit=20, archived="hide"),
        object(),
    )
    assert rc == 2
    assert "query" in capsys.readouterr().out


def test_search_rejects_a_bad_archived_mode(
    sa, store, fake_executor, monkeypatch, capsys
):
    engine = _engine(sa, store, fake_executor)
    _patch_engine(sa, monkeypatch, engine)

    rc = sa.sa_cli.run(
        _args(superagent_command="search", query=["x"], limit=20, archived="nope"),
        object(),
    )
    assert rc == 2
    assert "--archived" in capsys.readouterr().out


# -- retry -----------------------------------------------------------------


def _failed_chat(sa, store, prompt="fix the parser", *, hermes_session_id="h-1"):
    chat = sa.sa_chats.ChatStore(store.path)
    session = chat.create_session(prompt, model="m")
    chat.add_message(session["id"], "user", prompt)
    chat.update_session(
        session["id"], status=sa.sa_chats.FAILED, error="boom", tokens=99,
        duration_ms=5, hermes_session_id=hermes_session_id,
    )
    return chat, session["id"]


def test_retry_relaunches_and_prints_the_session(
    sa, store, fake_executor, monkeypatch, capsys
):
    _clear_chat_env(monkeypatch)
    engine = _engine(sa, store, fake_executor)
    _patch_engine(sa, monkeypatch, engine)
    runner = _patch_runner(sa, monkeypatch, FakeRunner())
    chat, session_id = _failed_chat(sa, store)

    rc = sa.sa_cli.run(
        _args(superagent_command="retry", session_id=session_id, detach=True), object()
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert session_id in out
    assert runner.launched == [{
        "session_id": session_id,
        "prompt": "fix the parser",
        "resume_session_id": "h-1",
        "model": "m",
        "provider": "",
        "repo": "",
        "block": False,
    }]
    assert chat.last_user_message(session_id)["content"] == "fix the parser"
    assert chat.get_session(session_id, message_limit=0)["tokens"] is None


def test_retry_json_shape(sa, store, fake_executor, monkeypatch, capsys):
    _clear_chat_env(monkeypatch)
    engine = _engine(sa, store, fake_executor)
    _patch_engine(sa, monkeypatch, engine)
    _patch_runner(sa, monkeypatch, FakeRunner())
    _, session_id = _failed_chat(sa, store)

    rc = sa.sa_cli.run(
        _args(superagent_command="retry", session_id=session_id, detach=True, json=True),
        object(),
    )
    data = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert data["ok"] is True and data["launched"] is True and data["resumed"] is True
    assert data["session"]["id"] == session_id
    assert data["transcript"].endswith("show " + session_id)


def test_retry_refuses_a_reviewed_chat(sa, store, fake_executor, monkeypatch, capsys):
    _clear_chat_env(monkeypatch)
    engine = _engine(sa, store, fake_executor)
    _patch_engine(sa, monkeypatch, engine)
    _patch_runner(sa, monkeypatch, FakeRunner())
    chat, session_id = _failed_chat(sa, store)
    chat.update_session(session_id, status=sa.sa_chats.REVIEW)

    rc = sa.sa_cli.run(
        _args(superagent_command="retry", session_id=session_id, detach=True), object()
    )
    out = capsys.readouterr().out
    assert rc == 1
    assert "review" in out


def test_retry_unknown_session_is_an_error(
    sa, store, fake_executor, monkeypatch, capsys
):
    _clear_chat_env(monkeypatch)
    engine = _engine(sa, store, fake_executor)
    _patch_engine(sa, monkeypatch, engine)
    _patch_runner(sa, monkeypatch, FakeRunner())

    rc = sa.sa_cli.run(
        _args(superagent_command="retry", session_id="S-9999", detach=True), object()
    )
    assert rc == 1
    assert "unknown chat session" in capsys.readouterr().out


def test_retry_without_an_id_is_usage(sa, store, fake_executor, monkeypatch, capsys):
    engine = _engine(sa, store, fake_executor)
    _patch_engine(sa, monkeypatch, engine)

    rc = sa.sa_cli.run(
        _args(superagent_command="retry", session_id="", detach=True), object()
    )
    assert rc == 2
    assert "usage" in capsys.readouterr().out.lower()


# -- export ----------------------------------------------------------------


def _export_args(**kwargs):
    base = dict(superagent_command="export", json=False, session_id="", out="")
    base.update(kwargs)
    return types.SimpleNamespace(**base)


def _session_with_messages(sa, store, prompt="fix the parser"):
    chat = sa.sa_chats.ChatStore(store.path)
    session = chat.create_session(prompt, model="m")
    chat.add_message(session["id"], "user", prompt)
    chat.add_message(session["id"], "assistant", "done, **tests** pass")
    return chat, session["id"]


def test_export_prints_markdown(sa, store, fake_executor, monkeypatch, capsys):
    engine = _engine(sa, store, fake_executor)
    _patch_engine(sa, monkeypatch, engine)
    _, session_id = _session_with_messages(sa, store)

    rc = sa.sa_cli.run(_export_args(session_id=session_id), object())
    out = capsys.readouterr().out
    assert rc == 0
    assert out.startswith("# fix the parser")
    assert "### Assistant" in out and "done, **tests** pass" in out


def test_export_writes_a_file(sa, store, fake_executor, monkeypatch, capsys, tmp_path):
    engine = _engine(sa, store, fake_executor)
    _patch_engine(sa, monkeypatch, engine)
    _, session_id = _session_with_messages(sa, store)
    target = tmp_path / "out.md"

    rc = sa.sa_cli.run(_export_args(session_id=session_id, out=str(target)), object())
    out = capsys.readouterr().out
    assert rc == 0
    assert str(target) in out
    assert target.read_text(encoding="utf-8").startswith("# fix the parser")


def test_export_to_a_directory_uses_the_session_filename(
    sa, store, fake_executor, monkeypatch, capsys, tmp_path
):
    engine = _engine(sa, store, fake_executor)
    _patch_engine(sa, monkeypatch, engine)
    _, session_id = _session_with_messages(sa, store)

    rc = sa.sa_cli.run(_export_args(session_id=session_id, out=str(tmp_path)), object())
    assert rc == 0
    assert (tmp_path / f"superagent-{session_id}.md").is_file()


def test_export_json_carries_the_markdown(sa, store, fake_executor, monkeypatch, capsys):
    engine = _engine(sa, store, fake_executor)
    _patch_engine(sa, monkeypatch, engine)
    _, session_id = _session_with_messages(sa, store)

    rc = sa.sa_cli.run(_export_args(session_id=session_id, json=True), object())
    data = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert data["filename"] == f"superagent-{session_id}.md"
    assert data["length"] == len(data["markdown"])
    assert "### You" in data["markdown"]


def test_export_unknown_session_is_an_error(
    sa, store, fake_executor, monkeypatch, capsys
):
    engine = _engine(sa, store, fake_executor)
    _patch_engine(sa, monkeypatch, engine)

    rc = sa.sa_cli.run(_export_args(session_id="S-9999"), object())
    assert rc == 1
    assert "unknown chat session" in capsys.readouterr().out
