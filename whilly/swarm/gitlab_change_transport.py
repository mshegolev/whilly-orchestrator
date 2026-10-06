"""Explicitly provisioned, origin-pinned GitLab publication; never merge/deploy."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Any
from urllib.parse import quote, unquote, urlsplit

import httpx

from whilly.swarm.publication import Candidate
from whilly.swarm.product_registry import ProductProjectPolicy, _branch
from whilly.swarm.change_set import Evidence, EvidenceOutcome, ExternalEffectReceipt, canonical_digest, thaw
from whilly.core.swarm_execution import ExecutionPolicy


class PublicationError(RuntimeError):
    """A bounded reason only: remote response bodies and credentials stay private."""


@dataclass(frozen=True)
class RepoPublicationRequest:
    change_id: str
    candidate: Candidate
    target_sha: str
    registry_digest: str
    policy_digest: str
    repo_version: int

    def __post_init__(self):
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", self.change_id) or not isinstance(self.candidate, Candidate):
            raise PublicationError("publication_binding_invalid")
        try:
            _branch(self.candidate.branch)
        except ValueError:
            raise PublicationError("publication_candidate_invalid") from None
        for value in (self.target_sha, self.candidate.head_sha, self.candidate.base_sha):
            if not re.fullmatch(r"(?:[a-f0-9]{40}|[a-f0-9]{64})", value):
                raise PublicationError("publication_sha_invalid")
        for value in (self.registry_digest, self.policy_digest, self.candidate.approved_digest):
            if not re.fullmatch(r"[a-f0-9]{64}", value):
                raise PublicationError("publication_binding_invalid")
        if type(self.repo_version) is not int or self.repo_version < 1:
            raise PublicationError("publication_binding_invalid")


@dataclass(frozen=True)
class PipelineReceipt:
    project_id: int
    sha: str
    pipeline_id: int | None
    status: str
    jobs: tuple[tuple[str, str, int], ...] = ()

    @property
    def green(self):
        return self.status == "success" and bool(self.jobs) and all(job[1] == "success" for job in self.jobs)

    def to_dict(self):
        return {"project_id": self.project_id, "sha": self.sha, "pipeline_id": self.pipeline_id,
                "status": self.status, "jobs": [list(job) for job in self.jobs]}


@dataclass(frozen=True)
class RepoPublicationReceipt:
    change_id: str
    repo_id: str
    source_sha: str
    target_sha: str
    source_branch: str
    mr_iid: int
    mr_url: str
    pipeline: PipelineReceipt
    registry_digest: str
    policy_digest: str
    approval_digest: str

    @property
    def status(self):
        return "pipeline_green" if self.pipeline.green else "mr_open"

    def to_dict(self):
        return {name: (value.to_dict() if isinstance(value, PipelineReceipt) else value)
                for name, value in vars(self).items()}


class PinnedGitLabHTTPS:
    """Fixed HTTPS origin/projects/routes; injected credentials, no ambient proxy."""
    def __init__(self, origin: str, *, allowed_project_ids: frozenset[int], credentials: Callable[[int], str],
                 transport: httpx.AsyncBaseTransport | None = None):
        parsed = urlsplit(origin)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
                or parsed.path not in {"", "/"} or parsed.query or parsed.fragment or "%" in parsed.netloc):
            raise PublicationError("publication_origin_invalid")
        if not allowed_project_ids or any(type(item) is not int or item <= 0 for item in allowed_project_ids):
            raise PublicationError("publication_project_scope_required")
        if not callable(credentials):
            raise PublicationError("publication_credentials_required")
        self.origin = origin.rstrip("/")
        self.project_ids = frozenset(allowed_project_ids)
        self._credentials = credentials
        self._transport = transport

    def credential(self, project_id: int) -> str:
        if project_id not in self.project_ids:
            raise PublicationError("publication_endpoint_blocked")
        try:
            token = self._credentials(project_id)
        except Exception:
            raise PublicationError("publication_credentials_unavailable") from None
        if not isinstance(token, str) or not token or len(token) > 8192 or any(ord(c) <= 32 for c in token):
            raise PublicationError("publication_credentials_unavailable")
        return token

    async def request(self, method, path, **kwargs):
        match = re.fullmatch(
            r"/api/v4/projects/([1-9][0-9]*)(?:/repository/branches/[a-zA-Z0-9._%/-]+"
            r"|/merge_requests(?:/[1-9][0-9]*)?|/pipelines(?:/[1-9][0-9]*(?:/jobs)?)?)?", path,
        )
        if (not match or ".." in path or "\\\\" in path or method not in {"GET", "POST"}
                or int(match[1]) not in self.project_ids or set(kwargs) - {"params", "json"}
                or (method == "POST" and not path.endswith("/merge_requests"))):
            raise PublicationError("publication_endpoint_blocked")
        if "/repository/branches/" in path:
            branch = path.split("/repository/branches/", 1)[1]
            try:
                _branch(unquote(branch))
                if "%" in unquote(branch) or quote(unquote(branch), safe="") != branch:
                    raise ValueError()
            except ValueError:
                raise PublicationError("publication_endpoint_blocked") from None
        token = self.credential(int(match[1]))
        try:
            async with httpx.AsyncClient(base_url=self.origin, transport=self._transport,
                                        trust_env=False, follow_redirects=False, timeout=30.0) as client:
                response = await client.request(method, path, headers={"PRIVATE-TOKEN": token}, **kwargs)
                response.raise_for_status()
                if len(response.content) > 1024 * 1024:
                    raise PublicationError("publication_response_limit")
                return response.json()
        except (httpx.HTTPError, ValueError):
            raise PublicationError("publication_request_unavailable") from None


class GuardedPublicationGit:
    """Only fixed, guarded Git operations; auth and hook digests are host injected.

    A publication toolchain and a concrete isolation probe are mandatory. Repo
    config cannot select helpers, hooks, proxies, includes or URL rewrites.
    Normal pre-push hooks remain enabled, but must match host-approved digests.
    """

    def __init__(self, executor, *, git_executable: str, output_root: Path, approved_hooks: dict[str, str]):
        executable = Path(git_executable)
        if not executable.is_absolute() or not executable.is_file():
            raise PublicationError("publication_git_toolchain_invalid")
        self.executor, self.executable = executor, str(executable)
        self.output_root = Path(output_root).resolve()
        self.approved_hooks = dict(approved_hooks)

    async def ready(self):
        try:
            self.executor.toolchain_for_phase("publication")
            result = self.executor.ready()
        except Exception:
            return {"ready": False, "reason": "publication_unavailable"}
        return result if result.get("ready") is True else {"ready": False, "reason": "publication_unavailable"}

    async def _run(self, change, arguments, *, remote=None, token=None):
        if (await self.ready()).get("ready") is not True:
            raise PublicationError("publication_unavailable")
        root = Path(change.candidate.repo_path).resolve(strict=True)
        attempt = self.output_root / uuid.uuid4().hex
        attempt.mkdir(parents=True)
        toolchain = self.executor.toolchain_for_phase("publication")
        reads, denied = self.executor.roots(toolchain)
        environment = self.executor.environment(toolchain_id=toolchain, phase="publication", attempt_root=attempt)
        environment.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null",
                            "GIT_TERMINAL_PROMPT": "0", "GIT_ALLOW_PROTOCOL": "https",
                            "GIT_NO_LAZY_FETCH": "1", "GIT_OPTIONAL_LOCKS": "0"})
        secret_values = ()
        if token is not None:
            encoded = base64.b64encode(f"oauth2:{token}".encode()).decode()
            header = f"Authorization: Basic {encoded}"
            origin = f"https://{urlsplit(remote).netloc}/"
            environment.update({"GIT_CONFIG_COUNT": "3", "GIT_CONFIG_KEY_0": f"http.{origin}.extraHeader",
                                "GIT_CONFIG_VALUE_0": header, "GIT_CONFIG_KEY_1": "http.followRedirects",
                                "GIT_CONFIG_VALUE_1": "false", "GIT_CONFIG_KEY_2": "credential.helper",
                                "GIT_CONFIG_VALUE_2": ""})
            secret_values = (token, encoded, header)
        policy = ExecutionPolicy("publication", (str(root), str(attempt), "/dev/null", *(str(p) for p in reads)),
                                 (str(attempt),), tuple(str(p) for p in denied), remote is not None, 120, 1024 * 1024)
        try:
            result = await self.executor.run((self.executable, *arguments), phase="publication", cwd=root,
                                             policy=policy, environment=environment, log_dir=attempt / "logs")
        except Exception:
            raise PublicationError("publication_git_unavailable") from None
        # GuardedExecutor owns output descriptors; redact explicit Git auth too.
        if secret_values:
            from whilly.swarm.execution import GuardedExecutor
            for path in (result.stdout_path, result.stderr_path):
                GuardedExecutor._redact(Path(path), secret_values)
        if result.exit_code != 0 or result.timed_out or result.cancelled or result.reason:
            raise PublicationError("publication_git_failed")
        return Path(result.stdout_path).read_text().strip()

    async def inspect(self, change, policy):
        names = await self._run(change, ("config", "--local", "--no-includes", "--name-only", "--null", "--list"))
        safe = {"core.repositoryformatversion", "core.filemode", "core.bare", "core.logallrefupdates",
                "core.ignorecase", "core.precomposeunicode", "user.name", "user.email", "remote.origin.url",
                "remote.origin.fetch"}
        if any(name.lower() not in safe for name in names.split("\0") if name):
            raise PublicationError("publication_git_config_untrusted")
        hooks = Path(await self._run(change, ("rev-parse", "--git-path", "hooks")))
        if not hooks.is_absolute():
            hooks = Path(change.candidate.repo_path) / hooks
        if hooks.is_symlink() or not hooks.is_dir():
            raise PublicationError("publication_git_hooks_untrusted")
        for hook in hooks.iterdir():
            if hook.name.endswith(".sample"):
                continue
            if (hook.is_symlink() or not hook.is_file()
                    or self.approved_hooks.get(str(hook.resolve())) != hashlib.sha256(hook.read_bytes()).hexdigest()):
                raise PublicationError("publication_git_hooks_untrusted")
        remote = await self._run(change, ("remote", "get-url", "origin"))
        sha = await self._run(change, ("rev-parse", "HEAD"))
        branch = await self._run(change, ("symbolic-ref", "--short", "HEAD"))
        dirty = bool(await self._run(change, ("status", "--porcelain", "--untracked-files=all")))
        await self._run(change, ("merge-base", "--is-ancestor", change.candidate.base_sha, change.candidate.head_sha))
        return {"remote": remote, "sha": sha, "branch": branch, "dirty": dirty}

    async def push(self, change, policy, token):
        observed = await self.inspect(change, policy)
        if observed != {"remote": policy.canonical_remote, "sha": change.candidate.head_sha,
                        "branch": change.candidate.branch, "dirty": False}:
            raise PublicationError("publication_candidate_changed")
        await self._run(change, ("push", "--", policy.canonical_remote,
                                f"{change.candidate.head_sha}:refs/heads/{change.candidate.branch}"),
                        remote=policy.canonical_remote, token=token)
        return {"exit_code": 0}


class GitLabChangeTransport:
    def __init__(self, git: Any, http: PinnedGitLabHTTPS, *, effect_store=None, before_effect=None):
        self.git, self.http = git, http
        self.effect_store, self.before_effect = effect_store, before_effect

    async def _effect(self, change, policy, operation, execute):
        key = f"{change.change_id}:{change.candidate.project}:{change.candidate.head_sha}:{operation}"
        digest = canonical_digest({"request": vars(change) | {"candidate": vars(change.candidate)},
                                   "policy": policy.to_dict(), "operation": operation})
        if self.effect_store is not None:
            previous = await self.effect_store.get_effect(key)
            if previous is not None:
                if previous.request_digest != digest or previous.evidence.outcome != EvidenceOutcome.PASSED:
                    raise PublicationError("publication_effect_requires_reconciliation")
                return thaw(previous.evidence.details)
        if self.before_effect is not None:
            await self.before_effect(change, policy)
        await self._remote_binding(change, policy)

        async def perform():
            if self.effect_store is not None:
                intent_key = key + ":intent"
                if await self.effect_store.get_effect(intent_key) is not None:
                    raise PublicationError("publication_effect_requires_reconciliation")
                owner = uuid.uuid4().hex
                intent = ExternalEffectReceipt(intent_key, change.change_id, change.candidate.project,
                    operation + "_intent", digest,
                    Evidence(operation + "_intent", EvidenceOutcome.UNAVAILABLE,
                             absence="publication_effect_in_flight", details={"owner": owner}))
                recorded = await self.effect_store.record_effect(intent)
                if recorded.evidence.details.get("owner") != owner:
                    raise PublicationError("publication_effect_requires_reconciliation")
            try:
                result = await execute()
                evidence = Evidence(operation, EvidenceOutcome.PASSED, sha=change.candidate.head_sha,
                    command=("git", "push", policy.canonical_remote,
                             f"{change.candidate.head_sha}:refs/heads/{change.candidate.branch}") if operation == "push" else (),
                    exit_code=0 if operation == "push" else None,
                    job_id=str(result["iid"]) if operation == "mr_open" else None, details=result)
            except Exception:
                if self.effect_store is not None:
                    await self.effect_store.record_effect(ExternalEffectReceipt(key, change.change_id,
                        change.candidate.project, operation, digest,
                        Evidence(operation, EvidenceOutcome.UNAVAILABLE, sha=change.candidate.head_sha,
                                 absence="publication_effect_requires_reconciliation")))
                raise PublicationError("publication_effect_requires_reconciliation") from None
            if self.effect_store is not None:
                await self.effect_store.record_effect(ExternalEffectReceipt(key, change.change_id,
                    change.candidate.project, operation, digest, evidence))
            return result

        task = asyncio.create_task(perform())
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            # Do not return to a retrying coordinator while a side effect or receipt is still in flight.
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    continue
                except Exception:
                    break
            if not task.cancelled():
                task.exception()
            raise

    async def _remote_binding(self, change, policy):
        observed = await self.git.inspect(change, policy)
        if observed.get("remote") != policy.canonical_remote:
            raise PublicationError("publication_remote_mismatch")
        if (observed.get("sha") != change.candidate.head_sha
                or observed.get("branch") != change.candidate.branch or observed.get("dirty") is not False):
            raise PublicationError("publication_candidate_changed")
        project = await self.http.request("GET", f"/api/v4/projects/{policy.gitlab_project_id}")
        if (not isinstance(project, dict) or project.get("id") != policy.gitlab_project_id
                or project.get("http_url_to_repo") != policy.canonical_remote):
            raise PublicationError("publication_remote_mismatch")
        target = await self.http.request("GET", f"/api/v4/projects/{policy.gitlab_project_id}/repository/branches/"
                                        + quote(policy.target_branch, safe=""))
        if (not isinstance(target, dict) or target.get("protected") is not True
                or target.get("name") != policy.target_branch or not isinstance(target.get("commit"), dict)
                or target["commit"].get("id") != change.target_sha):
            raise PublicationError("publication_target_changed")

    async def ready(self):
        try:
            result = await self.git.ready()
        except Exception:
            return {"ready": False, "reason": "publication_unavailable"}
        return result if isinstance(result, dict) and result.get("ready") is True else {
            "ready": False, "reason": "publication_unavailable"
        }

    async def _rows(self, path, params=None):
        rows = []
        for page in range(1, 11):
            batch = await self.http.request("GET", path, params={**(params or {}), "page": page, "per_page": 100})
            if not isinstance(batch, list) or any(not isinstance(row, dict) for row in batch):
                raise PublicationError("publication_response_invalid")
            rows.extend(batch)
            if len(batch) < 100:
                return rows
        raise PublicationError("publication_response_limit")

    async def read_exact_pipeline(self, project_id: int, sha: str, *, required_jobs=()) -> PipelineReceipt:
        if not re.fullmatch(r"(?:[a-f0-9]{40}|[a-f0-9]{64})", sha):
            raise PublicationError("publication_sha_invalid")
        rows = await self._rows(f"/api/v4/projects/{project_id}/pipelines",
                                {"sha": sha, "order_by": "id", "sort": "desc"})
        exact = [row for row in rows if row.get("sha") == sha and type(row.get("id")) is int and row["id"] > 0]
        if not exact:
            return PipelineReceipt(project_id, sha, None, "missing")
        pipeline = max(exact, key=lambda row: row["id"])
        pid = pipeline["id"]
        detail = await self.http.request("GET", f"/api/v4/projects/{project_id}/pipelines/{pid}")
        if (not isinstance(detail, dict) or detail.get("id") != pid or detail.get("sha") != sha
                or detail.get("project_id", project_id) != project_id):
            return PipelineReceipt(project_id, sha, pid, "missing")
        status = detail.get("status")
        if status not in {"success", "failed", "canceled", "running", "pending", "skipped", "created", "manual",
                          "preparing", "scheduled", "waiting_for_resource"}:
            raise PublicationError("publication_response_invalid")
        jobs = ()
        if status == "success" and not required_jobs:
            status = "missing"
        if status == "success":
            rows = await self._rows(f"/api/v4/projects/{project_id}/pipelines/{pid}/jobs", {"include_retried": False})
            values = []
            for name in required_jobs:
                matching = [row for row in rows if row.get("name") == name and row.get("retried") is not True]
                if len(matching) != 1 or type(matching[0].get("id")) is not int or matching[0]["id"] <= 0:
                    status = "missing"
                    break
                row = matching[0]
                if ("commit" in row and (not isinstance(row["commit"], dict) or row["commit"].get("id") != sha)):
                    status = "missing"
                    break
                state = row.get("status")
                if state not in {"success", "failed", "canceled", "running", "pending", "skipped", "manual", "created"}:
                    raise PublicationError("publication_response_invalid")
                values.append((name, state, row["id"]))
                if row.get("status") != "success":
                    status = "failed" if row.get("status") in {"failed", "canceled"} else "missing"
                    break
            jobs = tuple(values)
        return PipelineReceipt(project_id, sha, pid, status, jobs)

    async def prepare_repo_change(self, change: RepoPublicationRequest,
                                  policy: ProductProjectPolicy) -> RepoPublicationReceipt:
        if (await self.ready()).get("ready") is not True:
            raise PublicationError("publication_unavailable")
        candidate = change.candidate
        if (candidate.project != policy.project_id or not candidate.branch.startswith(policy.branch_prefix)
                or candidate.branch == policy.target_branch or candidate.dirty or not candidate.head_verified
                or candidate.verified_sha != candidate.head_sha or not candidate.review_approved):
            raise PublicationError("publication_candidate_invalid")
        parsed = urlsplit(policy.canonical_remote)
        if parsed.scheme != "https" or f"https://{parsed.netloc}" != self.http.origin:
            raise PublicationError("publication_remote_mismatch")
        await self._remote_binding(change, policy)
        rows = await self._rows(f"/api/v4/projects/{policy.gitlab_project_id}/merge_requests",
                               {"state": "opened", "source_branch": candidate.branch, "target_branch": policy.target_branch})
        if len(rows) > 1:
            raise PublicationError("publication_mr_ambiguous")
        if rows:
            self._validate_mr(rows[0], change, policy)
        async def push():
            result = await self.git.push(change, policy, self.http.credential(policy.gitlab_project_id))
            if not isinstance(result, dict) or result.get("exit_code") != 0:
                raise PublicationError("publication_push_failed")
            return {"exit_code": 0, "source_sha": candidate.head_sha, "target_sha": change.target_sha}
        await self._effect(change, policy, "push", push)
        async def open_mr():
            mr = rows[0] if rows else await self.http.request("POST",
                f"/api/v4/projects/{policy.gitlab_project_id}/merge_requests",
                json={"source_branch": candidate.branch, "target_branch": policy.target_branch,
                      "title": f"Draft: {change.change_id}", "description": f"Verified source {candidate.head_sha}"})
            self._validate_mr(mr, change, policy)
            return {name: mr[name] for name in ("iid", "web_url", "source_branch", "target_branch",
                                               "source_project_id", "target_project_id", "sha", "state")}
        mr = await self._effect(change, policy, "mr_open", open_mr)
        self._validate_mr(mr, change, policy)
        url = mr["web_url"]
        pipeline = await self.read_exact_pipeline(policy.gitlab_project_id, candidate.head_sha,
                                                   required_jobs=policy.checks["ci"])
        return RepoPublicationReceipt(change.change_id, candidate.project, candidate.head_sha, change.target_sha,
                                      candidate.branch, mr["iid"], url, pipeline, change.registry_digest,
                                      change.policy_digest, candidate.approved_digest)

    def _validate_mr(self, mr, change, policy):
        candidate = change.candidate
        if (not isinstance(mr, dict) or type(mr.get("iid")) is not int or mr["iid"] <= 0
                or mr.get("source_branch") != candidate.branch or mr.get("target_branch") != policy.target_branch
                or mr.get("source_project_id") != policy.gitlab_project_id
                or mr.get("target_project_id") != policy.gitlab_project_id or mr.get("sha") != candidate.head_sha
                or mr.get("state") != "opened"):
            raise PublicationError("publication_mr_identity_changed")
        url = mr.get("web_url")
        expected = self.http.origin + urlsplit(policy.canonical_remote).path.removesuffix(".git") + f"/-/merge_requests/{mr['iid']}"
        if url != expected:
            raise PublicationError("publication_mr_url_invalid")
