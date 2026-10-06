"""Controlled, transport-neutral primitives for publishing swarm candidates.

The module deliberately contains no GitLab SDK, credentials, subprocesses, or
network calls.  A caller supplies an allowlisted policy and an adapter that
implements :class:`PublicationTransport`.  Adapters must query an existing
open MR before creating one, create only draft MRs, and report CI for the
exact candidate SHA.  Merge and deployment are intentionally outside this
interface.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from typing import Any, Callable, Protocol

__all__ = [
    "Candidate",
    "GitLabTransport",
    "PublicationOutcome",
    "PublicationPolicy",
    "PublicationTransport",
    "Publisher",
]


@dataclass(frozen=True)
class Candidate:
    feature_id: str
    project: str
    repo_path: str
    branch: str
    base_sha: str
    head_sha: str
    approved_digest: str
    verified_sha: str
    review_approved: bool
    approval_identity: str = ""
    dirty: bool = False
    head_verified: bool = True


@dataclass(frozen=True)
class PublicationPolicy:
    remote_url: str
    project_id: int
    target_branch: str
    branch_prefix: str
    ci_safe: bool
    enabled: bool
    approval_identity: str = ""
    protected_target_branches: frozenset[str] = frozenset({"main", "master"})


@dataclass(frozen=True)
class PublicationOutcome:
    mr_url: str | None
    status: str
    blocker: str | None


class PublicationTransport(Protocol):
    """Adapter contract; implementations own credentials and external I/O."""

    def find_open_mr(self, project_id: int, source_branch: str, target_branch: str) -> dict[str, Any] | None: ...

    def create_draft_mr(
        self, project_id: int, source_branch: str, target_branch: str, title: str, description: str
    ) -> dict[str, Any]: ...

    def pipeline_status(self, project_id: int, sha: str) -> str: ...

    def verify_source(self, candidate: Candidate, policy: PublicationPolicy) -> str | None: ...

    def push_source(self, candidate: Candidate, policy: PublicationPolicy) -> None: ...

    def mark_ready(self, project_id: int, mr: dict[str, Any], expected_sha: str) -> None: ...


class GitLabTransport:
    """Concrete GitLab adapter with injected argv runner and HTTP client.

    ``runner`` receives ``(argv, cwd)`` and returns stdout. ``http`` must
    expose ``request(method, path, **kwargs)`` and own credentials. Production
    callers inject those dependencies; tests use fakes, so this module never
    performs an implicit subprocess or network operation. GitLab represents a
    draft through the ``Draft: `` title prefix; see the official documentation:
    https://docs.gitlab.com/user/project/merge_requests/drafts/.
    """

    def __init__(self, *, runner: Callable[[list[str], str], str], http: Any, remote_name: str = "origin") -> None:
        self._runner = runner
        self._http = http
        self._remote_name = remote_name

    def _git(self, repo_path: str, *args: str) -> str:
        return self._runner(["git", *args], repo_path).strip()

    def verify_source(self, candidate: Candidate, policy: PublicationPolicy) -> str | None:
        try:
            self._git(candidate.repo_path, "check-ref-format", "--branch", candidate.branch)
            remote = self._git(candidate.repo_path, "remote", "get-url", self._remote_name)
            dirty = self._git(candidate.repo_path, "status", "--porcelain")
            head = self._git(candidate.repo_path, "rev-parse", "HEAD")
            branch = self._git(candidate.repo_path, "symbolic-ref", "--short", "HEAD")
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            return f"policy_blocked:git_check_failed:{type(exc).__name__}"
        if remote != policy.remote_url:
            return "policy_blocked:remote_mismatch"
        if dirty:
            return "policy_blocked:dirty_head"
        if branch != candidate.branch or not branch.startswith(policy.branch_prefix):
            return "policy_blocked:source_branch"
        if head != candidate.head_sha or head != candidate.verified_sha:
            return "policy_blocked:unverified_head"
        project = self._http.request("GET", f"/api/v4/projects/{policy.project_id}")
        if not isinstance(project, dict) or policy.remote_url not in {
            project.get("http_url_to_repo"),
            project.get("ssh_url_to_repo"),
        }:
            return "policy_blocked:gitlab_project_remote_mismatch"
        return None

    def push_source(self, candidate: Candidate, policy: PublicationPolicy) -> None:
        if not candidate.branch.startswith(policy.branch_prefix) or candidate.branch == policy.target_branch:
            raise ValueError("policy_blocked:source_branch")
        self._runner(
            ["git", "push", policy.remote_url, f"{candidate.head_sha}:refs/heads/{candidate.branch}"],
            candidate.repo_path,
        )

    @staticmethod
    def validate_existing_mr(mr: dict[str, Any], candidate: Candidate, policy: PublicationPolicy) -> str | None:
        if mr.get("source_branch") not in (None, candidate.branch):
            return "policy_blocked:mr_source_mismatch"
        if mr.get("target_branch") not in (None, policy.target_branch):
            return "policy_blocked:mr_target_mismatch"
        if mr.get("target_project_id") not in (None, policy.project_id):
            return "policy_blocked:mr_project_mismatch"
        if mr.get("draft") is False and mr.get("sha") != candidate.head_sha:
            return "mr_sha_mismatch"
        return None

    def find_open_mr(self, project_id: int, source_branch: str, target_branch: str) -> dict[str, Any] | None:
        rows = self._http.request(
            "GET",
            f"/api/v4/projects/{project_id}/merge_requests",
            params={"state": "opened", "source_branch": source_branch, "target_branch": target_branch},
        )
        return rows[0] if rows else None

    def create_draft_mr(
        self, project_id: int, source_branch: str, target_branch: str, title: str, description: str
    ) -> dict[str, Any]:
        return self._http.request(
            "POST",
            f"/api/v4/projects/{project_id}/merge_requests",
            json={
                "source_branch": source_branch,
                "target_branch": target_branch,
                "title": f"Draft: {title}",
                "description": description,
            },
        )

    def pipeline_status(self, project_id: int, sha: str) -> str:
        rows = self._http.request(
            "GET", f"/api/v4/projects/{project_id}/pipelines", params={"sha": sha, "order_by": "id", "sort": "desc"}
        )
        if not rows:
            return "missing"
        exact = [row for row in rows if row.get("sha") == sha]
        if not exact:
            return "missing"
        latest = exact[0].get("status")
        if latest in {"failed", "canceled"}:
            return "failed"
        return "success" if latest == "success" else "missing"

    def mark_ready(self, project_id: int, mr: dict[str, Any], expected_sha: str) -> None:
        iid = mr.get("iid")
        if iid is None:
            raise ValueError("policy_blocked:mr_identity_missing")
        title = str(mr.get("title", ""))
        if title.startswith("Draft: "):
            title = title[7:]
        response = self._http.request(
            "PUT", f"/api/v4/projects/{project_id}/merge_requests/{iid}", json={"title": title}
        )
        if response.get("draft") is not False or response.get("sha") != expected_sha:
            raise ValueError("policy_blocked:mr_ready_sha_mismatch")


class Publisher:
    def __init__(self, transport: PublicationTransport) -> None:
        self._transport = transport

    def publish(self, candidate: Candidate, policy: PublicationPolicy) -> PublicationOutcome:
        blocker = self._policy_blocker(candidate, policy)
        if blocker:
            return PublicationOutcome(None, "blocked", blocker)

        existing = self._transport.find_open_mr(policy.project_id, candidate.branch, policy.target_branch)
        if existing:
            blocker = getattr(self._transport, "validate_existing_mr", lambda *_: None)(existing, candidate, policy)
            if blocker:
                return PublicationOutcome(existing.get("url") or existing.get("web_url"), "blocked", blocker)

        verify_source = getattr(self._transport, "verify_source", None)
        if verify_source is not None:
            blocker = verify_source(candidate, policy)
            if blocker:
                return PublicationOutcome(None, "blocked", blocker)
        push_source = getattr(self._transport, "push_source", None)
        if push_source is not None:
            try:
                push_source(candidate, policy)
            except (OSError, subprocess.SubprocessError, ValueError) as exc:
                return PublicationOutcome(None, "blocked", str(exc))

        mr = existing or self._transport.create_draft_mr(
            policy.project_id,
            candidate.branch,
            policy.target_branch,
            f"{candidate.feature_id}: swarm change",
            f"Draft for swarm feature {candidate.feature_id}; head {candidate.head_sha}.",
        )
        status = self._transport.pipeline_status(policy.project_id, candidate.head_sha)
        url = mr.get("url")
        url = url or mr.get("web_url")
        if mr.get("draft") is False:
            if mr.get("sha") != candidate.head_sha:
                return PublicationOutcome(url, "blocked", "mr_sha_mismatch")
            if status == "success":
                return PublicationOutcome(url, "ready", None)
            if status == "failed":
                return PublicationOutcome(url, "blocked", "pipeline_failed_for_exact_sha")
            return PublicationOutcome(url, "draft", "pipeline_missing_for_exact_sha")
        if status == "success":
            mark_ready = getattr(self._transport, "mark_ready", None)
            if mark_ready is None:
                return PublicationOutcome(url, "draft", "mr_draft_requires_mark_ready")
            try:
                mark_ready(policy.project_id, mr, candidate.head_sha)
            except (OSError, subprocess.SubprocessError, ValueError) as exc:
                return PublicationOutcome(url, "blocked", str(exc))
            return PublicationOutcome(url, "ready", None)
        if status == "failed":
            return PublicationOutcome(url, "blocked", "pipeline_failed_for_exact_sha")
        return PublicationOutcome(url, "draft", "pipeline_missing_for_exact_sha")

    @staticmethod
    def _policy_blocker(candidate: Candidate, policy: PublicationPolicy) -> str | None:
        required = {
            "remote_url": policy.remote_url,
            "project_id": policy.project_id,
            "target_branch": policy.target_branch,
            "branch_prefix": policy.branch_prefix,
        }
        if any(value in (None, "") for value in required.values()) or not policy.ci_safe or not policy.enabled:
            return "publication_unavailable"
        if not candidate.branch.startswith(policy.branch_prefix):
            return "policy_blocked:source_branch"
        if candidate.branch == policy.target_branch:
            return "policy_blocked:source_branch"
        if candidate.dirty:
            return "policy_blocked:dirty_head"
        if not candidate.head_verified or candidate.verified_sha != candidate.head_sha:
            return "policy_blocked:unverified_head"
        if not candidate.review_approved or not candidate.approved_digest:
            return "policy_blocked:approval"
        if not policy.approval_identity or candidate.approval_identity != policy.approval_identity:
            return "policy_blocked:approval_identity"
        return None
