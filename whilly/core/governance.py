"""Pure deterministic governance risk scoring for high-risk Whilly work."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

__all__ = [
    "REQUIRED_GOVERNANCE_CATEGORIES",
    "GovernanceAssessment",
    "GovernanceInput",
    "GovernanceRiskFinding",
    "GovernanceRiskLevel",
    "assess_governance_risk",
]


REQUIRED_GOVERNANCE_CATEGORIES: tuple[str, ...] = (
    "migration",
    "auth",
    "infrastructure",
    "dependencies",
    "release",
    "external_pr",
)

_HIGH_RISK_SCORE = 90


class GovernanceRiskLevel(str, Enum):
    """Governance risk level for a supplied task or plan fragment."""

    LOW = "LOW"
    HIGH = "HIGH"


@dataclass(frozen=True)
class GovernanceRiskFinding:
    """Inspectable high-risk evidence for one governance category."""

    category: str
    score: int
    reason: str
    matched_signal: str
    approval_boundary: str


@dataclass(frozen=True)
class GovernanceAssessment:
    """Pure-data result returned by :func:`assess_governance_risk`."""

    level: GovernanceRiskLevel
    score: int
    findings: tuple[GovernanceRiskFinding, ...] = ()


@dataclass(frozen=True)
class GovernanceInput:
    """Caller-supplied task or plan metadata used for deterministic scoring."""

    description: str = ""
    acceptance_criteria: tuple[str, ...] = ()
    test_steps: tuple[str, ...] = ()
    key_files: tuple[str, ...] = ()
    stage_type: str | None = None
    sink_type: str | None = None


@dataclass(frozen=True)
class _CategoryPolicy:
    text_signals: tuple[str, ...]
    path_signals: tuple[str, ...]
    reason: str
    approval_boundary: str


_CATEGORY_POLICIES: dict[str, _CategoryPolicy] = {
    "migration": _CategoryPolicy(
        text_signals=(
            "migration",
            "alembic",
            "schema sql",
            "schema.sql",
            "database migration",
            "db migration",
        ),
        path_signals=("alembic/", "migrations/", "schema.sql"),
        reason="Database schema or migration work changes persistent state and requires an operator-reviewed plan.",
        approval_boundary="requires_operator_approval_and_migration_plan",
    ),
    "auth": _CategoryPolicy(
        text_signals=("auth", "authn", "authz", "oauth", "token", "admin bearer", "permission"),
        path_signals=("/auth/", "auth.py", "auth_", "_auth.py", "tokens.py", "permissions.py"),
        reason="Authentication or authorization changes can alter access boundaries and require human review.",
        approval_boundary="requires_operator_approval_and_auth_boundary_review",
    ),
    "infrastructure": _CategoryPolicy(
        text_signals=("docker", "compose", "kubernetes", "deploy", "ci", "terraform", "infra", "worker runtime"),
        path_signals=("dockerfile", "docker-compose", ".github/workflows/", "k8s/", "terraform/", "docker/"),
        reason="Infrastructure, deployment, CI, or worker runtime changes affect execution environment safety.",
        approval_boundary="requires_operator_approval_and_infra_change_notes",
    ),
    "dependencies": _CategoryPolicy(
        text_signals=("dependency", "dependencies", "package", "bump", "upgrade", "pin", "lockfile"),
        path_signals=(
            "pyproject.toml",
            "requirements",
            "poetry.lock",
            "uv.lock",
            "package-lock.json",
            "pnpm-lock.yaml",
            "yarn.lock",
        ),
        reason="Dependency changes can alter the trusted runtime surface and require reviewable change notes.",
        approval_boundary="requires_operator_approval_and_dependency_change_notes",
    ),
    "release": _CategoryPolicy(
        text_signals=("release", "tag", "version", "publish", "production rollout"),
        path_signals=("changelog", "release", "version"),
        reason="Release or production rollout work changes what operators or users receive.",
        approval_boundary="requires_operator_approval_and_release_notes",
    ),
    "external_pr": _CategoryPolicy(
        text_signals=(
            "github pr",
            "github pull request",
            "external pull request",
            "auto-open pr",
            "auto open pr",
            "open external github",
            "whilly_auto_open_pr",
            "github_pr",
        ),
        path_signals=(),
        reason="Externally mutating pull-request behavior must stay opt-in and human-approved.",
        approval_boundary="external_mutation_must_be_opt_in_and_human_approved",
    ),
}


def assess_governance_risk(input: GovernanceInput) -> GovernanceAssessment:
    """Return deterministic governance risk for the supplied metadata only."""
    text = _joined_text(input)
    paths = tuple(path.lower() for path in input.key_files)

    findings: list[GovernanceRiskFinding] = []
    for category in REQUIRED_GOVERNANCE_CATEGORIES:
        policy = _CATEGORY_POLICIES[category]
        matched_signal = _match_policy(policy, text=text, paths=paths)
        if matched_signal is None:
            continue
        findings.append(
            GovernanceRiskFinding(
                category=category,
                score=_HIGH_RISK_SCORE,
                reason=policy.reason,
                matched_signal=matched_signal,
                approval_boundary=policy.approval_boundary,
            )
        )

    if not findings:
        return GovernanceAssessment(level=GovernanceRiskLevel.LOW, score=0, findings=())

    return GovernanceAssessment(
        level=GovernanceRiskLevel.HIGH,
        score=max(finding.score for finding in findings),
        findings=tuple(findings),
    )


def _joined_text(input: GovernanceInput) -> str:
    parts = (
        input.description,
        *input.acceptance_criteria,
        *input.test_steps,
        input.stage_type or "",
        input.sink_type or "",
    )
    return "\n".join(part for part in parts if part).lower()


def _match_policy(policy: _CategoryPolicy, *, text: str, paths: tuple[str, ...]) -> str | None:
    for signal in policy.text_signals:
        if _contains_signal(text, signal):
            return signal
    for path in paths:
        for signal in policy.path_signals:
            if signal in path:
                return path
    return None


def _contains_signal(text: str, signal: str) -> bool:
    normalized = signal.lower()
    if not normalized:
        return False
    if not normalized.replace("_", "").replace("-", "").replace(".", "").isalnum():
        return normalized in text
    return re.search(rf"(?<![a-z0-9]){re.escape(normalized)}(?![a-z0-9])", text) is not None
