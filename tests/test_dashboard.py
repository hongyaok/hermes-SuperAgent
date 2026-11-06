"""Tests for the read-only dashboard backend (dashboard/plugin_api.py).

The backend is imported by the gateway on its own, so it is loaded here the
same way — standalone, with the DB path patched to a throwaway database.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load_api():
    path = ROOT / "dashboard" / "plugin_api.py"
    spec = importlib.util.spec_from_file_location("superagent_plugin_api", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["superagent_plugin_api"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def api(tmp_path, monkeypatch, sa):
    module = load_api()
    db = tmp_path / "superagent.db"
    store = sa.sa_store.Store(db)
    store.create_ticket(source="local", request="Fix the parser crash",
                        status="observed", verdict="accept", reason="explicit")
    store.create_ticket(source="local", request="Add a regression test",
                        status="queued", verdict="accept", reason="explicit")
    monkeypatch.setattr(module, "_db_path", lambda: db)
    return module


def test_health_reports_version_and_db(api):
    body = api.health()
    assert body["ok"] is True
    assert body["db_exists"] is True
    assert body["version"]


def test_tickets_lists_attention_and_recent(api):
    body = api.list_tickets()
    assert body["db_exists"] is True
    assert body["counts"]["observed"] == 1
    assert body["counts"]["queued"] == 1
    assert len(body["attention"]) == 1
    assert body["attention"][0]["status"] == "observed"
    assert len(body["tickets"]) == 2


def test_ticket_detail_includes_events(api):
    first = api.list_tickets()["tickets"][0]
    detail = api.get_ticket(first["id"])
    assert detail["id"] == first["id"]
    assert detail["events"]


def test_unknown_ticket_is_404(api):
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as excinfo:
        api.get_ticket("T-9999")
    assert excinfo.value.status_code == 404


def test_missing_db_returns_empty_not_an_error(tmp_path, monkeypatch):
    module = load_api()
    monkeypatch.setattr(module, "_db_path", lambda: tmp_path / "nope.db")
    assert module.health()["db_exists"] is False
    body = module.list_tickets()
    assert body == {"db_exists": False, "counts": {}, "attention": [], "tickets": []}
