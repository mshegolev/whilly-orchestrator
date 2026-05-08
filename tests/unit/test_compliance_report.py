from __future__ import annotations

import json
from pathlib import Path

from whilly.cli.compliance import run_compliance_command
from whilly.compliance import CapabilityStatus, build_compliance_report, render_markdown

SEMANTIC_MEMORY_DEFERRAL = (
    "Semantic memory is explicitly deferred from current scope; deterministic events, task history, PR evidence, "
    "and verification logs remain authoritative."
)
BOUNDED_CI_SCOPE = "No continuous polling, auto-merge, production recovery, or unbounded repair is claimed."
USER_FACING_SCOPE_DOCS = (
    "README.md",
    "README-RU.md",
    "docs/index.md",
    "docs/Project-Description.md",
)


def _write_repo_file(repo: Path, relative_path: str, content: str) -> None:
    path = repo / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_report_model_classifies_capabilities_and_partial_helper_evidence() -> None:
    report = build_compliance_report(repo_root=Path.cwd())

    statuses = {item.status for item in report.matrix}
    assert CapabilityStatus.PASS in statuses
    assert CapabilityStatus.PARTIAL in statuses
    assert statuses <= set(CapabilityStatus)
    assert CapabilityStatus.FAIL.value == "FAIL"
    assert CapabilityStatus.UNKNOWN.value == "UNKNOWN"

    automatic_pr = report.capability("Automatic PR creation after DONE")
    assert automatic_pr.status is CapabilityStatus.PARTIAL
    assert "helper exists" in automatic_pr.evidence.lower()
    assert "not enabled by default" in automatic_pr.gap.lower()

    required_verification = report.capability("Required verification before DONE")
    assert required_verification.status is CapabilityStatus.PASS
    assert "verification_failed" in required_verification.evidence
    assert "when commands are configured" in required_verification.gap.lower()
    assert "profile-native verification command wiring remains future work" not in required_verification.gap.lower()

    payload = report.to_dict()
    assert set(payload) >= {
        "summary",
        "matrix",
        "findings",
        "doc_mismatches",
        "gaps",
        "security_risks",
        "implementation_tasks",
        "acceptance_criteria",
    }


def test_human_review_compliance_reports_admin_controls_and_remaining_ui_gap() -> None:
    report = build_compliance_report(repo_root=Path.cwd())

    finding = report.capability("Human review checkpoint model")

    assert finding.status is CapabilityStatus.PASS
    assert "admin human-review decision endpoint" in finding.evidence.lower()
    assert "release-hold enforcement" in finding.evidence.lower()
    assert "dashboard/tui operator controls" in finding.evidence.lower()
    assert "dashboard/tui operator controls" not in finding.gap.lower()
    assert "approval capture/enforcement is not yet" not in finding.gap.lower()


def test_profile_native_verification_compliance_reports_separate_honest_capability() -> None:
    report = build_compliance_report(repo_root=Path.cwd())

    profile_native = report.capability("Profile-native verification commands")

    assert profile_native.status is CapabilityStatus.PASS
    evidence = profile_native.evidence
    assert "ProjectConfig.verification_commands" in evidence
    assert "Plan.verification_commands" in evidence
    assert "resolve_verification_specs" in evidence
    assert "remote plan metadata" in evidence
    wording = f"{profile_native.evidence} {profile_native.gap} {profile_native.recommended_action}".lower()
    assert "configured profile commands feed runtime verification" in wording
    assert "exhaustive" not in wording
    assert "every profile" not in wording


def test_git_rollback_compliance_reports_phase10_safety_net_without_overclaiming() -> None:
    report = build_compliance_report(repo_root=Path.cwd())

    git_rollback = report.capability("Git rollback")

    assert git_rollback.status is CapabilityStatus.PASS
    assert "backup tags" in git_rollback.evidence
    assert "preflight reports" in git_rollback.evidence
    assert "confirmation-gated restore" in git_rollback.evidence
    assert "PR push preflight" in git_rollback.evidence
    wording = f"{git_rollback.evidence} {git_rollback.gap} {git_rollback.recommended_action}"
    assert "operator-triggered only; no autonomous recovery" in wording
    assert "auto-merge" not in wording.lower()
    assert "automatic production recovery" not in wording.lower()


def test_bounded_ci_polling_and_repair_compliance_is_scoped() -> None:
    report = build_compliance_report(repo_root=Path.cwd())

    finding = report.capability("Bounded CI polling and repair")

    assert finding.status is CapabilityStatus.PASS
    wording = f"{finding.evidence} {finding.gap} {finding.recommended_action}"
    assert "explicit configured CI polling" in wording
    assert "bounded repair attempts" in wording
    assert "repair.escalated" in wording
    assert "No continuous polling, auto-merge, production recovery, or unbounded repair is claimed." in wording


def test_bounded_ci_polling_and_repair_compliance_requires_runtime_evidence(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _write_repo_file(repo, "whilly/ci/verification.py", "def ci_result_to_verification_result(): pass\n")
    _write_repo_file(repo, "whilly/repair/policy.py", "REPAIR_ACTION_REQUEST = 'request_repair'\n")
    _write_repo_file(
        repo,
        "whilly/worker/local.py",
        "ci.poll.result\nrepair.escalated\nrepair.attempt.completed\n",
    )
    _write_repo_file(repo, "whilly/worker/remote.py", "ci.poll.result\nrepair.escalated\n")
    _write_repo_file(repo, "whilly/adapters/transport/server.py", "ci.\nrepair.\n")
    _write_repo_file(
        repo,
        "tests/unit/test_local_worker.py",
        "test_local_worker_records_ci_poll_events_before_verification_failure\n",
    )
    _write_repo_file(
        repo,
        "tests/unit/test_remote_worker.py",
        "test_remote_worker_records_ci_poll_events_before_verification_failure\n",
    )

    finding = build_compliance_report(repo_root=repo, doc_root=repo / "docs").capability(
        "Bounded CI polling and repair"
    )

    assert finding.status is not CapabilityStatus.PASS
    assert "remote worker repair.attempt.completed" in finding.gap
    assert "focused local worker repair tests" in finding.gap
    assert "focused remote worker repair tests" in finding.gap


def test_bounded_ci_polling_and_repair_compliance_does_not_overclaim_autonomy() -> None:
    report = build_compliance_report(repo_root=Path.cwd())

    finding = report.capability("Bounded CI polling and repair")
    allowed_negative_scope = "No continuous polling, auto-merge, production recovery, or unbounded repair is claimed."
    wording_without_scope_boundary = (
        f"{finding.evidence} {finding.gap} {finding.recommended_action}".replace(allowed_negative_scope, "")
    ).lower()

    for forbidden_claim in ("continuous autonomous", "auto-merge", "production recovery", "unbounded repair"):
        assert forbidden_claim not in wording_without_scope_boundary


def test_governance_policy_compliance_reports_required_categories() -> None:
    report = build_compliance_report(repo_root=Path.cwd())

    finding = report.capability("Governance risk policy")

    assert finding.status is CapabilityStatus.PASS
    assert finding.evidence == (
        "Deterministic governance policy covers migration, auth, infrastructure, dependencies, release, "
        "and external_pr risk categories with inspectable reasons and operator approval boundaries."
    )
    assert (
        "Governance policy recommends or requires gates; it does not claim autonomous production release "
        "or default auto-merge."
    ) in f"{finding.gap} {finding.recommended_action}"


def test_governance_policy_compliance_requires_code_and_tests(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _write_repo_file(
        repo,
        "whilly/core/governance.py",
        'REQUIRED_GOVERNANCE_CATEGORIES = ("migration", "auth", "infrastructure")\n',
    )

    finding = build_compliance_report(repo_root=repo, doc_root=repo / "docs").capability("Governance risk policy")

    assert finding.status is not CapabilityStatus.PASS
    assert "missing governance evidence" in finding.gap.lower()
    assert "tests/unit/core/test_governance_policy.py" in finding.gap


def test_semantic_memory_compliance_is_explicit_deferral_not_implemented_claim() -> None:
    report = build_compliance_report(repo_root=Path.cwd())

    finding = report.capability("Semantic memory")

    assert finding.status is CapabilityStatus.PARTIAL
    assert finding.evidence == (
        "Semantic memory is explicitly deferred from current scope; deterministic events, task history, "
        "PR evidence, and verification logs remain authoritative."
    )
    assert (
        finding.gap
        == "No deterministic semantic-memory runtime module is wired into worker task planning or completion."
    )
    assert finding.recommended_action == (
        "Keep semantic recall out of current-capability claims until it is deterministic, evidence-backed, "
        "and wired into planning or completion."
    )
    assert "semantic memory is implemented" not in render_markdown(report).lower()


def test_doc_mismatch_scan_allows_explicit_semantic_memory_deferral(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    docs = repo / "docs"
    docs.mkdir(parents=True)
    (repo / "README.md").write_text(
        "Semantic long-term memory is explicitly deferred from current scope; deterministic events, "
        "task history, PR evidence, and verification logs remain authoritative.\n",
        encoding="utf-8",
    )
    for relative in ("Whilly-v4-Architecture.md", "Whilly-Usage.md", "CODEX-MISSION.md"):
        (docs / relative).write_text("Current capability boundaries are documented here.\n", encoding="utf-8")

    report = build_compliance_report(repo_root=repo, doc_root=docs)

    assert not any("claims semantic long-term memory" in item for item in report.doc_mismatches)


def test_current_vs_target_docs_are_synchronized_with_compliance_scope() -> None:
    current = Path("docs/Current-vs-Target.md").read_text(encoding="utf-8")
    report_markdown = render_markdown(build_compliance_report(repo_root=Path.cwd()))

    for phrase in (
        "profile-native verification commands feed runtime verification",
        "operator-triggered rollback",
        "explicit configured CI polling",
        "bounded repair attempts",
        "deterministic governance risk policy",
        SEMANTIC_MEMORY_DEFERRAL,
        BOUNDED_CI_SCOPE,
    ):
        assert phrase in current

    for phrase in (
        "operator-triggered rollback",
        "explicit configured CI polling",
        "bounded repair attempts",
        "deterministic governance risk policy",
        SEMANTIC_MEMORY_DEFERRAL,
        BOUNDED_CI_SCOPE,
    ):
        assert phrase in report_markdown


def test_user_facing_docs_do_not_claim_deferred_capabilities() -> None:
    forbidden_current_claims = (
        "semantic long-term memory",
        "default auto-merge",
        "continuous autonomous repair",
        "autonomous production release",
        "autonomous production recovery",
        "full sandbox/VM isolation is implemented",
    )

    for relative in USER_FACING_SCOPE_DOCS:
        text = Path(relative).read_text(encoding="utf-8")
        lowered = text.lower()
        assert "control plane" in lowered
        assert "operator-triggered rollback" in text
        assert "explicit configured CI polling" in text
        assert "bounded repair attempts" in text
        assert "deterministic governance risk policy" in text
        assert SEMANTIC_MEMORY_DEFERRAL in text
        assert BOUNDED_CI_SCOPE in text
        for forbidden in forbidden_current_claims:
            assert forbidden not in lowered


def test_target_docs_keep_semantic_memory_future_scope() -> None:
    target_guide = Path("docs/target/04_Compliance_Validation_Guide.md").read_text(encoding="utf-8")
    target_roadmap = Path("docs/target/06_Autonomous_Developer_Roadmap.md").read_text(encoding="utf-8")
    combined = f"{target_guide}\n{target_roadmap}"

    assert SEMANTIC_MEMORY_DEFERRAL in target_guide
    assert "Semantic memory | Future target" in target_guide
    assert "future target architecture" in target_roadmap
    assert "deterministic events, task history, PR evidence, and verification logs remain authoritative" in combined
    assert "semantic memory is implemented" not in combined.lower()
    assert "provides semantic long-term memory" not in combined.lower()


def test_sandbox_compliance_reports_guard_evidence_without_overclaiming_isolation() -> None:
    report = build_compliance_report(repo_root=Path.cwd())

    finding = report.capability("Sandbox/VM isolation")

    assert finding.status is CapabilityStatus.PARTIAL
    assert "prompt, shell, secret, and runner-env guards" in finding.evidence
    assert "No per-task VM/container sandbox isolation is enforced by the worker runtime." in finding.gap
    assert report.security_risks[0] == (
        "No per-task VM/container sandbox isolation; prompt, shell, secret, and runner-env guards reduce but do "
        "not eliminate agent execution risk."
    )


def test_markdown_renderer_includes_required_sections_and_matrix() -> None:
    report = build_compliance_report(repo_root=Path.cwd())
    markdown = render_markdown(report)

    assert markdown.startswith("# Whilly Compliance Validation Report")
    for heading in [
        "## Summary",
        "## Capability Matrix",
        "## Critical Findings",
        "## Documentation Mismatches",
        "## Implementation Gaps",
        "## Security and Safety Risks",
        "## Recommended Implementation Tasks",
        "## Acceptance Criteria for Remediation",
    ]:
        assert heading in markdown
    assert "| Capability | Status | Evidence | Gap | Recommended action |" in markdown
    assert "| Automatic PR creation after DONE | PARTIAL |" in markdown


def test_compliance_report_command_writes_json_and_markdown(tmp_path: Path) -> None:
    json_out = tmp_path / "report.json"
    md_out = tmp_path / "report.md"

    assert run_compliance_command(["report", "--format", "json", "--out", str(json_out)]) == 0
    assert run_compliance_command(["report", "--format", "markdown", "--out", str(md_out)]) == 0

    payload = json.loads(json_out.read_text(encoding="utf-8"))
    assert payload["summary"]["overall_status"] in {"PASS", "PARTIAL", "FAIL", "UNKNOWN"}
    assert payload["matrix"]
    assert md_out.read_text(encoding="utf-8").startswith("# Whilly Compliance Validation Report")


def test_doc_mismatch_scan_ignores_negative_boundary_claims(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    docs = repo / "docs"
    docs.mkdir(parents=True)
    (repo / "README.md").write_text(
        "Whilly is not a fully autonomous developer and does **not** claim full multi-repo execution, "
        "\nfull sandbox isolation, or semantic long-term memory.\n",
        encoding="utf-8",
    )
    for relative in ("Whilly-v4-Architecture.md", "Whilly-Usage.md", "CODEX-MISSION.md"):
        (docs / relative).write_text("Current capability boundaries are documented here.\n", encoding="utf-8")

    report = build_compliance_report(repo_root=repo, doc_root=docs)

    assert not any(item.startswith("README.md:") for item in report.doc_mismatches)


def test_doc_mismatch_scan_ignores_long_negative_boundary_list(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    docs = repo / "docs"
    docs.mkdir(parents=True)
    (repo / "README.md").write_text(
        "The core worker loop does **not** claim all of the following as complete product "
        "guarantees: full multi-repo execution, automatic PR review feedback loops, "
        "mandatory CI/lint verification unless verification commands are configured, "
        "full sandbox or VM isolation, semantic long-term memory, reliable git rollback, "
        "or autonomous production release without human review.\n",
        encoding="utf-8",
    )
    for relative in ("Whilly-v4-Architecture.md", "Whilly-Usage.md", "CODEX-MISSION.md"):
        (docs / relative).write_text("Current capability boundaries are documented here.\n", encoding="utf-8")

    report = build_compliance_report(repo_root=repo, doc_root=docs)

    assert not any(item.startswith("README.md:") for item in report.doc_mismatches)


def test_doc_mismatch_scan_still_flags_positive_claims(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    docs = repo / "docs"
    docs.mkdir(parents=True)
    (repo / "README.md").write_text(
        "Whilly provides full sandbox or VM isolation and semantic long-term memory.\n",
        encoding="utf-8",
    )
    for relative in ("Whilly-v4-Architecture.md", "Whilly-Usage.md", "CODEX-MISSION.md"):
        (docs / relative).write_text("Current capability boundaries are documented here.\n", encoding="utf-8")

    report = build_compliance_report(repo_root=repo, doc_root=docs)

    assert any("claims full sandbox/VM isolation" in item for item in report.doc_mismatches)
    assert any("claims semantic long-term memory" in item for item in report.doc_mismatches)
