"""Load the plugin directory as a package so tests import the real code."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

spec = importlib.util.spec_from_file_location(
    "sa_plugin", ROOT / "__init__.py", submodule_search_locations=[str(ROOT)]
)
assert spec and spec.loader
plugin = importlib.util.module_from_spec(spec)
sys.modules["sa_plugin"] = plugin
spec.loader.exec_module(plugin)


@pytest.fixture
def sa():
    return plugin


@pytest.fixture
def store(tmp_path, sa):
    return sa.sa_store.Store(tmp_path / "superagent.db")


class FakeExecutor:
    """Records launches and lets a test decide what workers report."""

    def __init__(self, state: str = "RUNNING"):
        self.state = state
        self.launched = []
        self.cancelled = []
        self.error = None

    def available(self):
        return True, ""

    def launch(self, *, goal, context, correlation_id):
        if self.error:
            raise RuntimeError(self.error)
        self.launched.append({"id": correlation_id, "goal": goal, "context": context})
        return {"contract_version": 1, "subagent_id": f"sa-{correlation_id}", "correlation_id": correlation_id}

    def poll(self, handle):
        return plugin.sa_executor.WorkerUpdate(self.state, summary="worker summary" if self.state == "SUCCEEDED" else None)

    def cancel(self, handle):
        self.cancelled.append(handle)


@pytest.fixture
def fake_executor():
    return FakeExecutor()


def settings(sa, **overrides):
    base = dict(mode="observe", max_workers=2, max_runtime_seconds=60)
    base.update(overrides)
    return sa.sa_engine.Settings(**base)
