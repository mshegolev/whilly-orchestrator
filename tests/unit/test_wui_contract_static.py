from __future__ import annotations

import re
from pathlib import Path

from whilly.operator_views import OperatorUiArtifactStatus, operator_wui_artifacts


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
    assert hotkeys_artifact.status is OperatorUiArtifactStatus.INACTIVE_QUARANTINED
    assert (
        hotkeys_artifact.reason
        == "Static hotkey file still contains pre-contract selectors/routes; Task 2 fixes it before Phase 13 completes."
    )
    assert hotkeys_artifact.followup_phase == "13"


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
