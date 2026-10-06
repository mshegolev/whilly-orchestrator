"""Host-owned project verification policy and trusted gate-result parsers.

Verification configuration is executable input, so it is immutable, explicit
argv (never shell text), and bound into the coordinator's approval digest.
Parsers accept only host-captured reports and fail closed on ambiguous output.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import re
import xml.etree.ElementTree as ET
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from whilly.swarm.agent import ProcessOutcome
    from whilly.swarm.registry import Project

__all__ = [
    "DEFAULT_PROTECTED_PATHS",
    "GateEvidence",
    "VerificationError",
    "VerificationPolicy",
    "parse_gate_result",
    "protected_changes",
    "require_project_verification",
    "project_execution_binding",
    "host_project_execution_binding",
]

MAX_REPORT_BYTES = 4 * 1024 * 1024
DEFAULT_PROTECTED_PATHS = (
    ".github/workflows/**",
    ".gitlab-ci.yml",
    ".gitlab/**",
    "AGENTS.md",
    "**/AGENTS.md",
    "CLAUDE.md",
    "**/CLAUDE.md",
    "pyproject.toml",
    "setup.cfg",
    "tox.ini",
    "pytest.ini",
    "ruff.toml",
    ".ruff.toml",
    "mypy.ini",
    "requirements*.txt",
    "requirements/**",
    "Pipfile.lock",
    "poetry.lock",
    "uv.lock",
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "tests/**",
    "test/**",
    "architecture/**",
    "**/architecture/**",
)


class VerificationError(ValueError):
    """Named fail-closed verification configuration or evidence error."""


def _canonical_commands(commands: tuple[tuple[str, ...], ...], label: str) -> tuple[tuple[str, ...], ...]:
    if not isinstance(commands, tuple) or not commands:
        raise VerificationError(f"{label}_required")
    result: list[tuple[str, ...]] = []
    for command in commands:
        if not isinstance(command, tuple) or not command or not all(isinstance(arg, str) and arg for arg in command):
            raise VerificationError(f"{label}_argv_invalid")
        result.append(command)
    return tuple(result)


@dataclass(frozen=True)
class VerificationPolicy:
    test: tuple[tuple[str, ...], ...]
    lint: tuple[tuple[str, ...], ...]
    architecture: tuple[tuple[str, ...], ...]
    protected_paths: tuple[str, ...]
    toolchain_id: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "test", _canonical_commands(self.test, "test_verification"))
        object.__setattr__(self, "lint", _canonical_commands(self.lint, "lint_verification"))
        object.__setattr__(self, "architecture", _canonical_commands(self.architecture, "architecture_verification"))
        if not isinstance(self.toolchain_id, str) or not self.toolchain_id.strip():
            raise VerificationError("toolchain_id_required")
        paths = tuple(dict.fromkeys((*DEFAULT_PROTECTED_PATHS, *self.protected_paths)))
        if not all(isinstance(path, str) and path and "\x00" not in path for path in paths):
            raise VerificationError("protected_paths_invalid")
        object.__setattr__(self, "protected_paths", paths)

    def to_dict(self) -> dict[str, Any]:
        return {
            "test": [list(command) for command in self.test],
            "lint": [list(command) for command in self.lint],
            "architecture": [list(command) for command in self.architecture],
            "protected_paths": list(self.protected_paths),
            "toolchain_id": self.toolchain_id,
        }

    @classmethod
    def from_dict(cls, value: Any) -> "VerificationPolicy":
        if not isinstance(value, dict):
            raise VerificationError("verification_policy_invalid")
        allowed = {"test", "lint", "architecture", "protected_paths", "toolchain_id"}
        if set(value) != allowed:
            raise VerificationError("verification_policy_keys_invalid")

        def commands(name: str) -> tuple[tuple[str, ...], ...]:
            raw = value[name]
            if not isinstance(raw, list):
                raise VerificationError(f"{name}_verification_argv_invalid")
            return tuple(tuple(command) if isinstance(command, list) else () for command in raw)

        paths = value["protected_paths"]
        if not isinstance(paths, list) or not all(isinstance(path, str) for path in paths):
            raise VerificationError("protected_paths_invalid")
        return cls(commands("test"), commands("lint"), commands("architecture"), tuple(paths), value["toolchain_id"])

    def digest(self) -> str:
        encoded = json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class GateEvidence:
    stage: str
    category: str
    argv: tuple[str, ...]
    outcome: str
    exit_code: int | None
    collected: int | None
    passed: int | None
    skipped: int | None
    head_sha: str
    policy_digest: str


def require_project_verification(project: Project) -> VerificationPolicy:
    policy = getattr(project, "verification_policy", None)
    if not isinstance(policy, VerificationPolicy):
        raise VerificationError(f"verification_policy_required: {project.id}")
    return policy


def project_execution_binding(project: Project, base_sha: str, *, hook_digest: str | None = None) -> dict[str, str]:
    policy = require_project_verification(project)
    if not isinstance(base_sha, str) or not base_sha:
        raise VerificationError(f"base_sha_required: {project.id}")
    if hook_digest is None:
        raise VerificationError(f"hook_policy_required: {project.id}")
    if not isinstance(hook_digest, str) or not hook_digest:
        raise VerificationError(f"hook_policy_required: {project.id}")
    return {"base_sha": base_sha, "policy_digest": policy.digest(), "hook_digest": hook_digest}


def host_project_execution_binding(project: Project, base_sha: str) -> dict[str, str]:
    """Resolve Task4's pinned hook policy before creating a host binding."""
    try:
        from whilly.adapters.filesystem.swarm_workspace import resolve_hook_policy

        resolved = resolve_hook_policy(Path(project.path), project.hook_policy or {})
        return project_execution_binding(project, base_sha, hook_digest=str(resolved["digest"]))
    except VerificationError:
        raise
    except Exception as exc:
        raise VerificationError(f"hook_policy_required: {project.id}") from exc


def protected_changes(paths: Sequence[str], policy: VerificationPolicy) -> tuple[str, ...]:
    result: list[str] = []
    for path in paths:
        normalized = path.replace("\\", "/")
        while normalized.startswith("./"):
            normalized = normalized[2:]
        if any(
            fnmatch.fnmatch(normalized, pattern) or fnmatch.fnmatch(normalized, pattern.lstrip("**/"))
            for pattern in policy.protected_paths
        ):
            if path not in result:
                result.append(path)
    return tuple(result)


def parse_gate_result(
    kind: str,
    process: ProcessOutcome,
    report: bytes,
    *,
    stage: str,
    category: str,
    argv: tuple[str, ...],
    head_sha: str,
    policy_digest: str,
) -> GateEvidence:
    """Parse a host-captured report using an enrolled parser schema.

    ``pytest-junit`` consumes JUnit XML, ``ruff-json`` consumes a JSON array,
    and ``architecture-json`` consumes ``{"evaluated_rules": N,
    "violations": [...]}``. A parser kind is never inferred from worker text.
    """
    if kind not in {"pytest-junit", "ruff-json", "architecture-json"}:
        raise VerificationError(f"unsupported_parser: {kind}")
    if len(report) > MAX_REPORT_BYTES:
        raise VerificationError("report_too_large")
    base = dict(
        stage=stage,
        category=category,
        argv=argv,
        exit_code=getattr(process, "exit_code", None),
        collected=None,
        passed=None,
        skipped=None,
        head_sha=head_sha,
        policy_digest=policy_digest,
    )
    if getattr(process, "timed_out", False):
        return GateEvidence(outcome="timed_out", **base)
    if getattr(process, "cancelled", False):
        return GateEvidence(outcome="not_run", **base)
    reason = getattr(process, "reason", None) or getattr(process, "spawn_error", None)
    if reason:
        named = str(reason)
        if named.startswith("execution_isolation_unavailable") or named == "isolation_unavailable":
            outcome = "isolation_unavailable"
        else:
            outcome = named if named in {"dependency_missing", "policy_changed"} else "not_run"
        return GateEvidence(outcome=outcome, **base)
    if not report:
        raise VerificationError("report_missing")
    if kind == "pytest-junit":
        return _parse_junit(report, base)
    try:
        payload = json.loads(report.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise VerificationError("report_invalid") from exc
    if kind == "ruff-json":
        if not isinstance(payload, list):
            raise VerificationError("report_invalid")
        exit_code = base["exit_code"]
        if exit_code not in (0, 1):
            return GateEvidence(outcome="failed", **base)
        return GateEvidence(outcome="passed" if exit_code == 0 and not payload else "failed", **base)
    if (
        not isinstance(payload, dict)
        or type(payload.get("evaluated_rules")) is not int
        or payload["evaluated_rules"] <= 0
        or not isinstance(payload.get("violations"), list)
    ):
        raise VerificationError("report_invalid")
    violations = len(payload["violations"])
    outcome = "passed" if base["exit_code"] == 0 and violations == 0 else "failed"
    values = {**base, "collected": payload["evaluated_rules"], "passed": payload["evaluated_rules"] - violations}
    return GateEvidence(outcome=outcome, **values)


def _parse_junit(report: bytes, base: dict[str, Any]) -> GateEvidence:
    if re.search(rb"<!DOCTYPE|<!ENTITY", report, flags=re.IGNORECASE):
        raise VerificationError("report_invalid")
    try:
        root = ET.fromstring(report)
    except ET.ParseError as exc:
        raise VerificationError("report_invalid") from exc
    cases = root.findall(".//testcase")
    skipped = len(root.findall(".//testcase/skipped"))
    skipped += sum(
        int(case.attrib.get("skipped", "0"))
        for case in root.findall(".//testcase")
        if case.attrib.get("skipped", "0").isdigit()
    )
    if skipped == 0:
        suites = [root, *root.findall(".//testsuite")] if root.tag == "testsuite" else root.findall(".//testsuite")
        skipped = sum(
            int(suite.attrib.get("skipped", "0")) for suite in suites if suite.attrib.get("skipped", "0").isdigit()
        )
    failures = len(root.findall(".//testcase/failure")) + len(root.findall(".//testcase/error"))
    collected = len(cases)
    if not collected:
        outcome = "empty_discovery"
    elif skipped:
        outcome = "skipped_required"
    else:
        outcome = "passed" if base["exit_code"] == 0 and failures == 0 else "failed"
    values = {**base, "collected": collected, "passed": max(collected - skipped - failures, 0), "skipped": skipped}
    return GateEvidence(outcome=outcome, **values)
