"""Build publication candidates from durable accepted evidence, not browser claims."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import subprocess
from urllib.parse import urlsplit

import httpx

from whilly.swarm import gitops
from whilly.swarm.publication import Candidate, GitLabTransport, PublicationPolicy, Publisher
from whilly.swarm.registry import load_registry
from whilly.swarm.product_workflow import WorkflowBlocked, require_feature_permission
from whilly.swarm.change_set import ChangeSetStatus, Evidence, EvidenceOutcome, RepoChangeStatus
from whilly.swarm.gitlab_change_transport import GitLabChangeTransport, PublicationError, RepoPublicationRequest


class ProductPublicationBackend:
    """Bind the injected transport to current durable approval and registry state."""

    def __init__(self, snapshot, store, transport: GitLabChangeTransport, *, feature_requests=None):
        self.snapshot, self.store, self.transport = snapshot, store, transport
        self.feature_requests = feature_requests
        if transport.effect_store is not None and transport.effect_store is not store:
            raise WorkflowBlocked("publication_effect_store_mismatch")
        transport.effect_store = store
        transport.before_effect = self._recheck

    async def ready(self):
        return await self.transport.ready()

    async def _recheck(self, request, policy):
        try:
            self.snapshot.assert_unchanged()
        except ValueError:
            raise WorkflowBlocked("publication_registry_changed") from None
        current = await self.store.get(request.change_id)
        if (
            current is None
            or current.status not in {ChangeSetStatus.EXECUTING, ChangeSetStatus.VERIFYING_REPOS}
            or current.registry_digest != request.registry_digest
            or self.snapshot.registry_digest != request.registry_digest
            or self.snapshot.policy_digest != request.policy_digest
            or current.approval_digest != request.candidate.approved_digest
            or self.snapshot.projects.get(request.candidate.project) != policy
        ):
            raise WorkflowBlocked("publication_approval_binding_changed")
        repo = next((value for value in current.repo_changes if value.repo_id == request.candidate.project), None)
        offsets = {RepoChangeStatus.LOCAL_VERIFIED: 0, RepoChangeStatus.MR_OPEN: 1, RepoChangeStatus.PIPELINE_GREEN: 2}
        if (
            repo is None
            or repo.status not in offsets
            or repo.version != request.repo_version + offsets[repo.status]
            or repo.base_sha != request.candidate.base_sha
            or request.target_sha != repo.base_sha
            or repo.last_evidence is None
            or repo.last_evidence.outcome != EvidenceOutcome.PASSED
            or repo.last_evidence.sha != request.candidate.head_sha
        ):
            raise WorkflowBlocked("publication_repo_binding_changed")
        return repo

    async def prepare_repo_change(self, request: RepoPublicationRequest):
        if (await self.ready()).get("ready") is not True:
            raise WorkflowBlocked("publication_unavailable")
        policy = self.snapshot.projects.get(request.candidate.project)
        if policy is None:
            raise WorkflowBlocked("publication_repo_binding_changed")
        await self._recheck(request, policy)
        try:
            receipt = await self.transport.prepare_repo_change(request, policy)
        except PublicationError as exc:
            raise WorkflowBlocked(str(exc)) from None
        repo = await self._recheck(request, policy)
        details = receipt.to_dict()
        if repo.status == RepoChangeStatus.LOCAL_VERIFIED:
            evidence = Evidence(
                "mr_open",
                EvidenceOutcome.PASSED,
                sha=receipt.source_sha,
                job_id=f"mr:{receipt.mr_iid}",
                details=details,
            )
            repo = await self.store.transition_repo(
                request.change_id, request.candidate.project, repo.version, RepoChangeStatus.MR_OPEN, evidence
            )
        if repo.status == RepoChangeStatus.MR_OPEN and receipt.pipeline.green:
            evidence = Evidence(
                "exact_sha_pipeline",
                EvidenceOutcome.PASSED,
                sha=receipt.source_sha,
                job_id=f"pipeline:{receipt.pipeline.pipeline_id}",
                details=details,
            )
            await self.store.transition_repo(
                request.change_id, request.candidate.project, repo.version, RepoChangeStatus.PIPELINE_GREEN, evidence
            )
        return receipt


class GitLabHTTP:
    def __init__(self, base_url: str, token: str):
        parsed = urlsplit(base_url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise WorkflowBlocked("publication_policy_invalid: HTTPS GitLab URL required")
        self.client = httpx.Client(
            base_url=base_url.rstrip("/"), headers={"PRIVATE-TOKEN": token}, timeout=30, follow_redirects=False
        )

    def request(self, method, path, **kwargs):
        response = self.client.request(method, path, **kwargs)
        if not response.is_success:
            raise WorkflowBlocked(f"publication_http_failed: {response.status_code}")
        return response.json()

    def close(self):
        self.client.close()


def run_git(argv: list[str], cwd: str) -> str:
    result = subprocess.run(
        argv, cwd=cwd, capture_output=True, text=True, timeout=120, env={**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    )
    if result.returncode:
        # Git output can contain credential-bearing remote URLs; never persist it.
        raise ValueError(f"publication_git_failed: exit {result.returncode}")
    return result.stdout


async def publish_feature(workflow, feature_id: str, *, transport_factory=None) -> dict:
    feature = await workflow.require(feature_id)
    backend = getattr(workflow, "publication_backend", None)
    if backend is None or (await backend.ready()).get("ready") is not True:
        raise WorkflowBlocked("publication_unavailable: guarded publication backend is not provisioned")
    if backend.feature_requests is None:
        raise WorkflowBlocked("publication_change_set_binding_required")
    requests = await backend.feature_requests(feature_id)
    if not requests or any(
        not isinstance(request, RepoPublicationRequest) or request.candidate.feature_id != feature_id
        for request in requests
    ):
        raise WorkflowBlocked("publication_change_set_binding_required")
    receipts = [await backend.prepare_repo_change(request) for request in requests]
    return {"status": "published_candidates", "receipts": [receipt.to_dict() for receipt in receipts]}
    await require_feature_permission(
        workflow.pool,
        feature["session_id"],
        action="publish",
        revision=feature["plan_revision"],
        expected_digest=feature["approved_digest"],
    )
    registry = load_registry(workflow.registry_path)
    policies = registry.raw.get("publication", {})
    if not isinstance(policies, dict) or not policies:
        raise WorkflowBlocked("publication_unavailable: no authorized destinations configured")
    from whilly.swarm.runtime import SwarmService

    service = SwarmService(workflow.pool)
    tasks = [
        t for t in await service.store.session_tasks(feature["session_id"]) if t["revision"] == feature["plan_revision"]
    ]
    if not tasks or any(t["status"] != "DONE" for t in tasks):
        raise WorkflowBlocked("publication_tasks_incomplete")
    attempts = await service.store.attempts_for_session(feature["session_id"])
    candidates = []
    for project_id in sorted({t["project_id"] for t in tasks}):
        policy_data = policies.get(project_id)
        if not isinstance(policy_data, dict):
            raise WorkflowBlocked(f"publication_unavailable: {project_id}")
        required = ("remote_url", "project_id", "target_branch", "branch_prefix", "gitlab_url", "token_env")
        if (
            any(not policy_data.get(k) for k in required)
            or policy_data.get("enabled") is not True
            or policy_data.get("ci_safe") is not True
        ):
            raise WorkflowBlocked(f"publication_policy_blocked: {project_id}")
        project_tasks = [t for t in tasks if t["project_id"] == project_id]
        accepted = []
        for task in project_tasks:
            evidence = [a for a in attempts if a["task_id"] == task["task_id"] and a["status"] == "accepted"]
            if not evidence:
                raise WorkflowBlocked("publication_evidence_missing")
            item = evidence[-1]
            if (
                not item.get("verification")
                or not all(e.get("passed") is True for e in item["verification"])
                or item.get("review", {}).get("verdict") != "approve"
            ):
                raise WorkflowBlocked("publication_evidence_invalid")
            accepted.append(item)
        # Only publish an integrated head containing every accepted project result.
        tip = None
        for possible in accepted:
            try:
                for other in accepted:
                    await asyncio.to_thread(
                        gitops.git,
                        possible["worktree_path"],
                        "merge-base",
                        "--is-ancestor",
                        other["head_sha"],
                        possible["head_sha"],
                    )
            except gitops.GitError:
                continue
            tip = possible
            break
        if tip is None:
            raise WorkflowBlocked(f"project_integration_required: {project_id}")
        branch = f"{policy_data['branch_prefix']}{feature_id}/{project_id}"
        if not branch.startswith("swarm/"):
            raise WorkflowBlocked("publication_policy_blocked: branch_prefix must start swarm/")
        token = os.environ.get(policy_data["token_env"])
        if not transport_factory and not token:
            raise WorkflowBlocked(f"publication_credentials_missing: {project_id}")
        policy = PublicationPolicy(
            **{
                k: policy_data[k]
                for k in ("remote_url", "project_id", "target_branch", "branch_prefix", "ci_safe", "enabled")
            },
            approval_identity=feature["approved_digest"],
        )
        candidates.append((project_id, tip, branch, policy, policy_data, token))

    outcomes = []
    for project_id, tip, branch, policy, data, token in candidates:
        await require_feature_permission(
            workflow.pool,
            feature["session_id"],
            action="publish",
            revision=feature["plan_revision"],
            expected_digest=feature["approved_digest"],
        )
        # A retained isolated publication worktree gives one stable branch per feature/project.
        path = registry.resolved_state_dir() / "publications" / feature_id / project_id
        if not path.exists():
            await asyncio.to_thread(
                gitops.create_worktree, registry.projects[project_id].path, path, branch, tip["head_sha"]
            )
        elif await asyncio.to_thread(gitops.head_sha, path) != tip["head_sha"]:
            raise WorkflowBlocked("publication_branch_changed: operator reconciliation required")
        candidate = Candidate(
            feature_id=feature_id,
            project=project_id,
            repo_path=str(path),
            branch=branch,
            base_sha=feature["base_shas"][project_id],
            head_sha=tip["head_sha"],
            verified_sha=tip["head_sha"],
            approved_digest=feature["approved_digest"],
            approval_identity=feature["approved_digest"],
            review_approved=True,
        )
        http = None
        cancelled = False
        try:
            if transport_factory:
                transport = transport_factory(project_id)
            else:
                http = GitLabHTTP(data["gitlab_url"], token)
                transport = GitLabTransport(runner=run_git, http=http)
            pending = asyncio.create_task(asyncio.to_thread(Publisher(transport).publish, candidate, policy))
            try:
                result = await asyncio.shield(pending)
            except asyncio.CancelledError:
                # An in-flight push cannot be undone. Drain it and retain its
                # receipt before propagating stop; never start the next project.
                result = await pending
                cancelled = True
        finally:
            if http:
                http.close()
        receipt = {
            **dataclasses.asdict(result),
            "project": project_id,
            "sha": tip["head_sha"],
            "branch": branch,
            "approved_digest": feature["approved_digest"],
            "plan_revision": feature["plan_revision"],
        }
        outcomes.append(receipt)
        async with workflow.pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO swarm_publications(feature_id,project_id,receipt) VALUES($1,$2,$3::jsonb) ON CONFLICT(feature_id,project_id) DO UPDATE SET receipt=EXCLUDED.receipt,updated_at=NOW()",
                feature_id,
                project_id,
                json.dumps(receipt),
            )
        if cancelled:
            raise asyncio.CancelledError
    await require_feature_permission(
        workflow.pool,
        feature["session_id"],
        action="publish",
        revision=feature["plan_revision"],
        expected_digest=feature["approved_digest"],
    )
    state = "mr_ready" if all(r["status"] == "ready" for r in outcomes) else "review"
    await workflow.products.finish(feature_id, state, expected_digest=feature["approved_digest"])
    return {"feature": await workflow.require(feature_id), "publications": outcomes}
