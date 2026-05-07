"""Compliance report generation for Whilly target-doc validation."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path

DEFAULT_DOC_ROOT = Path(__file__).resolve().parents[2] / "docs" / "target"


class CapabilityStatus(str, Enum):
    PASS = "PASS"
    PARTIAL = "PARTIAL"
    FAIL = "FAIL"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class CapabilityFinding:
    capability: str
    status: CapabilityStatus
    evidence: str
    gap: str
    recommended_action: str

    def to_dict(self) -> dict[str, str]:
        return {
            "capability": self.capability,
            "status": self.status.value,
            "evidence": self.evidence,
            "gap": self.gap,
            "recommended_action": self.recommended_action,
        }


@dataclass(frozen=True)
class ComplianceReport:
    target_spec_version: str
    repository_commit: str
    generated_at: str
    overall_status: CapabilityStatus
    matrix: tuple[CapabilityFinding, ...]
    findings: tuple[str, ...]
    doc_mismatches: tuple[str, ...]
    gaps: tuple[str, ...]
    security_risks: tuple[str, ...]
    implementation_tasks: tuple[str, ...]
    acceptance_criteria: tuple[str, ...]

    def capability(self, name: str) -> CapabilityFinding:
        for item in self.matrix:
            if item.capability == name:
                return item
        raise KeyError(name)

    def to_dict(self) -> dict[str, object]:
        return {
            "summary": {
                "overall_status": self.overall_status.value,
                "target_spec_version": self.target_spec_version,
                "repository_commit": self.repository_commit,
                "date": self.generated_at,
            },
            "matrix": [item.to_dict() for item in self.matrix],
            "findings": list(self.findings),
            "doc_mismatches": list(self.doc_mismatches),
            "gaps": list(self.gaps),
            "security_risks": list(self.security_risks),
            "implementation_tasks": list(self.implementation_tasks),
            "acceptance_criteria": list(self.acceptance_criteria),
        }


def build_compliance_report(
    *,
    repo_root: Path | str = ".",
    doc_root: Path | str = DEFAULT_DOC_ROOT,
) -> ComplianceReport:
    root = Path(repo_root).resolve()
    docs = Path(doc_root)
    files = _RepoFiles(root)
    matrix = (
        _cap(
            "JSON plan import",
            CapabilityStatus.PASS
            if files.exists("whilly/cli/plan.py", "whilly/adapters/filesystem/plan_io.py")
            else CapabilityStatus.FAIL,
            "whilly/cli/plan.py and whilly/adapters/filesystem/plan_io.py implement import/export parsing.",
            "",
            "Keep plan import regression tests green.",
        ),
        _cap(
            "GitHub issue source",
            CapabilityStatus.PASS
            if files.exists("whilly/sources/github_issues.py", "whilly/sources/github_issues_and_project.py")
            else CapabilityStatus.FAIL,
            "GitHub source adapters exist under whilly/sources.",
            "",
            "Validate authentication and boundary handling before claiming broader GitHub automation.",
        ),
        _cap(
            "Jira source",
            CapabilityStatus.PASS if files.exists("whilly/sources/jira.py") else CapabilityStatus.FAIL,
            "whilly/sources/jira.py implements Jira collection and conversion.",
            "",
            "Keep Jira TLS/auth configuration documented and tested.",
        ),
        _cap(
            "Forge/PRD intake",
            CapabilityStatus.PASS
            if files.exists("whilly/forge/intake.py", "whilly/prd_generator.py")
            else CapabilityStatus.FAIL,
            "Forge intake and PRD generation entry points are present; PRD generation involves an LLM path.",
            "",
            "Document nondeterministic LLM involvement when presenting Forge/PRD intake.",
        ),
        _cap(
            "Postgres task state",
            CapabilityStatus.PASS
            if files.contains("whilly/adapters/db/repository.py", "TaskRepository")
            and files.exists("whilly/adapters/db/schema.sql")
            else CapabilityStatus.FAIL,
            "TaskRepository and schema.sql provide the Postgres-backed task state surface.",
            "",
            "Continue validating transactional state transitions with database-backed tests.",
        ),
        _cap(
            "Dependency/cycle checks",
            CapabilityStatus.PASS
            if files.contains("whilly/core/scheduler.py", "def detect_cycles")
            else CapabilityStatus.FAIL,
            "whilly/core/scheduler.py exposes deterministic cycle detection.",
            "",
            "Keep dependency validation in plan import and config-generation paths.",
        ),
        _cap(
            "Decision gates",
            CapabilityStatus.PASS
            if files.contains("whilly/core/gates.py", "evaluate_decision_gate")
            else CapabilityStatus.FAIL,
            "whilly/core/gates.py evaluates description, acceptance-criteria, and test-step gates.",
            "",
            "Keep strict/default behavior explicit in CLI and docs.",
        ),
        _cap(
            "Worker claim with SKIP LOCKED",
            CapabilityStatus.PASS
            if files.contains("whilly/adapters/db/repository.py", "SKIP LOCKED")
            else CapabilityStatus.FAIL,
            "claim_task SQL uses FOR UPDATE OF t SKIP LOCKED.",
            "",
            "Keep concurrency tests around claim ordering and lost races.",
        ),
        _cap(
            "Prompt injection guard",
            CapabilityStatus.PASS
            if files.contains("whilly/core/prompts.py", "PROMPT_INJECTION_BLOCKED_EVENT_TYPE")
            else CapabilityStatus.FAIL,
            "Prompt construction has baseline injection-deny patterns and audit event naming.",
            "",
            "Extend coverage as new untrusted text slots are added.",
        ),
        _cap(
            "Dangerous command guard",
            CapabilityStatus.PASS
            if files.contains("whilly/core/agent_runner.py", "SHELL_COMMAND_BLOCKED_EVENT_TYPE")
            and files.contains("whilly/worker/local.py", "scan_task_command_surface")
            else CapabilityStatus.FAIL,
            "Shell deny-list scanner exists and the local worker scans task command surfaces before runner invocation.",
            "",
            "Keep deny-list placement before local and remote runner calls.",
        ),
        _cap(
            "Runner abstraction",
            CapabilityStatus.PASS
            if files.contains("whilly/adapters/runner/result_parser.py", "class AgentResult")
            else CapabilityStatus.FAIL,
            "Runner adapters normalize subprocess output into AgentResult.",
            "",
            "Keep backend adapters returning the same result contract.",
        ),
        _cap(
            "Completion marker parsing",
            CapabilityStatus.PASS
            if files.contains("whilly/adapters/runner/result_parser.py", "<promise>COMPLETE</promise>")
            else CapabilityStatus.FAIL,
            "result_parser.py treats the completion marker as AgentResult.is_complete evidence.",
            "",
            "Keep marker semantics aligned with prompt construction.",
        ),
        _cap(
            "Required verification before DONE",
            _verification_status(files),
            "whilly/verifier.py helper exists, but worker/local.py completes tasks directly from AgentResult.is_complete.",
            "helper exists but not wired into the main DONE transition path.",
            "Wire verification into the worker completion path or model verification state before treating DONE as verified.",
        ),
        _cap(
            "Project profiles",
            CapabilityStatus.PASS
            if files.exists(
                "whilly/project_config/models.py", "whilly/project_config/loader.py", "whilly/cli/project_config.py"
            )
            else CapabilityStatus.FAIL,
            "ProjectConfig, loader validation, presets, and a project-config CLI are present.",
            "",
            "Keep generated plans preserving project type, repos, and stage metadata.",
        ),
        _cap(
            "Configurable pipeline stages",
            CapabilityStatus.PARTIAL
            if files.contains("whilly/project_config/models.py", "class PipelineStepConfig")
            else CapabilityStatus.FAIL,
            "PipelineStepConfig and presets generate plan tasks from configured stages.",
            "Pipeline configuration generates tasks, but runtime worker execution still follows the generic task loop.",
            "Add explicit runtime audit events for configured pipeline stage boundaries.",
        ),
        _cap(
            "Human review checkpoint model",
            CapabilityStatus.PARTIAL
            if files.contains("whilly/project_config/models.py", "class HumanLoopConfig")
            else CapabilityStatus.FAIL,
            "HumanLoopConfig and PipelineStepConfig.human_gate model review requirements.",
            "Review gates are represented in generated tasks, not enforced as a separate runtime approval state.",
            "Emit and enforce auditable approval checkpoints for configured high-risk stages.",
        ),
        _cap(
            "Automatic PR creation after DONE",
            _automatic_pr_status(files),
            "GitHub PR helper exists and cli/run.py can build a post-complete hook when WHILLY_AUTO_OPEN_PR=1.",
            "helper exists but automatic PR opening is not enabled by default and depends on opt-in runtime configuration.",
            "Do not claim unconditional PR creation; keep it documented as opt-in behavior.",
        ),
        _cap(
            "PR review feedback loop",
            CapabilityStatus.PARTIAL
            if files.exists("whilly/cli/pr_feedback.py", "whilly/sources/github_pr_feedback.py")
            else CapabilityStatus.FAIL,
            "pr-feedback CLI and GitHub PR feedback source exist.",
            "Polling is a separate command/timer surface, not an always-on autonomous repair loop.",
            "Document polling requirements and avoid claiming automatic continuous review remediation.",
        ),
        _cap(
            "Multi-repo task execution",
            CapabilityStatus.PARTIAL
            if files.contains("whilly/project_config/models.py", "repo_full_name")
            and files.contains("whilly/workspaces.py", "repo_target")
            else CapabilityStatus.FAIL,
            "Repo target metadata and workspace preparation code exist.",
            "Execution remains single checked-out workspace per task; no full multi-repo orchestration planner is wired.",
            "Keep multi-repo support described as limited until cross-repo scheduling is implemented.",
        ),
        _cap(
            "Sandbox/VM isolation",
            CapabilityStatus.PARTIAL
            if files.contains("whilly/adapters/runner/claude_cli.py", "--disallowedTools")
            else CapabilityStatus.FAIL,
            "Agent tools default to restricted CLI flags and shell commands are scanned.",
            "No per-task VM/container sandbox isolation is enforced by the worker runtime.",
            "Document residual risk and add isolation only in a hardening slice.",
        ),
        _cap(
            "Semantic memory",
            CapabilityStatus.FAIL,
            "No deterministic semantic-memory runtime module is wired into worker task planning or completion.",
            "Capability is not implemented in this repository slice.",
            "Keep semantic memory out of current-capability claims.",
        ),
        _cap(
            "Git rollback",
            CapabilityStatus.PARTIAL
            if files.contains("whilly/verifier.py", "revert_on_fail")
            else CapabilityStatus.FAIL,
            "verify_task can revert on verification failure with guard checks.",
            "Rollback is tied to verifier helper behavior, not a general smart rollback system in the worker path.",
            "Document limitation and avoid claiming robust smart rollback.",
        ),
        _cap(
            "Observability",
            CapabilityStatus.PASS
            if files.exists("whilly/api/metrics.py", "whilly/api/sse.py", "whilly/audit/jsonl_sink.py")
            else CapabilityStatus.FAIL,
            "Metrics, SSE, dashboard, and JSONL audit surfaces exist.",
            "",
            "Keep event payload schemas and metrics labels under regression tests.",
        ),
    )

    doc_mismatches = _doc_mismatches(root)
    findings = _critical_findings(matrix)
    gaps = tuple(item.gap for item in matrix if item.gap)
    security_risks = (
        "No per-task VM/container sandbox isolation; command guards reduce but do not eliminate agent execution risk.",
        "DONE can still mean agent marker success without mandatory post-task verification in the main worker path.",
        "Opt-in PR opening depends on local git/gh credentials and should remain explicitly configured.",
    )
    implementation_tasks = (
        "Wire verifier results into the DONE transition or add explicit verification state/events.",
        "Enforce human approval checkpoints for configured high-risk pipeline stages.",
        "Emit runtime audit events that name configured pipeline stages.",
        "Keep PR creation and PR-feedback documentation aligned with opt-in/polling behavior.",
    )
    acceptance_criteria = (
        "Compliance report generation succeeds in markdown and JSON formats.",
        "Every capability row uses PASS, PARTIAL, FAIL, or UNKNOWN with concrete repo evidence.",
        "Helpers that are present but not wired into the default runtime path are reported as PARTIAL.",
        "Reports identify documentation mismatches, implementation gaps, and security risks.",
    )
    return ComplianceReport(
        target_spec_version=_target_spec_version(docs, root),
        repository_commit=_git_commit(root),
        generated_at=datetime.now(UTC).date().isoformat(),
        overall_status=_overall_status(matrix),
        matrix=matrix,
        findings=findings,
        doc_mismatches=doc_mismatches,
        gaps=gaps,
        security_risks=security_risks,
        implementation_tasks=implementation_tasks,
        acceptance_criteria=acceptance_criteria,
    )


def render_markdown(report: ComplianceReport) -> str:
    lines = [
        "# Whilly Compliance Validation Report",
        "",
        "## Summary",
        f"- Overall status: {report.overall_status.value}",
        f"- Target spec version: {report.target_spec_version}",
        f"- Repository commit: {report.repository_commit}",
        f"- Date: {report.generated_at}",
        "",
        "## Capability Matrix",
        "| Capability | Status | Evidence | Gap | Recommended action |",
        "|---|---|---|---|---|",
    ]
    for item in report.matrix:
        lines.append(
            "| "
            + " | ".join(
                [
                    _md_cell(item.capability),
                    item.status.value,
                    _md_cell(item.evidence),
                    _md_cell(item.gap or "None"),
                    _md_cell(item.recommended_action),
                ]
            )
            + " |"
        )
    lines.extend(["", "## Critical Findings"])
    lines.extend(_numbered(report.findings))
    lines.extend(["", "## Documentation Mismatches"])
    lines.extend(_numbered(report.doc_mismatches))
    lines.extend(["", "## Implementation Gaps"])
    lines.extend(_numbered(report.gaps))
    lines.extend(["", "## Security and Safety Risks"])
    lines.extend(_numbered(report.security_risks))
    lines.extend(["", "## Recommended Implementation Tasks"])
    lines.extend(_numbered(report.implementation_tasks))
    lines.extend(["", "## Acceptance Criteria for Remediation"])
    lines.extend(f"- {item}" for item in report.acceptance_criteria)
    return "\n".join(lines) + "\n"


def _cap(
    capability: str,
    status: CapabilityStatus,
    evidence: str,
    gap: str,
    recommended_action: str,
) -> CapabilityFinding:
    return CapabilityFinding(capability, status, evidence, gap, recommended_action)


def _verification_status(files: _RepoFiles) -> CapabilityStatus:
    if not files.exists("whilly/verifier.py"):
        return CapabilityStatus.FAIL
    worker_text = files.read("whilly/worker/local.py")
    return CapabilityStatus.PASS if "verify_task" in worker_text else CapabilityStatus.PARTIAL


def _automatic_pr_status(files: _RepoFiles) -> CapabilityStatus:
    if not files.exists("whilly/sinks/github_pr.py", "whilly/sinks/post_complete_pr_hook.py"):
        return CapabilityStatus.FAIL
    if files.contains("whilly/cli/run.py", "WHILLY_AUTO_OPEN_PR=1") and files.contains(
        "whilly/cli/run.py", "post_complete_hook = None"
    ):
        return CapabilityStatus.PARTIAL
    return CapabilityStatus.UNKNOWN


def _overall_status(matrix: tuple[CapabilityFinding, ...]) -> CapabilityStatus:
    statuses = {item.status for item in matrix}
    if CapabilityStatus.FAIL in statuses:
        return CapabilityStatus.FAIL
    if CapabilityStatus.UNKNOWN in statuses:
        return CapabilityStatus.UNKNOWN
    if CapabilityStatus.PARTIAL in statuses:
        return CapabilityStatus.PARTIAL
    return CapabilityStatus.PASS


def _critical_findings(matrix: tuple[CapabilityFinding, ...]) -> tuple[str, ...]:
    important = {
        "Required verification before DONE",
        "Sandbox/VM isolation",
        "Semantic memory",
        "Human review checkpoint model",
    }
    findings = [
        f"{item.capability}: {item.status.value} - {item.gap or item.evidence}"
        for item in matrix
        if item.capability in important and item.status is not CapabilityStatus.PASS
    ]
    return tuple(findings) or ("No critical findings detected by deterministic inspection.",)


def _doc_mismatches(root: Path) -> tuple[str, ...]:
    docs = {
        "README.md": root / "README.md",
        "docs/Whilly-v4-Architecture.md": root / "docs" / "Whilly-v4-Architecture.md",
        "docs/Whilly-Usage.md": root / "docs" / "Whilly-Usage.md",
        "docs/CODEX-MISSION.md": root / "docs" / "CODEX-MISSION.md",
    }
    rules = (
        ("fully autonomous", "may imply Whilly is already a fully autonomous developer."),
        ("DONE always means verified", "claims DONE always means verified code."),
        ("automatically creates PRs", "claims DONE automatically creates PRs."),
        ("full multi-repo", "claims full multi-repo execution."),
        ("full sandbox", "claims full sandbox/VM isolation."),
        ("semantic long-term memory", "claims semantic long-term memory."),
        ("smart rollback", "may overstate robust smart rollback."),
        ("automatically processes PR review feedback", "claims automatic PR review feedback remediation."),
    )
    mismatches: list[str] = []
    for label, path in docs.items():
        try:
            text = path.read_text(encoding="utf-8").lower()
        except OSError:
            mismatches.append(f"{label}: could not inspect documentation file.")
            continue
        for needle, message in rules:
            if _contains_positive_claim(text, needle):
                mismatches.append(f"{label}: {message}")
    return tuple(mismatches) or ("No target-rule documentation mismatches found in required docs.",)


def _contains_positive_claim(text: str, needle: str) -> bool:
    start = 0
    while True:
        index = text.find(needle, start)
        if index < 0:
            return False
        sentence_start = max(text.rfind(".", 0, index), text.rfind(";", 0, index)) + 1
        sentence_end_candidates = [pos for pos in (text.find(".", index), text.find(";", index)) if pos >= 0]
        sentence_end = min(sentence_end_candidates) if sentence_end_candidates else len(text)
        sentence = text[sentence_start:sentence_end].strip()
        prefix = sentence[: max(0, sentence.find(needle))]
        if not _has_negative_boundary(prefix):
            return True
        start = index + len(needle)


def _has_negative_boundary(prefix: str) -> bool:
    normalized = prefix[-160:].replace("*", "").replace("_", "").replace("`", "")
    markers = (
        "does not",
        "do not",
        "should not",
        "not ",
        "not yet",
        "no ",
        "cannot",
        "do n't",
        "does n't",
        "не ",
        "нельзя",
    )
    return any(marker in normalized for marker in markers)


def _target_spec_version(doc_root: Path, repo_root: Path) -> str:
    if (doc_root / "04_Compliance_Validation_Guide.md").exists():
        return "target-doc-pack:04_Compliance_Validation_Guide"
    if (repo_root / "docs" / "target" / "04_Compliance_Validation_Guide.md").exists():
        return "docs/target:04_Compliance_Validation_Guide"
    return "UNKNOWN"


def _git_commit(root: Path) -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=root,
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return "UNKNOWN"
    commit = result.stdout.strip()
    return commit if result.returncode == 0 and commit else "UNKNOWN"


def _numbered(items: tuple[str, ...]) -> list[str]:
    return [f"{idx}. {item}" for idx, item in enumerate(items, start=1)]


def _md_cell(value: str) -> str:
    return value.replace("\n", " ").replace("|", "\\|")


class _RepoFiles:
    def __init__(self, root: Path) -> None:
        self.root = root

    def exists(self, *paths: str) -> bool:
        return all((self.root / path).exists() for path in paths)

    def contains(self, path: str, needle: str) -> bool:
        return needle in self.read(path)

    def read(self, path: str) -> str:
        try:
            return (self.root / path).read_text(encoding="utf-8")
        except OSError:
            return ""


__all__ = [
    "CapabilityFinding",
    "CapabilityStatus",
    "ComplianceReport",
    "DEFAULT_DOC_ROOT",
    "build_compliance_report",
    "render_markdown",
]
