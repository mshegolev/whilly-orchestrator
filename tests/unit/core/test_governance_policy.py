"""Unit tests for the pure deterministic governance risk policy."""

from __future__ import annotations

import asyncio
import importlib
import socket
import subprocess
import urllib.request
from typing import Any

import pytest


REQUIRED_CATEGORIES = (
    "migration",
    "auth",
    "infrastructure",
    "dependencies",
    "release",
    "external_pr",
)

EXPECTED_BOUNDARIES = {
    "migration": "requires_operator_approval_and_migration_plan",
    "auth": "requires_operator_approval_and_auth_boundary_review",
    "infrastructure": "requires_operator_approval_and_infra_change_notes",
    "dependencies": "requires_operator_approval_and_dependency_change_notes",
    "release": "requires_operator_approval_and_release_notes",
    "external_pr": "external_mutation_must_be_opt_in_and_human_approved",
}

SIGNAL_INPUTS = {
    "migration": {
        "description": "Add Alembic migration for the plans table",
        "key_files": ("alembic/versions/20260508_add_plan.py",),
    },
    "auth": {
        "description": "Change OAuth token permission checks for admin bearer auth",
        "key_files": ("whilly/auth/tokens.py",),
    },
    "infrastructure": {
        "description": "Update Docker compose and CI deployment worker runtime",
        "key_files": ("docker-compose.demo.yml", ".github/workflows/test.yml"),
    },
    "dependencies": {
        "description": "Bump and pin package dependency versions",
        "key_files": ("pyproject.toml", "requirements-dev.txt"),
    },
    "release": {
        "description": "Prepare production release tag and publish version metadata",
        "key_files": ("CHANGELOG.md", "whilly/__init__.py"),
    },
    "external_pr": {
        "description": "Auto-open external GitHub pull request after completion",
        "key_files": ("whilly/pipeline/sinks.py",),
        "sink_type": "github_pr",
    },
}


def _governance_module() -> Any:
    try:
        return importlib.import_module("whilly.core.governance")
    except ModuleNotFoundError as exc:  # pragma: no cover - RED phase assertion
        pytest.fail(f"missing governance module: {exc}")


@pytest.mark.parametrize("category", REQUIRED_CATEGORIES)
def test_required_governance_domains_are_high_risk(category: str) -> None:
    governance = _governance_module()
    signal = SIGNAL_INPUTS[category]
    risk_input = governance.GovernanceInput(
        description=signal["description"],
        key_files=signal["key_files"],
        sink_type=signal.get("sink_type"),
    )

    assessment = governance.assess_governance_risk(risk_input)

    assert governance.REQUIRED_GOVERNANCE_CATEGORIES == REQUIRED_CATEGORIES
    assert assessment.level is governance.GovernanceRiskLevel.HIGH
    assert assessment.score >= 80
    assert assessment.findings
    finding = assessment.findings[0]
    assert finding.category == category
    assert finding.score >= 80
    assert finding.reason
    assert finding.matched_signal
    assert finding.approval_boundary == EXPECTED_BOUNDARIES[category]


def test_multiple_findings_are_ordered_by_required_categories() -> None:
    governance = _governance_module()
    risk_input = governance.GovernanceInput(
        description="Add migration, auth, Docker, dependency, release, and GitHub PR changes",
        acceptance_criteria=("CI deploy notes updated",),
        test_steps=("Open external pull request",),
        key_files=(
            "migrations/001.sql",
            "whilly/auth/tokens.py",
            "Dockerfile",
            "pyproject.toml",
            "CHANGELOG.md",
        ),
        sink_type="github_pr",
    )

    assessment = governance.assess_governance_risk(risk_input)

    assert tuple(finding.category for finding in assessment.findings) == REQUIRED_CATEGORIES
    assert assessment.score == max(finding.score for finding in assessment.findings)


def test_no_governance_signals_returns_low_risk_without_findings() -> None:
    governance = _governance_module()
    risk_input = governance.GovernanceInput(
        description="Update dashboard copy",
        acceptance_criteria=("Copy renders",),
        test_steps=("Run focused unit tests",),
        key_files=("docs/usage.md",),
    )

    assessment = governance.assess_governance_risk(risk_input)

    assert assessment.level is governance.GovernanceRiskLevel.LOW
    assert assessment.score == 0
    assert assessment.findings == ()


def test_governance_output_is_deterministic_on_equal_input() -> None:
    governance = _governance_module()
    risk_input = governance.GovernanceInput(
        description="Update schema migration and Docker deployment release",
        key_files=("alembic/versions/change.py", "docker-compose.yml"),
    )

    first = governance.assess_governance_risk(risk_input)

    for _ in range(25):
        assert governance.assess_governance_risk(risk_input) == first


def test_governance_scorer_performs_no_io(monkeypatch: pytest.MonkeyPatch) -> None:
    governance = _governance_module()

    def _fail(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("I/O attempted")

    monkeypatch.setattr("builtins.open", _fail)
    monkeypatch.setattr(socket, "socket", _fail)
    monkeypatch.setattr(subprocess, "run", _fail)
    monkeypatch.setattr(subprocess, "Popen", _fail)
    monkeypatch.setattr(urllib.request, "urlopen", _fail)
    monkeypatch.setattr(asyncio, "get_event_loop", _fail)

    assessment = governance.assess_governance_risk(
        governance.GovernanceInput(
            description="Configure WHILLY_AUTO_OPEN_PR for external GitHub PR behavior",
            sink_type="github_pr",
        )
    )

    assert assessment.level is governance.GovernanceRiskLevel.HIGH
    assert assessment.findings[0].category == "external_pr"
