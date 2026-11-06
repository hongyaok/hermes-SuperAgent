import json


class FakeCtx:
    def __init__(self, config=None):
        self.config = config or {"mode": "observe"}
        self.subagent_lifecycle = None

    def get_config(self, key, default=None):
        return self.config.get(key, default)


def test_ask_records_an_observed_ticket(tmp_path, monkeypatch, sa):
    monkeypatch.setattr(sa.sa_engine, "_db_path", lambda: tmp_path / "db.sqlite")
    result = json.loads(sa.sa_tools.superagent_ask({"request": "Fix the parser crash"}, ctx=FakeCtx()))
    assert result["ok"] is True
    assert result["ticket"]["status"] == "observed"


def test_ask_rejects_an_empty_request(tmp_path, monkeypatch, sa):
    monkeypatch.setattr(sa.sa_engine, "_db_path", lambda: tmp_path / "db.sqlite")
    result = json.loads(sa.sa_tools.superagent_ask({"request": "  "}, ctx=FakeCtx()))
    assert "error" in result


def test_status_reports_counts(tmp_path, monkeypatch, sa):
    monkeypatch.setattr(sa.sa_engine, "_db_path", lambda: tmp_path / "db.sqlite")
    ctx = FakeCtx()
    sa.sa_tools.superagent_ask({"request": "Fix the parser crash"}, ctx=ctx)
    result = json.loads(sa.sa_tools.superagent_status({}, ctx=ctx))
    assert result["mode"] == "observe"
    assert result["counts"]["observed"] == 1


def test_tick_without_a_parent_still_reports(tmp_path, monkeypatch, sa):
    monkeypatch.setattr(sa.sa_engine, "_db_path", lambda: tmp_path / "db.sqlite")
    ctx = FakeCtx({"mode": "auto"})
    sa.sa_tools.superagent_ask({"request": "Fix the parser crash"}, ctx=ctx)
    result = json.loads(sa.sa_tools.superagent_tick({}, ctx=ctx))
    assert result["tick"]["mode"] == "auto"
    assert result["status"]["queued"] == 1


def test_handlers_never_raise(tmp_path, monkeypatch, sa):
    def boom(_ctx):
        raise RuntimeError("database exploded")

    monkeypatch.setattr(sa.sa_engine, "build_engine", boom)
    result = json.loads(sa.sa_tools.superagent_status({}, ctx=FakeCtx()))
    assert "database exploded" in result["error"]


def test_register_wires_tools_commands_blueprint_skill_and_docs(sa):
    from pathlib import Path

    class RecordingCtx(FakeCtx):
        def __init__(self):
            super().__init__()
            self.tools = {}
            self.commands = {}
            self.cli_commands = {}
            self.blueprints = {}
            self.skills = {}

        def register_tool(self, *, name, toolset, schema, handler):
            self.tools[name] = (toolset, schema, handler)

        def register_command(self, name, *, handler, description, args_hint=""):
            self.commands[name] = (handler, description, args_hint)

        def register_cli_command(self, *, name, help, setup_fn, handler_fn):
            self.cli_commands[name] = (help, setup_fn, handler_fn)

        def register_automation_blueprint(self, name, **kwargs):
            self.blueprints[name] = kwargs

        def register_skill(self, name, skill_md):
            self.skills[name] = Path(skill_md)

    ctx = RecordingCtx()
    sa.register(ctx)

    assert set(ctx.tools) == {"superagent_ask", "superagent_tick", "superagent_status", "superagent_run"}
    assert all(toolset == "superagent" for toolset, _, _ in ctx.tools.values())
    assert "superagent" in ctx.commands
    assert "superagent" in ctx.cli_commands
    assert "watch" in ctx.blueprints
    assert "superagent-operator" in ctx.skills
    assert ctx.skills["superagent-operator"].is_file()

    root = Path(sa.__file__).resolve().parent
    assert (root / "README.md").is_file()
    assert (root / "DESIGN.md").is_file()


def test_slash_command_subcommands(tmp_path, monkeypatch, sa):
    monkeypatch.setattr(sa.sa_engine, "_db_path", lambda: tmp_path / "db.sqlite")
    ctx = FakeCtx({"mode": "observe"})

    assert "Usage: /superagent" in sa.slash_command("help", ctx)
    assert "Usage: /superagent ask" in sa.slash_command("ask", ctx)
    assert "Usage: /superagent run" in sa.slash_command("run", ctx)
    assert "Unknown subcommand" in sa.slash_command("nonexistent", ctx)

    asked = sa.slash_command("ask Fix the crash in parser.py", ctx)
    ticket_id = asked.split()[0]
    assert "recorded as observed" in asked

    status_out = sa.slash_command("status", ctx)
    assert ticket_id in status_out

    run_out = sa.slash_command(f"run {ticket_id}", ctx)
    assert ticket_id in run_out
    assert "queued" in run_out

    tick_out = sa.slash_command("tick", ctx)
    assert "polled=" in tick_out

