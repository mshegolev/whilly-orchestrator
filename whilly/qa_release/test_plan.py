"""Build QA test plans from collected release context."""

from __future__ import annotations

import re

from whilly.qa_release.models import (
    LinkedIssue,
    QATestCase,
    QATestPlan,
    QATestRequirement,
    ReleaseContext,
    ReleaseLink,
)

_VERSION_RE = re.compile(r"\bv?\d[\w.-]{5,}\b")


def build_test_plan(context: ReleaseContext) -> QATestPlan:
    """Create a deterministic first-pass QA test plan for a release context."""

    requirements = _requirements_from_context(context)
    test_cases: list[QATestCase] = []
    for index, requirement in enumerate(requirements, start=1):
        test_cases.append(_functional_test_case(index, requirement, context))

    next_case_no = len(test_cases) + 1
    if context.repo_hints:
        test_cases.append(_repo_scope_contract_case(next_case_no, context))
        next_case_no += 1
    if _links_by_kind(context.links, "deployment"):
        test_cases.append(_deployment_smoke_case(next_case_no, context))
        next_case_no += 1
    test_cases.append(_regression_case(next_case_no, requirements, context))

    return QATestPlan(
        release_key=context.root_key,
        release_summary=context.root_summary,
        release_url=context.root_url,
        release_version=_release_version(context),
        requirements=tuple(requirements),
        test_cases=tuple(test_cases),
        repo_hints=context.repo_hints,
        artifact_links=context.links,
        preconditions=tuple(_preconditions(context)),
        warnings=context.warnings,
    )


def _requirements_from_context(context: ReleaseContext) -> tuple[QATestRequirement, ...]:
    source_issues = [issue for issue in context.linked_issues if issue.key and issue.key != context.root_key]
    if not source_issues and context.linked_issues:
        source_issues = [context.linked_issues[0]]

    requirements: list[QATestRequirement] = []
    for index, issue in enumerate(source_issues, start=1):
        requirements.append(
            QATestRequirement(
                id=f"REQ-{index:03d}",
                source_issue_key=issue.key,
                title=issue.summary or issue.key,
                source_url=issue.url,
                text=_compact_text(issue.description or issue.summary),
                priority=_priority_from_issue(issue),
            )
        )
    return tuple(requirements)


def _functional_test_case(index: int, requirement: QATestRequirement, context: ReleaseContext) -> QATestCase:
    return QATestCase(
        id=f"TC-{index:03d}",
        title=f"Verify {requirement.source_issue_key}: {requirement.title}",
        kind="functional",
        source_requirement_ids=(requirement.id,),
        source_issue_keys=(requirement.source_issue_key,),
        steps=(
            f"Review requirement source {requirement.source_issue_key}.",
            "Identify ETL input/output tables, partitions, and acceptance rules from linked artifacts.",
            "Add or update an automated functional assertion in the ETL test monorepo.",
            "Run the test against the deployed STAGE release.",
        ),
        expected_results=(
            "The STAGE ETL output satisfies the requirement acceptance rules.",
            "The automated assertion is repeatable and can run in release regression.",
        ),
        automation_hint=(
            f"Create/update pytest coverage for {requirement.source_issue_key} under the release "
            f"{context.root_key} test suite."
        ),
        tags=("qa-release", "functional", context.root_key.lower()),
    )


def _repo_scope_contract_case(index: int, context: ReleaseContext) -> QATestCase:
    repos = ", ".join(_repo_label(repo.repo_full_name, repo.ref_type, repo.ref) for repo in context.repo_hints)
    return QATestCase(
        id=f"TC-{index:03d}",
        title="Pin release implementation repository scope",
        kind="contract",
        steps=(
            "Verify every implementation/deployment repository discovered from Jira links is present in the test plan.",
            "Verify release tags, branches, or commit SHAs are pinned before test execution.",
        ),
        expected_results=(f"Repository scope is explicit and auditable: {repos}.",),
        automation_hint="Keep this as a local contract test so accidental release-scope drift is caught cheaply.",
        tags=("qa-release", "repo-scope", context.root_key.lower()),
    )


def _deployment_smoke_case(index: int, context: ReleaseContext) -> QATestCase:
    return QATestCase(
        id=f"TC-{index:03d}",
        title="Deploy release to STAGE using linked deployment instructions",
        kind="deployment",
        steps=(
            "Open the deployment instruction links discovered from Jira.",
            "Deploy the pinned release version to STAGE using the documented runbook.",
            "Capture deployment command output, deployed version, and environment evidence.",
        ),
        expected_results=(
            "STAGE deployment completes without failed steps.",
            "The deployed version matches the release version from the Jira release context.",
        ),
        automation_hint="Later slice: execute deployment runbook through a guarded deployment runner.",
        tags=("qa-release", "deployment", "stage", context.root_key.lower()),
    )


def _regression_case(index: int, requirements: tuple[QATestRequirement, ...], context: ReleaseContext) -> QATestCase:
    return QATestCase(
        id=f"TC-{index:03d}",
        title="Run release regression suite and audit failures",
        kind="regression",
        source_requirement_ids=tuple(requirement.id for requirement in requirements),
        source_issue_keys=tuple(requirement.source_issue_key for requirement in requirements),
        steps=(
            "Run all functional tests added for this release.",
            "Run the existing ETL regression suite required for the release area.",
            "Audit failed tests and classify each failure as test issue, environment issue, or product defect.",
        ),
        expected_results=(
            "All release tests and required regression tests pass, or defects are filed with evidence.",
            "The release Jira ticket can move to the configured target state only after audit passes.",
        ),
        automation_hint="Later slice: create Jira bug reports for audited product defects and trigger rerun cycle.",
        tags=("qa-release", "regression", context.root_key.lower()),
    )


def _preconditions(context: ReleaseContext) -> list[str]:
    items = [
        f"Jira release ticket is readable: {context.root_key}",
        "STAGE environment credentials and access are available.",
        "ETL test monorepo is checked out and writable.",
    ]
    confluence_links = _links_by_kind(context.links, "confluence")
    if confluence_links:
        items.append(f"Review {len(confluence_links)} Confluence/business requirement link(s).")
    deployment_links = _links_by_kind(context.links, "deployment")
    if deployment_links:
        items.append(f"Review {len(deployment_links)} deployment instruction link(s) before STAGE deploy.")
    if context.repo_hints:
        items.append(f"Resolve and pin {len(context.repo_hints)} implementation/deployment repo ref(s).")
    return items


def _links_by_kind(links: tuple[ReleaseLink, ...], kind: str) -> list[ReleaseLink]:
    return [link for link in links if link.kind == kind]


def _priority_from_issue(issue: LinkedIssue) -> str:
    text = f"{issue.issue_type} {issue.status} {issue.summary}".lower()
    if any(word in text for word in ("blocker", "critical", "highest")):
        return "critical"
    if "high" in text:
        return "high"
    if "low" in text:
        return "low"
    return "medium"


def _release_version(context: ReleaseContext) -> str:
    for repo in context.repo_hints:
        if repo.ref_type == "tag" and repo.ref:
            return repo.ref
    match = _VERSION_RE.search(context.root_summary)
    return match.group(0) if match else ""


def _compact_text(text: str, *, limit: int = 600) -> str:
    compact = " ".join((text or "").split())
    if len(compact) <= limit:
        return compact
    return compact[: limit - 14].rstrip() + " [truncated]"


def _repo_label(repo: str, ref_type: str, ref: str) -> str:
    if ref_type and ref:
        return f"{repo}@{ref_type}:{ref}"
    return repo
