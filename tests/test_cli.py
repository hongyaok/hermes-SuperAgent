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
