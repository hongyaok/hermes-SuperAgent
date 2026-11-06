"""One settings loader for the dashboard and the CLI.

Every surface used to read configuration its own way: the CLI asked the Hermes
plugin context, the dashboard read environment variables, and the in-app
connect form had nowhere to write. This module is the single source of truth:

    DEFAULTS  <  Hermes config.yaml  <  <data_dir>/settings.json  <  environment

`settings.json` is what the desktop connect form writes (a repository and a
token live there, so it is chmod 600 where the platform supports it). The
token itself is never returned by an HTTP handler: `public_settings()` reports
`github_token_set` instead of the value.

The merged result is cached for a few seconds and keyed by the mtimes of both
files plus a fingerprint of the environment, so a test (or a user editing
config.yaml) does not have to know about the cache.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

DEFAULTS: dict[str, Any] = {
    "mode": "observe",
    "repo_path": "",
    "github_repo": "",
    "github_agent_login": "",
    "github_allowed_users": [],
    "github_token": "",
    "default_model": "",
    "default_provider": "",
    "model_presets": [],
    "max_workers": 2,
    "worker_yolo": True,
    "chat_max_turns": 120,
    "github_poll_seconds": 90,
    "github_watch_mode": "observe",
    "github_watch_new_prs": False,
    "github_pr_limit": 10,
}

# Environment overrides, in the same order as DEFAULTS so a missing value in
# one layer falls through instead of blanking the one below it.
ENV_VARS: dict[str, str] = {
    "mode": "SUPERAGENT_MODE",
    "repo_path": "SUPERAGENT_REPO",
    "github_repo": "SUPERAGENT_GITHUB_REPO",
    "github_token": "SUPERAGENT_GITHUB_TOKEN",
    "github_agent_login": "SUPERAGENT_GITHUB_AGENT_LOGIN",
    "github_allowed_users": "SUPERAGENT_GITHUB_ALLOWED_USERS",
    "default_model": "SUPERAGENT_DEFAULT_MODEL",
    "default_provider": "SUPERAGENT_DEFAULT_PROVIDER",
    "model_presets": "SUPERAGENT_MODEL_PRESETS",
    "max_workers": "SUPERAGENT_MAX_WORKERS",
    "worker_yolo": "SUPERAGENT_WORKER_YOLO",
    "chat_max_turns": "SUPERAGENT_CHAT_MAX_TURNS",
    "github_poll_seconds": "SUPERAGENT_GITHUB_POLL_SECONDS",
    "github_watch_mode": "SUPERAGENT_GITHUB_WATCH_MODE",
    "github_watch_new_prs": "SUPERAGENT_GITHUB_WATCH_NEW_PRS",
    "github_pr_limit": "SUPERAGENT_GITHUB_PR_LIMIT",
}

CACHE_SECONDS = 5.0

_SETTINGS_FILE = "settings.json"
_GITHUB_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_PLACEHOLDER_RE = re.compile(r"^\$\{[^}]+\}$")

_cache_lock = threading.Lock()
_cache: tuple[float, tuple, dict[str, Any]] | None = None


# -- locations -----------------------------------------------------------


def _hermes_home() -> Path:
    """Hermes home: hermes_constants, then HERMES_HOME, then ~/.hermes."""
    try:
        from hermes_constants import get_hermes_home

        return Path(get_hermes_home())
    except Exception:
        pass
    env = (os.environ.get("HERMES_HOME") or "").strip()
    return Path(env) if env else Path.home() / ".hermes"


def data_dir() -> Path:
    """`<HERMES_HOME>/plugin-data/superagent` — the plugin's own directory."""
    return _hermes_home() / "plugin-data" / "superagent"


def settings_path() -> Path:
    return data_dir() / _SETTINGS_FILE


def config_path() -> Path:
    """The Hermes config.yaml, with the same fallbacks the dashboard uses."""
    try:
        from hermes_constants import get_config_path

        return Path(get_config_path())
    except Exception:
        pass
    env = (os.environ.get("HERMES_CONFIG") or "").strip()
    return Path(env) if env else _hermes_home() / "config.yaml"


# -- coercion ------------------------------------------------------------


def _as_bool(value: Any, fallback: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value or "").strip().lower()
    if text in ("1", "true", "yes", "on"):
        return True
    if text in ("0", "false", "no", "off", ""):
        return False
    return fallback


def _as_int(value: Any, fallback: int) -> int:
    if isinstance(value, bool):
        return int(value)
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return fallback


def _as_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    if isinstance(value, (list, tuple, set)):
        return [str(part).strip() for part in value if str(part).strip()]
    return []


def _coerce(key: str, value: Any, fallback: Any) -> Any:
    """Coerce one value to the type of its default; bad input keeps ``fallback``."""
    default = DEFAULTS.get(key)
    if isinstance(default, bool):
        return _as_bool(value, bool(fallback))
    if isinstance(default, int):
        return _as_int(value, int(fallback))
    if isinstance(default, list):
        # An explicit empty list is meaningful ("nobody", "use the built-in
        # presets"), so it is never replaced by the fallback.
        return _as_list(value)
    return str(value if value is not None else "").strip()


def _merge_into(merged: dict[str, Any], source: dict[str, Any] | None) -> None:
    """Apply one settings layer, ignoring unknown keys and None values."""
    if not isinstance(source, dict):
        return
    for key, value in source.items():
        if key not in DEFAULTS or value is None:
            continue
        merged[key] = _coerce(key, value, merged.get(key, DEFAULTS[key]))


# -- file layers ---------------------------------------------------------


def _load_yaml(path: Path) -> dict[str, Any]:
    """Parse the Hermes config; never raises (a broken config is not fatal)."""
    try:
        import yaml  # optional: the dashboard must work without PyYAML
    except Exception:
        return {}
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
    except (OSError, ValueError, yaml.YAMLError):
        return {}
    return data if isinstance(data, dict) else {}


def _config_settings() -> dict[str, Any]:
    """`plugins.entries.superagent.settings` from the Hermes config.yaml."""
    data = _load_yaml(config_path())
    plugins = data.get("plugins") if isinstance(data, dict) else None
    entries = plugins.get("entries") if isinstance(plugins, dict) else None
    entry = entries.get("superagent") if isinstance(entries, dict) else None
    settings = entry.get("settings") if isinstance(entry, dict) else None
    return settings if isinstance(settings, dict) else {}


def _json_settings() -> dict[str, Any]:
    """The in-app settings file (written by POST /github/connect)."""
    try:
        with settings_path().open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _env_settings() -> dict[str, Any]:
    values: dict[str, Any] = {}
    for key, name in ENV_VARS.items():
        raw = os.environ.get(name)
        if raw is None or not str(raw).strip():
            continue  # an empty variable means "not set", not "blank the layer"
        values[key] = raw
    return values


def _finalize_token(merged: dict[str, Any]) -> None:
    """Resolve `${...}` placeholders and empty tokens against the environment."""
    token = str(merged.get("github_token") or "").strip()
    if not token or _PLACEHOLDER_RE.match(token):
        token = (os.environ.get("SUPERAGENT_GITHUB_TOKEN") or "").strip()
    if _PLACEHOLDER_RE.match(token):
        token = ""
    merged["github_token"] = token


# -- cache ---------------------------------------------------------------


def _mtime(path: Path) -> int:
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return -1


def _cache_key() -> tuple:
    env = tuple((name, os.environ.get(name, "")) for _, name in sorted(ENV_VARS.items()))
    config = config_path()
    settings = settings_path()
    # The paths are part of the key so tests (and a changed HERMES_HOME) cannot
    # hit a cache entry belonging to a different directory with the same mtimes.
    return (str(config), _mtime(config), str(settings), _mtime(settings), env)


def clear_cache() -> None:
    """Drop the merged cache (tests, or after save_settings)."""
    global _cache
    with _cache_lock:
        _cache = None


# -- public API ----------------------------------------------------------


def _copy(settings: dict[str, Any]) -> dict[str, Any]:
    """A shallow copy with fresh lists, so a caller cannot mutate the cache."""
    copy = dict(settings)
    for key, value in copy.items():
        if isinstance(value, list):
            copy[key] = list(value)
    return copy


def load_settings() -> dict[str, Any]:
    """Merged settings: defaults, config.yaml, settings.json, environment."""
    global _cache
    key = _cache_key()
    now = time.time()
    with _cache_lock:
        cached = _cache
        if cached and now - cached[0] < CACHE_SECONDS and cached[1] == key:
            return _copy(cached[2])

    merged = dict(DEFAULTS)
    merged["github_allowed_users"] = list(DEFAULTS["github_allowed_users"])
    merged["model_presets"] = list(DEFAULTS["model_presets"])
    _merge_into(merged, _config_settings())
    _merge_into(merged, _json_settings())
    _merge_into(merged, _env_settings())
    _finalize_token(merged)

    with _cache_lock:
        _cache = (now, key, dict(merged))
    return _copy(merged)


def public_settings() -> dict[str, Any]:
    """Settings safe to hand to an HTTP client: the token becomes a boolean."""
    data = load_settings()
    token = data.pop("github_token", "")
    data["github_token_set"] = bool(token)
    return data


def valid_repo_name(repo: Any) -> bool:
    """True for a GitHub `owner/name` string."""
    return bool(_GITHUB_REPO_RE.match(str(repo or "").strip()))


def save_settings(patch: dict[str, Any]) -> dict[str, Any]:
    """Merge `patch` into settings.json atomically and return the file contents.

    Unknown keys are rejected: a typo in the connect form must fail loudly
    instead of being written and silently ignored by every reader. The token
    is stored as given (empty string clears it) and never logged.
    """
    if not isinstance(patch, dict):
        raise ValueError("settings patch must be an object")
    unknown = sorted(set(patch) - set(DEFAULTS))
    if unknown:
        raise ValueError(f"unknown setting(s): {', '.join(unknown)}")

    current = {
        key: _coerce(key, value, DEFAULTS[key])
        for key, value in _json_settings().items()
        if key in DEFAULTS
    }
    for key, value in patch.items():
        current[key] = _coerce(key, value, current.get(key, DEFAULTS[key]))

    path = settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=str(path.parent),
        prefix=".settings-",
        suffix=".tmp",
        delete=False,
    )
    temp_path = Path(handle.name)
    try:
        with handle:
            json.dump(current, handle, indent=2, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.chmod(temp_path, 0o600)
        except OSError:
            pass  # Windows: the file is per-user under LOCALAPPDATA already
        os.replace(temp_path, path)
    finally:
        if temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                pass
    clear_cache()
    # Never echo the token: callers only need to know whether one was stored.
    result = {key: value for key, value in current.items() if key != "github_token"}
    result["github_token_set"] = bool(current.get("github_token"))
    return result
