"""Tests for the shared settings loader (sa_settings.py).

Every test points the loader at a throwaway HERMES_HOME (settings.json and
config.yaml both live there), so the real machine configuration is never read
or written.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sa_plugin import sa_settings

yaml = pytest.importorskip("yaml")


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A throwaway Hermes home with no config and no environment overrides."""
    root = tmp_path / "hermes"
    root.mkdir()
    monkeypatch.setattr(sa_settings, "_hermes_home", lambda: root)
    monkeypatch.setattr(sa_settings, "config_path", lambda: root / "config.yaml")
    for name in sa_settings.ENV_VARS.values():
        monkeypatch.delenv(name, raising=False)
    sa_settings.clear_cache()
    yield root
    sa_settings.clear_cache()


def write_config(root: Path, settings: dict) -> None:
    payload = {"plugins": {"entries": {"superagent": {"settings": settings}}}}
    (root / "config.yaml").write_text(yaml.safe_dump(payload), encoding="utf-8")
    sa_settings.clear_cache()


def test_defaults_are_complete(home):
    settings = sa_settings.load_settings()
    assert settings == sa_settings.DEFAULTS
    assert settings["github_watch_mode"] == "observe"
    assert settings["github_poll_seconds"] == 90
    assert settings["github_pr_limit"] == 10


def test_defaults_match_plugin_yaml(home):
    manifest = yaml.safe_load((Path(__file__).resolve().parents[1] / "plugin.yaml").read_text("utf-8"))
    schema = manifest["config_schema"]
    for key, value in sa_settings.DEFAULTS.items():
        spec = schema.get(key)
        assert spec is not None, f"{key} is missing from plugin.yaml"
        if "default" in spec:
            assert spec["default"] == value, f"plugin.yaml default for {key} drifted"


def test_data_dir_lives_under_the_hermes_home(home):
    assert sa_settings.data_dir() == home / "plugin-data" / "superagent"
    assert sa_settings.settings_path() == home / "plugin-data" / "superagent" / "settings.json"


def test_config_yaml_settings_are_merged(home):
    write_config(
        home,
        {"repo_path": "C:/work/repo", "default_model": "qwen/qwen3.8-27b", "max_workers": 4},
    )
    settings = sa_settings.load_settings()
    assert settings["repo_path"] == "C:/work/repo"
    assert settings["default_model"] == "qwen/qwen3.8-27b"
    assert settings["max_workers"] == 4
    # Untouched keys keep their defaults.
    assert settings["github_watch_mode"] == "observe"


def test_settings_json_beats_config_yaml(home):
    write_config(home, {"default_model": "from-config", "default_provider": "config-provider"})
    sa_settings.save_settings({"default_model": "from-json"})
    settings = sa_settings.load_settings()
    assert settings["default_model"] == "from-json"
    assert settings["default_provider"] == "config-provider"


def test_environment_beats_settings_json(home, monkeypatch):
    sa_settings.save_settings({"default_model": "from-json", "max_workers": 2})
    monkeypatch.setenv("SUPERAGENT_DEFAULT_MODEL", "from-env")
    monkeypatch.setenv("SUPERAGENT_MAX_WORKERS", "7")
    settings = sa_settings.load_settings()
    assert settings["default_model"] == "from-env"
    assert settings["max_workers"] == 7


def test_placeholder_token_falls_back_to_environment(home, monkeypatch):
    write_config(home, {"github_token": "${SUPERAGENT_GITHUB_TOKEN}"})
    monkeypatch.setenv("SUPERAGENT_GITHUB_TOKEN", "ghp_from_env")
    assert sa_settings.load_settings()["github_token"] == "ghp_from_env"


def test_placeholder_token_without_environment_is_empty(home):
    write_config(home, {"github_token": "${MISSING_TOKEN}"})
    assert sa_settings.load_settings()["github_token"] == ""


def test_empty_token_falls_back_to_environment(home, monkeypatch):
    sa_settings.save_settings({"github_token": "   "})
    monkeypatch.setenv("SUPERAGENT_GITHUB_TOKEN", "ghp_saved_env")
    assert sa_settings.load_settings()["github_token"] == "ghp_saved_env"


def test_allowed_users_accept_a_comma_separated_env_list(home, monkeypatch):
    monkeypatch.setenv("SUPERAGENT_GITHUB_ALLOWED_USERS", "alice, bob ,carol")
    assert sa_settings.load_settings()["github_allowed_users"] == ["alice", "bob", "carol"]


def test_an_explicit_empty_list_clears_a_list_setting(home):
    write_config(home, {"github_allowed_users": ["alice"]})
    assert sa_settings.load_settings()["github_allowed_users"] == ["alice"]
    sa_settings.save_settings({"github_allowed_users": []})
    assert sa_settings.load_settings()["github_allowed_users"] == []


def test_bool_and_int_values_are_coerced(home):
    write_config(home, {"worker_yolo": "false", "chat_max_turns": "9", "github_watch_new_prs": "yes"})
    settings = sa_settings.load_settings()
    assert settings["worker_yolo"] is False
    assert settings["chat_max_turns"] == 9
    assert settings["github_watch_new_prs"] is True


def test_save_settings_is_atomic_and_merges(home):
    sa_settings.save_settings({"github_repo": "owner/name"})
    sa_settings.save_settings({"github_token": "ghp_secret"})
    path = sa_settings.settings_path()
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["github_repo"] == "owner/name"  # the second save kept the first
    assert data["github_token"] == "ghp_secret"
    assert list(path.parent.glob("*.tmp")) == []  # no temp file left behind


def test_save_settings_rejects_unknown_keys(home):
    with pytest.raises(ValueError) as excinfo:
        sa_settings.save_settings({"github_repoo": "typo"})
    assert "unknown" in str(excinfo.value)
    assert not sa_settings.settings_path().exists()


def test_save_settings_can_clear_the_token(home):
    sa_settings.save_settings({"github_token": "ghp_secret"})
    sa_settings.save_settings({"github_token": ""})
    assert sa_settings.load_settings()["github_token"] == ""


def test_cache_refreshes_when_settings_json_changes(home):
    assert sa_settings.load_settings()["mode"] == "observe"
    sa_settings.save_settings({"mode": "auto"})  # save_settings clears the cache
    assert sa_settings.load_settings()["mode"] == "auto"
    # And a write that bypasses save_settings is still noticed by mtime.
    path = sa_settings.settings_path()
    data = json.loads(path.read_text(encoding="utf-8"))
    data["mode"] = "off"
    path.write_text(json.dumps(data), encoding="utf-8")
    assert sa_settings.load_settings()["mode"] == "off"


def test_public_settings_hides_the_token(home):
    sa_settings.save_settings({"github_token": "ghp_secret", "github_repo": "owner/name"})
    public = sa_settings.public_settings()
    assert public["github_token_set"] is True
    assert "github_token" not in public
    assert "ghp_secret" not in json.dumps(public)


def test_save_settings_does_not_return_the_token(home):
    saved = sa_settings.save_settings({"github_token": "ghp_secret", "github_repo": "owner/name"})
    assert saved["github_repo"] == "owner/name"
    assert saved["github_token_set"] is True
    assert "ghp_secret" not in json.dumps(saved)


def test_valid_repo_name(home):
    assert sa_settings.valid_repo_name("owner/name")
    assert sa_settings.valid_repo_name("my-org/my.repo_name")
    assert not sa_settings.valid_repo_name("owner")
    assert not sa_settings.valid_repo_name("owner/name/extra")
    assert not sa_settings.valid_repo_name("")


def test_a_broken_config_file_is_not_fatal(home):
    (home / "config.yaml").write_text("plugins: [unclosed", encoding="utf-8")
    sa_settings.clear_cache()
    assert sa_settings.load_settings()["mode"] == "observe"


# -- model presets ---------------------------------------------------------


def test_normalize_presets_accepts_legacy_plain_strings():
    presets = sa_settings.normalize_presets(["qwen/qwen3.8-27b", "z-ai/glm-5.3-flash"])
    assert presets == [
        {"name": "qwen/qwen3.8-27b", "model": "qwen/qwen3.8-27b", "provider": ""},
        {"name": "z-ai/glm-5.3-flash", "model": "z-ai/glm-5.3-flash", "provider": ""},
    ]


def test_normalize_presets_parses_named_strings():
    presets = sa_settings.normalize_presets(
        ["fast=opencode-go:qwen/qwen3.8-27b", "cheap=qwen3-local"]
    )
    assert presets == [
        {"name": "fast", "model": "qwen/qwen3.8-27b", "provider": "opencode-go"},
        {"name": "cheap", "model": "qwen3-local", "provider": ""},
    ]


def test_normalize_presets_accepts_objects_and_derives_missing_names():
    presets = sa_settings.normalize_presets(
        [
            {"name": "strong", "model": "z-ai/glm-5.3-flash", "provider": "opencode-go"},
            {"model": "deepseek-v4.1-flash"},
        ]
    )
    assert presets == [
        {"name": "strong", "model": "z-ai/glm-5.3-flash", "provider": "opencode-go"},
        {"name": "deepseek-v4.1-flash", "model": "deepseek-v4.1-flash", "provider": ""},
    ]


def test_normalize_presets_is_lenient_by_default():
    presets = sa_settings.normalize_presets(
        ["Fast=opencode-go:one", "fast=two", {"model": ""}, "", None]
    )
    # Duplicates keep the first spelling, unusable entries are dropped.
    assert presets == [{"name": "Fast", "model": "one", "provider": "opencode-go"}]


def test_normalize_presets_strict_rejects_bad_input():
    with pytest.raises(ValueError, match="duplicate"):
        sa_settings.normalize_presets(["fast=one", "FAST=two"], strict=True)
    with pytest.raises(ValueError, match="model"):
        sa_settings.normalize_presets([{"name": "empty"}], strict=True)
    with pytest.raises(ValueError, match="list"):
        sa_settings.normalize_presets(42, strict=True)
    with pytest.raises(ValueError, match="at most"):
        sa_settings.normalize_presets([f"m{index}" for index in range(13)], strict=True)


def test_load_settings_normalizes_legacy_presets(home):
    write_config(home, {"model_presets": ["one-model", "two-model"]})
    assert sa_settings.load_settings()["model_presets"] == [
        {"name": "one-model", "model": "one-model", "provider": ""},
        {"name": "two-model", "model": "two-model", "provider": ""},
    ]


def test_load_settings_normalizes_named_presets_from_env(home, monkeypatch):
    monkeypatch.setenv("SUPERAGENT_MODEL_PRESETS", "fast=opencode-go:one-model, two-model")
    assert sa_settings.load_settings()["model_presets"] == [
        {"name": "fast", "model": "one-model", "provider": "opencode-go"},
        {"name": "two-model", "model": "two-model", "provider": ""},
    ]


def test_save_settings_normalizes_presets(home):
    sa_settings.save_settings({"model_presets": ["fast=opencode-go:one-model"]})
    assert sa_settings.load_settings()["model_presets"] == [
        {"name": "fast", "model": "one-model", "provider": "opencode-go"}
    ]
    # The file itself is normalised too, so every reader sees the same shape.
    data = json.loads(sa_settings.settings_path().read_text(encoding="utf-8"))
    assert data["model_presets"] == [
        {"name": "fast", "model": "one-model", "provider": "opencode-go"}
    ]


def _fake_hermes_config(monkeypatch, *, path, payload):
    """Install a fake ``hermes_cli.config`` module for one test."""
    import sys
    import types

    package = types.ModuleType("hermes_cli")
    package.__path__ = []  # mark it as a package so the submodule import works
    module = types.ModuleType("hermes_cli.config")
    module.get_config_path = lambda: path
    module.load_config = lambda: payload
    monkeypatch.setitem(sys.modules, "hermes_cli", package)
    monkeypatch.setitem(sys.modules, "hermes_cli.config", module)


def test_config_settings_uses_hermes_loader_when_paths_match(home, monkeypatch):
    """The plugin host has no PyYAML; Hermes' loader must be the primary read."""
    cfg = home / "config.yaml"
    cfg.write_text("plugins: {}\n", encoding="utf-8")
    payload = {"plugins": {"entries": {"superagent": {"settings": {"default_model": "from-loader"}}}}}
    _fake_hermes_config(monkeypatch, path=cfg, payload=payload)
    monkeypatch.setattr(
        sa_settings, "_load_yaml", lambda path: (_ for _ in ()).throw(AssertionError("yaml read"))
    )

    assert sa_settings._config_settings() == {"default_model": "from-loader"}


def test_config_settings_falls_back_to_yaml_when_loader_points_elsewhere(home, monkeypatch):
    """A test/isolated home must never borrow the machine's config."""
    elsewhere = home.parent / "other" / "config.yaml"
    _fake_hermes_config(monkeypatch, path=elsewhere, payload={})
    import sys

    def boom():
        raise AssertionError("loader read")

    sys.modules["hermes_cli.config"].load_config = boom
    monkeypatch.setattr(
        sa_settings,
        "_load_yaml",
        lambda path: {"plugins": {"entries": {"superagent": {"settings": {"default_model": "from-yaml"}}}}},
    )

    assert sa_settings._config_settings() == {"default_model": "from-yaml"}
