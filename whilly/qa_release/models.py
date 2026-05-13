"""Value objects for QA release-verification intake."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any


UTC = timezone.utc


@dataclass(frozen=True)
class ReleaseLink:
    """External URL discovered from Jira issue fields or remote links."""

    url: str
    title: str = ""
    kind: str = "other"
    source_issue_key: str = ""
    relationship: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class GitRepoHint:
    """Repository/ref hint parsed from GitLab or GitHub URLs."""

    provider: str
    repo_full_name: str
    url: str
    clone_url: str = ""
    ref: str = ""
    ref_type: str = ""
    source_issue_key: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class LinkedIssue:
    """One Jira issue in the release verification scope."""

    key: str
    summary: str = ""
    url: str = ""
    status: str = ""
    issue_type: str = ""
    relation: str = ""
    description: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ReleaseContext:
    """Structured intake context for a QA ETL release verification run."""

    root_key: str
    root_summary: str
    root_url: str
    linked_issues: tuple[LinkedIssue, ...]
    links: tuple[ReleaseLink, ...]
    repo_hints: tuple[GitRepoHint, ...]
    warnings: tuple[str, ...] = ()
    generated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "root_key": self.root_key,
            "root_summary": self.root_summary,
            "root_url": self.root_url,
            "linked_issues": [issue.to_dict() for issue in self.linked_issues],
            "links": [link.to_dict() for link in self.links],
            "repo_hints": [hint.to_dict() for hint in self.repo_hints],
            "warnings": list(self.warnings),
            "generated_at": self.generated_at or datetime.now(UTC).isoformat(),
        }


@dataclass(frozen=True)
class QATestRequirement:
    """Requirement selected for QA verification from Jira/Confluence context."""

    id: str
    source_issue_key: str
    title: str
    source_url: str = ""
    source_type: str = "jira_issue"
    text: str = ""
    priority: str = "medium"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class QATestCase:
    """Planned automated or manual verification case for the release."""

    id: str
    title: str
    kind: str
    source_requirement_ids: tuple[str, ...] = ()
    source_issue_keys: tuple[str, ...] = ()
    steps: tuple[str, ...] = ()
    expected_results: tuple[str, ...] = ()
    automation_hint: str = ""
    target_repo: str = "test_monorepo"
    tags: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        out = asdict(self)
        out["source_requirement_ids"] = list(self.source_requirement_ids)
        out["source_issue_keys"] = list(self.source_issue_keys)
        out["steps"] = list(self.steps)
        out["expected_results"] = list(self.expected_results)
        out["tags"] = list(self.tags)
        return out


@dataclass(frozen=True)
class QATestPlan:
    """Actionable QA verification plan derived from a release context."""

    release_key: str
    release_summary: str
    release_url: str
    release_version: str = ""
    requirements: tuple[QATestRequirement, ...] = ()
    test_cases: tuple[QATestCase, ...] = ()
    repo_hints: tuple[GitRepoHint, ...] = ()
    artifact_links: tuple[ReleaseLink, ...] = ()
    preconditions: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    generated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "release_key": self.release_key,
            "release_summary": self.release_summary,
            "release_url": self.release_url,
            "release_version": self.release_version,
            "requirements": [requirement.to_dict() for requirement in self.requirements],
            "test_cases": [test_case.to_dict() for test_case in self.test_cases],
            "repo_hints": [hint.to_dict() for hint in self.repo_hints],
            "artifact_links": [link.to_dict() for link in self.artifact_links],
            "preconditions": list(self.preconditions),
            "warnings": list(self.warnings),
            "generated_at": self.generated_at or datetime.now(UTC).isoformat(),
        }


def release_context_from_dict(data: dict[str, Any]) -> ReleaseContext:
    """Decode ``ReleaseContext.to_dict()`` output."""

    root_key = str(data.get("root_key") or "")
    if not root_key:
        raise ValueError("release context JSON is missing non-empty 'root_key'")
    return ReleaseContext(
        root_key=root_key,
        root_summary=str(data.get("root_summary") or ""),
        root_url=str(data.get("root_url") or ""),
        linked_issues=tuple(_linked_issue_from_dict(item) for item in data.get("linked_issues") or []),
        links=tuple(_release_link_from_dict(item) for item in data.get("links") or []),
        repo_hints=tuple(_git_repo_hint_from_dict(item) for item in data.get("repo_hints") or []),
        warnings=tuple(str(item) for item in data.get("warnings") or []),
        generated_at=str(data.get("generated_at") or ""),
    )


def qa_test_plan_from_dict(data: dict[str, Any]) -> QATestPlan:
    """Decode ``QATestPlan.to_dict()`` output."""

    release_key = str(data.get("release_key") or "")
    if not release_key:
        raise ValueError("QA test-plan JSON is missing non-empty 'release_key'")
    return QATestPlan(
        release_key=release_key,
        release_summary=str(data.get("release_summary") or ""),
        release_url=str(data.get("release_url") or ""),
        release_version=str(data.get("release_version") or ""),
        requirements=tuple(_qa_requirement_from_dict(item) for item in data.get("requirements") or []),
        test_cases=tuple(_qa_test_case_from_dict(item) for item in data.get("test_cases") or []),
        repo_hints=tuple(_git_repo_hint_from_dict(item) for item in data.get("repo_hints") or []),
        artifact_links=tuple(_release_link_from_dict(item) for item in data.get("artifact_links") or []),
        preconditions=tuple(str(item) for item in data.get("preconditions") or []),
        warnings=tuple(str(item) for item in data.get("warnings") or []),
        generated_at=str(data.get("generated_at") or ""),
    )


def _release_link_from_dict(data: Any) -> ReleaseLink:
    item = data if isinstance(data, dict) else {}
    return ReleaseLink(
        url=str(item.get("url") or ""),
        title=str(item.get("title") or ""),
        kind=str(item.get("kind") or "other"),
        source_issue_key=str(item.get("source_issue_key") or ""),
        relationship=str(item.get("relationship") or ""),
    )


def _git_repo_hint_from_dict(data: Any) -> GitRepoHint:
    item = data if isinstance(data, dict) else {}
    return GitRepoHint(
        provider=str(item.get("provider") or ""),
        repo_full_name=str(item.get("repo_full_name") or ""),
        url=str(item.get("url") or ""),
        clone_url=str(item.get("clone_url") or ""),
        ref=str(item.get("ref") or ""),
        ref_type=str(item.get("ref_type") or ""),
        source_issue_key=str(item.get("source_issue_key") or ""),
    )


def _linked_issue_from_dict(data: Any) -> LinkedIssue:
    item = data if isinstance(data, dict) else {}
    return LinkedIssue(
        key=str(item.get("key") or ""),
        summary=str(item.get("summary") or ""),
        url=str(item.get("url") or ""),
        status=str(item.get("status") or ""),
        issue_type=str(item.get("issue_type") or ""),
        relation=str(item.get("relation") or ""),
        description=str(item.get("description") or ""),
    )


def _qa_requirement_from_dict(data: Any) -> QATestRequirement:
    item = data if isinstance(data, dict) else {}
    return QATestRequirement(
        id=str(item.get("id") or ""),
        source_issue_key=str(item.get("source_issue_key") or ""),
        title=str(item.get("title") or ""),
        source_url=str(item.get("source_url") or ""),
        source_type=str(item.get("source_type") or "jira_issue"),
        text=str(item.get("text") or ""),
        priority=str(item.get("priority") or "medium"),
    )


def _qa_test_case_from_dict(data: Any) -> QATestCase:
    item = data if isinstance(data, dict) else {}
    return QATestCase(
        id=str(item.get("id") or ""),
        title=str(item.get("title") or ""),
        kind=str(item.get("kind") or ""),
        source_requirement_ids=tuple(str(value) for value in item.get("source_requirement_ids") or []),
        source_issue_keys=tuple(str(value) for value in item.get("source_issue_keys") or []),
        steps=tuple(str(value) for value in item.get("steps") or []),
        expected_results=tuple(str(value) for value in item.get("expected_results") or []),
        automation_hint=str(item.get("automation_hint") or ""),
        target_repo=str(item.get("target_repo") or "test_monorepo"),
        tags=tuple(str(value) for value in item.get("tags") or []),
    )
