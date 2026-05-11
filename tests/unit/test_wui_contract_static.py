from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

from whilly.operator_views import (
    OperatorUiArtifactStatus,
    operator_surface_items,
    operator_wui_artifacts,
    operator_wui_route_prefixes,
    operator_wui_selectors,
)


BANNED_ACTIVE_WUI_PATTERNS = ("1-7", "/^[1-7]$/", ".tabs [data-key]")
BANNED_ACTIVE_WUI_REGEXES = (r"(?<!/api/v1)/admin/workers/",)


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _wui_artifact_paths() -> set[str]:
    project_root = _project_root()
    template_paths = {
        path.relative_to(project_root).as_posix()
        for path in (project_root / "whilly/api/templates").iterdir()
        if path.is_file()
    }
    static_javascript_paths = {
        path.relative_to(project_root).as_posix()
        for path in (project_root / "whilly/api/static").glob("*.js")
        if path.is_file()
    }
    return template_paths | static_javascript_paths


def test_wui_artifacts_are_classified() -> None:
    artifacts = operator_wui_artifacts()
    assert _wui_artifact_paths() == {artifact.path for artifact in artifacts}

    artifacts_by_path = {artifact.path: artifact for artifact in artifacts}
    logs_artifact = artifacts_by_path["whilly/api/templates/_logs.html"]
    assert logs_artifact.status is OperatorUiArtifactStatus.ROUTEABLE_NONCANONICAL
    assert logs_artifact.reason == "Routeable by ?fragment=logs but not in canonical nav/TUI parity yet."
    assert logs_artifact.followup_phase == "14"

    admin_artifact = artifacts_by_path["whilly/api/templates/_admin.html"]
    assert admin_artifact.status is OperatorUiArtifactStatus.INACTIVE_QUARANTINED
    assert admin_artifact.reason == "Contains admin controls with unsupported /admin/* routes."
    assert admin_artifact.followup_phase == "14"

    prd_artifact = artifacts_by_path["whilly/api/templates/_prd.html"]
    assert prd_artifact.status is OperatorUiArtifactStatus.INACTIVE_QUARANTINED
    assert prd_artifact.reason == "Contains PRD controls with unsupported /prd/* routes."
    assert prd_artifact.followup_phase == "14"

    hotkeys_artifact = artifacts_by_path["whilly/api/static/whilly-hotkeys.js"]
    assert hotkeys_artifact.status is OperatorUiArtifactStatus.ACTIVE


def test_non_active_wui_artifacts_have_reason_and_followup() -> None:
    for artifact in operator_wui_artifacts():
        if artifact.status is not OperatorUiArtifactStatus.ACTIVE:
            assert artifact.reason
            assert artifact.followup_phase


def test_active_wui_artifacts_reject_stale_patterns() -> None:
    project_root = _project_root()
    for artifact in operator_wui_artifacts(OperatorUiArtifactStatus.ACTIVE):
        text = (project_root / artifact.path).read_text()
        for pattern in BANNED_ACTIVE_WUI_PATTERNS:
            assert pattern not in text
        for regex in BANNED_ACTIVE_WUI_REGEXES:
            assert re.search(regex, text) is None


def test_static_hotkeys_file_uses_current_contract() -> None:
    project_root = _project_root()
    javascript_text = (project_root / "whilly/api/static/whilly-hotkeys.js").read_text()
    surface_values = [surface.value for surface, _label in operator_surface_items()]
    assert json.dumps(surface_values) in javascript_text

    surface_key_regex = f"/^[1-{len(surface_values)}]$/"
    assert surface_key_regex in javascript_text

    selectors = operator_wui_selectors()
    assert selectors["surface_tab"].removesuffix("]") + '="' in javascript_text
    assert selectors["filter"] in javascript_text
    assert selectors["review_actionable_row"] in javascript_text

    worker_prefix = operator_wui_route_prefixes()["worker_control"]
    assert f"{worker_prefix}pause" in javascript_text
    assert f"{worker_prefix}resume" in javascript_text

    for pattern in BANNED_ACTIVE_WUI_PATTERNS:
        assert pattern not in javascript_text
    for regex in BANNED_ACTIVE_WUI_REGEXES:
        assert re.search(regex, javascript_text) is None


def test_setuptools_includes_wui_static_package_data() -> None:
    pyproject = tomllib.loads((_project_root() / "pyproject.toml").read_text())
    package_data = pyproject["tool"]["setuptools"]["package-data"]["whilly"]

    assert "api/static/*.css" in package_data
    assert "api/static/*.js" in package_data
    assert "api/static/fonts/*.ttf" in package_data
