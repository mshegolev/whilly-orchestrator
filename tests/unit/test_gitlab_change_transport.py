"""Hermetic guarded publication contracts: exact source/target identity and CI."""

from __future__ import annotations

import asyncio
import socket
from dataclasses import replace

import httpx
import pytest

from tests.unit.test_product_registry import policy as raw_policy
from whilly.swarm.product_registry import parse_product_policy
from whilly.swarm.publication import Candidate
from whilly.swarm.gitlab_change_transport import (
    GitLabChangeTransport, PinnedGitLabHTTPS, PublicationError, RepoPublicationRequest,
)

SHA = "b" * 40
TARGET = "a" * 40
SECRET = "synthetic-publication-token"


def policy():
    raw = raw_policy(17)
    raw["canonical_remote"] = "https://gitlab.example.com/demo/repo.git"
    return parse_product_policy(raw, project_id="demo", local_path="/tmp/demo", depends_on=())


def request():
    return RepoPublicationRequest(
        change_id="change-demo",
        candidate=Candidate("feature-demo", "demo", "/tmp/demo", "whilly/change-demo", TARGET, SHA,
                            "d" * 64, SHA, True, "approval-demo"),
        target_sha=TARGET, registry_digest="e" * 64, policy_digest="f" * 64, repo_version=4,
    )


class Git:
    def __init__(self, *, ready=True, remote=None):
        self.available = ready
        self.remote = remote or policy().canonical_remote
        self.pushes = []
        self.started = asyncio.Event()
        self.release = None

    async def ready(self):
        return {"ready": self.available, "backend": "test-injected", "reason": None}

    async def inspect(self, change, selected_policy):
        return {"sha": SHA, "branch": change.candidate.branch, "remote": self.remote, "dirty": False}

    async def push(self, change, selected_policy, credential):
        self.started.set()
        if self.release is not None:
            await self.release.wait()
        self.pushes.append((change.candidate.head_sha, selected_policy.canonical_remote))
        return {"exit_code": 0}


class API:
    def __init__(self, *, existing=False, status="success", pipeline_sha=SHA):
        self.existing, self.status, self.pipeline_sha = existing, status, pipeline_sha
        self.calls = []
        self.remote = policy().canonical_remote

    def __call__(self, req):
        self.calls.append((req.method, req.url.path))
        assert req.url.host == "gitlab.example.com"
        assert req.headers["PRIVATE-TOKEN"] == SECRET
        p = req.url.path
        mr = {"iid": 7, "source_branch": "whilly/change-demo", "target_branch": "master",
              "source_project_id": 17, "target_project_id": 17, "state": "opened",
              "sha": SHA, "draft": True, "web_url": "https://gitlab.example.com/demo/repo/-/merge_requests/7"}
        if p == "/api/v4/projects/17":
            body = {"id": 17, "http_url_to_repo": self.remote}
        elif "/repository/branches/" in p:
            body = {"name": "master", "protected": True, "commit": {"id": TARGET}}
        elif p.endswith("/merge_requests"):
            body = [mr] if req.method == "GET" and self.existing else ([] if req.method == "GET" else mr)
        elif p.endswith("/pipelines"):
            body = [] if self.status == "missing" else [{"id": 31, "sha": self.pipeline_sha, "status": self.status}]
        elif p.endswith("/pipelines/31/jobs"):
            body = [{"id": 41, "name": "test", "status": "success"},
                    {"id": 42, "name": "build", "status": "success"}]
        elif p.endswith("/pipelines/31"):
            body = {"id": 31, "sha": self.pipeline_sha, "status": self.status}
        else:
            raise AssertionError(p)
        return httpx.Response(200 if req.method == "GET" else 201, json=body)


def transport(git=None, api=None):
    api = api or API()
    http = PinnedGitLabHTTPS("https://gitlab.example.com", allowed_project_ids=frozenset({17}),
                            credentials=lambda project_id: SECRET, transport=httpx.MockTransport(api))
    return GitLabChangeTransport(git or Git(), http), api


@pytest.fixture(autouse=True)
def block_real_network(monkeypatch):
    def deny(*args, **kwargs):
        raise AssertionError("real endpoint forbidden")
    monkeypatch.setattr(socket.socket, "connect", deny)
    monkeypatch.setattr(socket, "getaddrinfo", deny)


@pytest.mark.parametrize("existing", [True, False])
async def test_new_or_existing_draft_mr_keeps_exact_source_target_and_pipeline(existing):
    adapter, api = transport(api=API(existing=existing))
    receipt = await adapter.prepare_repo_change(request(), policy())
    assert receipt.mr_iid == 7 and receipt.source_sha == SHA and receipt.target_sha == TARGET
    assert receipt.pipeline.pipeline_id == 31 and receipt.pipeline.sha == SHA
    assert receipt.pipeline.status == "success" and receipt.status == "pipeline_green"
    assert sum(method == "POST" and path.endswith("/merge_requests") for method, path in api.calls) == (0 if existing else 1)
    assert not any(method in {"PUT", "DELETE"} for method, _ in api.calls)


@pytest.mark.parametrize("status", ["missing", "failed", "canceled", "running", "pending", "skipped"])
async def test_non_success_pipeline_never_opens_exact_sha_barrier(status):
    adapter, _ = transport(api=API(status=status))
    receipt = await adapter.prepare_repo_change(request(), policy())
    assert receipt.status == "mr_open"
    assert receipt.pipeline.status == status
    assert receipt.pipeline.green is False


async def test_old_sha_pipeline_is_missing_even_when_gitlab_claims_success():
    adapter, _ = transport(api=API(pipeline_sha="c" * 40))
    receipt = await adapter.prepare_repo_change(request(), policy())
    assert receipt.pipeline.status == "missing" and not receipt.pipeline.green


async def test_remote_mismatch_blocks_before_push_or_mr():
    git = Git(remote="https://gitlab.example.com/foreign/repo.git")
    adapter, api = transport(git=git)
    with pytest.raises(PublicationError, match="publication_remote_mismatch"):
        await adapter.prepare_repo_change(request(), policy())
    assert not git.pushes and not any(method == "POST" for method, _ in api.calls)


async def test_unavailable_readiness_does_not_read_credentials_or_contact_endpoint():
    adapter, api = transport(git=Git(ready=False))
    with pytest.raises(PublicationError, match="publication_unavailable"):
        await adapter.prepare_repo_change(request(), policy())
    assert not api.calls


async def test_exact_pipeline_detail_sha_and_required_jobs_are_checked():
    api = API()
    def handler(req):
        response = api(req)
        if req.url.path.endswith("/pipelines/31/jobs"):
            return httpx.Response(200, json=[{"id": 41, "name": "test", "status": "success"}])
        return response
    adapter, _ = transport(api=handler)
    receipt = await adapter.prepare_repo_change(request(), policy())
    assert receipt.pipeline.status == "missing" and not receipt.pipeline.green


async def test_http_boundary_rejects_origin_override_and_redacts_transport_error():
    def unsafe(req):
        raise httpx.ConnectError("leaked " + SECRET, request=req)
    http = PinnedGitLabHTTPS("https://gitlab.example.com", allowed_project_ids=frozenset({17}),
                            credentials=lambda _: SECRET, transport=httpx.MockTransport(unsafe))
    with pytest.raises(PublicationError, match="publication_request_unavailable") as caught:
        await http.request("GET", "/api/v4/projects/17")
    assert SECRET not in str(caught.value)
    for path in ("https://attacker.example/api/v4/projects/17", "/api/v4/projects/99", "/api/v4/projects/17/../../users"):
        with pytest.raises(PublicationError, match="publication_endpoint_blocked"):
            await http.request("GET", path)


async def test_encoded_route_traversal_is_rejected_before_credentials():
    http = PinnedGitLabHTTPS("https://gitlab.example.com", allowed_project_ids=frozenset({17}),
                            credentials=lambda _: pytest.fail("invalid route reached credentials"))
    for suffix in ("%2e%2e%2fusers", "master%3Ftoken=secret", "master%252Fusers"):
        with pytest.raises(PublicationError, match="publication_endpoint_blocked"):
            await http.request("GET", "/api/v4/projects/17/repository/branches/" + suffix)


@pytest.mark.parametrize("foreign", ["pipeline", "job"])
async def test_foreign_pipeline_or_job_commit_never_green(foreign):
    api = API()
    def handler(req):
        response = api(req)
        body = response.json()
        if foreign == "pipeline" and req.url.path.endswith("/pipelines/31"):
            body["project_id"] = 99
        elif foreign == "job" and req.url.path.endswith("/pipelines/31/jobs"):
            body[0]["commit"] = {"id": TARGET}
        return httpx.Response(200, json=body)
    adapter, _ = transport(api=handler)
    receipt = await adapter.prepare_repo_change(request(), policy())
    assert not receipt.pipeline.green

async def test_existing_foreign_mr_refuses_before_pushing():
    git = Git()
    api = API(existing=True)
    def handler(req):
        response = api(req)
        if req.url.path.endswith("/merge_requests"):
            body = response.json()
            body[0]["target_project_id"] = 99
            return httpx.Response(200, json=body)
        return response
    adapter, _ = transport(git=git, api=handler)
    with pytest.raises(PublicationError, match="publication_mr_identity_changed"):
        await adapter.prepare_repo_change(request(), policy())
    assert git.pushes == []


async def test_cancellation_drains_push_receipt_and_never_creates_mr():
    from whilly.swarm.change_set import EvidenceOutcome
    class Store:
        def __init__(self):
            self.receipts = {}
        async def get_effect(self, key):
            return self.receipts.get(key)
        async def record_effect(self, receipt):
            self.receipts[receipt.effect_key] = receipt
            return receipt
    git = Git()
    git.release = asyncio.Event()
    store = Store()
    adapter, api = transport(git=git)
    adapter.effect_store = store
    task = asyncio.create_task(adapter.prepare_repo_change(request(), policy()))
    await git.started.wait()
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    git.release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    receipts = list(store.receipts.values())
    completed = [receipt for receipt in receipts if receipt.operation == "push"]
    assert len(completed) == 1
    assert completed[0].evidence.outcome == EvidenceOutcome.PASSED
    assert completed[0].evidence.sha == SHA
    assert not any(method == "POST" for method, _ in api.calls)


async def test_publication_effect_replay_does_not_repeat_push_or_mr():
    class Store:
        def __init__(self):
            self.receipts = {}
        async def get_effect(self, key):
            return self.receipts.get(key)
        async def record_effect(self, receipt):
            self.receipts.setdefault(receipt.effect_key, receipt)
            return self.receipts[receipt.effect_key]
    git = Git()
    adapter, api = transport(git=git)
    adapter.effect_store = Store()
    first = await adapter.prepare_repo_change(request(), policy())
    second = await adapter.prepare_repo_change(request(), policy())
    assert first == second
    assert len(git.pushes) == 1
    assert sum(method == "POST" for method, _ in api.calls) == 1


def test_publication_phase_is_explicit_and_offline_git_remains_closed(tmp_path):
    from whilly.core.swarm_execution import ExecutionPolicy
    values = dict(read_roots=(str(tmp_path),), write_roots=(str(tmp_path / "scratch"),),
                  denied_roots=(), network=True, timeout_seconds=5, max_output_bytes=4096)
    assert ExecutionPolicy(phase="publication", **values).phase == "publication"
    for phase in ("git", "verify", "host_script"):
        with pytest.raises(ValueError, match="offline phase"):
            ExecutionPolicy(phase=phase, **values)


async def test_guarded_git_fixed_push_injected_credentials_and_redacted_logs(tmp_path):
    from types import SimpleNamespace
    from whilly.swarm.gitlab_change_transport import GuardedPublicationGit
    calls = []
    class Executor:
        def toolchain_for_phase(self, phase):
            assert phase == "publication"
            return "git"
        def roots(self, toolchain):
            return ((), ())
        def ready(self):
            return {"ready": True}
        def environment(self, **kwargs):
            return {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path / "home"), "TMPDIR": str(tmp_path / "tmp")}
        async def run(self, argv, **kwargs):
            calls.append((argv, kwargs))
            stdout = tmp_path / f"stdout-{len(calls)}"
            stderr = tmp_path / f"stderr-{len(calls)}"
            command = tuple(argv[1:])
            value = {("config", "--local", "--no-includes", "--name-only", "--null", "--list"): "core.repositoryformatversion\0",
                     ("remote", "get-url", "origin"): policy().canonical_remote,
                     ("rev-parse", "HEAD"): SHA,
                     ("symbolic-ref", "--short", "HEAD"): request().candidate.branch,
                     ("status", "--porcelain", "--untracked-files=all"): "",
                     ("rev-parse", "--git-path", "hooks"): str(tmp_path / "hooks")}.get(command, "")
            stdout.write_text(value)
            stderr.write_text("synthetic failure " + SECRET if command[0] == "push" else "")
            return SimpleNamespace(exit_code=0, timed_out=False, cancelled=False, reason=None,
                                   stdout_path=str(stdout), stderr_path=str(stderr))
    repo = tmp_path / "repo"
    repo.mkdir()
    (tmp_path / "hooks").mkdir()
    change = replace(request(), candidate=replace(request().candidate, repo_path=str(repo)))
    git = GuardedPublicationGit(Executor(), git_executable="/usr/bin/git", output_root=tmp_path / "receipts",
                                approved_hooks={})
    assert (await git.ready())["ready"] is True
    assert (await git.inspect(change, policy()))["sha"] == SHA
    assert await git.push(change, policy(), SECRET) == {"exit_code": 0}
    argv, kwargs = calls[-1]
    assert argv == ("/usr/bin/git", "push", "--", policy().canonical_remote,
                    f"{SHA}:refs/heads/{change.candidate.branch}")
    assert kwargs["policy"].phase == "publication" and kwargs["policy"].network is True
    assert SECRET not in str(argv)
    assert kwargs["environment"]["GIT_CONFIG_KEY_0"].startswith("http.https://gitlab.example.com/")
    assert "--force" not in argv and "--no-verify" not in argv
    assert SECRET not in (tmp_path / f"stderr-{len(calls)}").read_text()


async def test_guarded_git_default_unprovisioned_is_unavailable(tmp_path):
    from whilly.swarm.execution import GuardedExecutor
    from whilly.swarm.gitlab_change_transport import GuardedPublicationGit
    git = GuardedPublicationGit(GuardedExecutor(), git_executable="/usr/bin/git", output_root=tmp_path,
                               approved_hooks={})
    assert (await git.ready())["ready"] is False


async def test_pipeline_without_required_job_scope_cannot_be_green():
    adapter, _ = transport()
    receipt = await adapter.read_exact_pipeline(17, SHA)
    assert not receipt.green and receipt.status == "missing"


async def test_merge_reads_revalidate_target_and_merge_request_identity():
    base = API(existing=True)

    def handler(req):
        if req.url.path.endswith("/merge_requests/7"):
            return httpx.Response(200, json={
                "iid": 7, "source_branch": "whilly/change-demo", "target_branch": "master",
                "source_project_id": 17, "target_project_id": 17, "state": "opened", "sha": SHA,
                "web_url": "https://gitlab.example.com/demo/repo/-/merge_requests/7",
            })
        return base(req)

    adapter, _ = transport(api=handler)
    receipt = await adapter.prepare_repo_change(request(), policy())
    assert await adapter.read_target_sha(policy()) == TARGET
    assert await adapter.read_merge_request(policy(), receipt) == receipt


async def test_durable_unfinished_intent_blocks_retry_without_repeating_effect():
    from whilly.swarm.change_set import Evidence, EvidenceOutcome, ExternalEffectReceipt
    class Store:
        async def get_effect(self, key):
            if key.endswith(":intent"):
                return ExternalEffectReceipt(key, "change-demo", "demo", "push_intent", "a" * 64,
                    Evidence("push_intent", EvidenceOutcome.UNAVAILABLE, absence="in_flight"))
        async def record_effect(self, receipt):
            pytest.fail("unfinished durable intent must not authorize another effect")
    git = Git()
    adapter, _ = transport(git=git)
    adapter.effect_store = Store()
    with pytest.raises(PublicationError, match="publication_effect_requires_reconciliation"):
        await adapter.prepare_repo_change(request(), policy())
    assert git.pushes == []
