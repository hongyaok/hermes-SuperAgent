"""Release consistency checks: one version, one story, one manifest description.

These guard the places a release number is published (the plugin manifest,
the dashboard manifest the desktop shell reads, the changelog's newest heading,
and the backend's ``/health`` version). They are cheap because drift here is
invisible at runtime and embarrassing in a release.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_SEMVER = re.compile(r"^\d+\.\d+\.\d+$")
_RELEASE_HEADING = re.compile(r"^##\s+(\d+\.\d+\.\d+)\b")
_PLUGIN_VERSION = re.compile(r"^version:\s*(\S+)\s*$", re.MULTILINE)


def _plugin_version() -> str:
    manifest = (ROOT / "plugin.yaml").read_text(encoding="utf-8")
    match = _PLUGIN_VERSION.search(manifest)
    assert match, "plugin.yaml has no top-level version"
    return match.group(1).strip().strip('"\'')


def _manifest() -> dict:
    return json.loads((ROOT / "dashboard" / "manifest.json").read_text(encoding="utf-8"))


def _top_release_heading() -> str:
    """The first ``##`` heading in the changelog must be a release, not Unreleased."""
    for line in (ROOT / "CHANGELOG.md").read_text(encoding="utf-8").splitlines():
        if line.startswith("## "):
            match = _RELEASE_HEADING.match(line)
            assert match, f"the newest changelog heading is not a release: {line!r}"
            return match.group(1)
    raise AssertionError("CHANGELOG.md has no release heading")


def test_plugin_manifest_changelog_and_backend_versions_agree():
    plugin_version = _plugin_version()
    manifest_version = str(_manifest()["version"]).strip()
    changelog_version = _top_release_heading()
    api_source = (ROOT / "dashboard" / "plugin_api.py").read_text(encoding="utf-8")
    match = re.search(r'^VERSION\s*=\s*"([^"]+)"', api_source, re.MULTILINE)

    assert _SEMVER.match(plugin_version), plugin_version
    assert plugin_version == manifest_version, (
        f"plugin.yaml {plugin_version} != dashboard/manifest.json {manifest_version}"
    )
    assert plugin_version == changelog_version, (
        f"plugin.yaml {plugin_version} != newest CHANGELOG release {changelog_version}"
    )
    assert match, "dashboard/plugin_api.py has no VERSION constant"
    assert plugin_version == match.group(1), (
        f"plugin.yaml {plugin_version} != dashboard/plugin_api.py VERSION {match.group(1)}"
    )


def test_manifest_description_mentions_sessions():
    description = str(_manifest().get("description") or "")
    assert "session" in description.lower(), (
        "dashboard/manifest.json must describe the chat-session surface"
    )


def test_top_changelog_release_has_added_changed_fixed_sections():
    lines = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8").splitlines()
    start = next(index for index, line in enumerate(lines) if line.startswith("## "))
    block: list[str] = []
    for line in lines[start + 1:]:
        if line.startswith("## "):
            break
        block.append(line)
    headings = {line.strip().lower() for line in block if line.startswith("### ")}
    for section in ("### added", "### changed", "### fixed"):
        assert section in headings, f"newest release is missing a {section[4:]} section"
