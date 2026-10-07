from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from whilly.swarm.registry import Project
from whilly.swarm.verification import (
    VerificationError,
    VerificationPolicy,
    parse_gate_result,
    protected_changes,
    project_execution_binding,
    require_project_verification,
)


def _process(*, exit_code: int | None = 0, timed_out: bool = False, spawn_error: str | None = None):
    return SimpleNamespace(exit_code=exit_code, timed_out=timed_out, cancelled=False, spawn_error=spawn_error)


def _policy() -> VerificationPolicy:
    return VerificationPolicy(
        test=(("python", "-m", "pytest", "--junitxml", "reports/test.xml"),),
        lint=(("ruff", "check", "--output-format", "json", "."),),
        architecture=(("python", "scripts/architecture.py", "--json"),),
        protected_paths=("custom-policy.toml",),
        toolchain_id="python312-ruff",
    )


def test_policy_digest_is_canonical_and_required_defaults_are_protected():
    policy = _policy()
    same = VerificationPolicy(
        test=policy.test,
        lint=policy.lint,
        architecture=policy.architecture,
        protected_paths=("custom-policy.toml",),
        toolchain_id="python312-ruff",
    )
    assert policy.digest() == same.digest()
    changes = protected_changes(("AGENTS.md", ".gitlab-ci.yml", "pyproject.toml", "src/app.py"), policy)
    assert changes == ("AGENTS.md", ".gitlab-ci.yml", "pyproject.toml")


def test_legacy_project_verification_cannot_satisfy_guarded_readiness(tmp_path):
    project = Project("demo", str(tmp_path), "main", "demo", verification=(("pytest",),))
    with pytest.raises(VerificationError, match="verification_policy_required"):
        require_project_verification(project)


def test_project_binding_requires_resolved_hook_digest(tmp_path):
    policy = _policy()
    project = Project(
        "demo",
        str(tmp_path),
        "main",
        "demo",
        verification_policy=policy,
    )
    with pytest.raises(VerificationError, match="hook_policy_required"):
        project_execution_binding(project, "a" * 40)


def test_junit_requires_real_tests_and_rejects_required_skips():
    report = b'<testsuite tests="0" failures="0" errors="0" skipped="0"></testsuite>'
    evidence = parse_gate_result(
        "pytest-junit",
        _process(),
        report,
        stage="candidate",
        category="test",
        argv=("pytest",),
        head_sha="a" * 40,
        policy_digest="b" * 64,
    )
    assert evidence.outcome == "empty_discovery"

    report = b'<testsuite tests="1" failures="0" errors="0" skipped="1"><testcase /></testsuite>'
    evidence = parse_gate_result(
        "pytest-junit",
        _process(),
        report,
        stage="candidate",
        category="test",
        argv=("pytest",),
        head_sha="a" * 40,
        policy_digest="b" * 64,
    )
    assert evidence.outcome == "skipped_required"


def test_structured_parsers_fail_closed_and_prove_tool_specific_success():
    with pytest.raises(VerificationError, match="report_invalid"):
        parse_gate_result(
            "pytest-junit",
            _process(),
            b"<!DOCTYPE testsuite [<!ENTITY x SYSTEM 'file:///etc/passwd'>]><testsuite />",
            stage="candidate",
            category="test",
            argv=("pytest",),
            head_sha="a" * 40,
            policy_digest="b" * 64,
        )
    with pytest.raises(VerificationError, match="report_too_large"):
        parse_gate_result(
            "ruff-json",
            _process(),
            b"[]" * 3 * 1024 * 1024,
            stage="candidate",
            category="lint",
            argv=("ruff",),
            head_sha="a" * 40,
            policy_digest="b" * 64,
        )
    evidence = parse_gate_result(
        "ruff-json",
        _process(exit_code=0),
        b"[]",
        stage="candidate",
        category="lint",
        argv=("ruff",),
        head_sha="a" * 40,
        policy_digest="b" * 64,
    )
    assert evidence.outcome == "passed"
    architecture = json.dumps({"evaluated_rules": 2, "violations": []}).encode()
    evidence = parse_gate_result(
        "architecture-json",
        _process(exit_code=0),
        architecture,
        stage="candidate",
        category="architecture",
        argv=("arch",),
        head_sha="a" * 40,
        policy_digest="b" * 64,
    )
    assert evidence.outcome == "passed" and evidence.collected == 2
    with pytest.raises(VerificationError, match="unsupported_parser"):
        parse_gate_result(
            "shell",
            _process(),
            b"ok",
            stage="candidate",
            category="test",
            argv=("sh",),
            head_sha="a" * 40,
            policy_digest="b" * 64,
        )
