from __future__ import annotations

import json
from pathlib import Path

from whilly.cli.compliance import run_compliance_command
from whilly.compliance import CapabilityStatus, build_compliance_report, render_markdown


def test_report_model_classifies_capabilities_and_partial_helper_evidence() -> None:
    report = build_compliance_report(repo_root=Path.cwd())

    statuses = {item.status for item in report.matrix}
    assert CapabilityStatus.PASS in statuses
    assert CapabilityStatus.PARTIAL in statuses
    assert CapabilityStatus.FAIL in statuses
    assert CapabilityStatus.UNKNOWN.value == "UNKNOWN"

    automatic_pr = report.capability("Automatic PR creation after DONE")
    assert automatic_pr.status is CapabilityStatus.PARTIAL
    assert "helper exists" in automatic_pr.evidence.lower()
    assert "not enabled by default" in automatic_pr.gap.lower()

    required_verification = report.capability("Required verification before DONE")
    assert required_verification.status is CapabilityStatus.PASS
    assert "verification_failed" in required_verification.evidence
    assert "when commands are configured" in required_verification.gap.lower()

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
