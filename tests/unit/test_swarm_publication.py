from __future__ import annotations

import pytest

from whilly.swarm.product_workflow import WorkflowBlocked
from whilly.swarm.product_publication import publish_feature
from whilly.swarm.publication import Candidate, GitLabTransport, Publisher, PublicationPolicy


def candidate(**overrides):
    values = {
        "feature_id": "feature-one",
        "project": "demo",
        "repo_path": "/tmp/demo",
        "branch": "swarm/feature-one",
        "base_sha": "a" * 40,
        "head_sha": "b" * 40,
        "approved_digest": "digest",
        "verified_sha": "b" * 40,
        "review_approved": True,
        "approval_identity": "plan-approval-1",
    }
    values.update(overrides)
    return Candidate(**values)


def policy(**overrides):
    values = {
        "remote_url": "https://example.invalid/demo.git",
        "project_id": 17,
        "target_branch": "main",
        "branch_prefix": "swarm/",
        "ci_safe": True,
        "enabled": True,
        "approval_identity": "plan-approval-1",
    }
    values.update(overrides)
    return PublicationPolicy(**values)


class Transport:
    def __init__(self, *, existing=None, pipeline="success"):
        self.existing = existing
        self.pipeline = pipeline
        self.calls = []

    def find_open_mr(self, project_id, source_branch, target_branch):
        self.calls.append(("find", project_id, source_branch, target_branch))
        return self.existing

    def create_draft_mr(self, project_id, source_branch, target_branch, title, description):
        self.calls.append(("create", project_id, source_branch, target_branch, title, description))
        return {"url": "https://example.invalid/mr/1", "iid": 1}

    def pipeline_status(self, project_id, sha):
        self.calls.append(("pipeline", project_id, sha))
        return self.pipeline

    def verify_source(self, candidate, policy):
        return None

    def push_source(self, candidate, policy):
        self.calls.append(("push",))

    def mark_ready(self, project_id, mr, expected_sha):
        self.calls.append(("ready", project_id, mr, expected_sha))


def test_creates_one_draft_and_keeps_it_draft_until_exact_sha_pipeline_succeeds():
    transport = Transport(pipeline="missing")
    outcome = Publisher(transport).publish(candidate(), policy())
    assert outcome.status == "draft"
    assert outcome.mr_url == "https://example.invalid/mr/1"
    assert outcome.blocker == "pipeline_missing_for_exact_sha"
    assert [call[0] for call in transport.calls] == ["find", "push", "create", "pipeline"]


def test_existing_mr_is_reused_and_successful_exact_sha_is_publish_ready_but_not_merged():
    transport = Transport(existing={"url": "https://example.invalid/mr/9", "iid": 9, "draft": True}, pipeline="success")
    outcome = Publisher(transport).publish(candidate(), policy())
    assert outcome == outcome.__class__(outcome.mr_url, "ready", None)
    assert [call[0] for call in transport.calls] == ["find", "push", "pipeline", "ready"]


def test_existing_ready_mr_with_exact_sha_is_reused_without_marking_ready_again():
    transport = Transport(
        existing={"url": "https://example.invalid/mr/9", "iid": 9, "draft": False, "sha": "b" * 40},
        pipeline="success",
    )
    outcome = Publisher(transport).publish(candidate(), policy())
    assert outcome.status == "ready"
    assert [call[0] for call in transport.calls] == ["find", "push", "pipeline"]


def test_existing_ready_mr_with_different_sha_is_blocked():
    transport = Transport(
        existing={"url": "https://example.invalid/mr/9", "iid": 9, "draft": False, "sha": "c" * 40},
        pipeline="success",
    )
    outcome = Publisher(transport).publish(candidate(), policy())
    assert outcome.status == "blocked"
    assert outcome.blocker == "mr_sha_mismatch"


@pytest.mark.asyncio
async def test_product_publication_fails_closed_before_legacy_transport_or_git() -> None:
    calls = []

    class Workflow:
        pool = object()

        async def require(self, feature_id):
            return {"id": feature_id}

    def transport_factory(_project):
        calls.append("transport")
        raise AssertionError("legacy transport must not be constructed")

    with pytest.raises(WorkflowBlocked, match="publication_unavailable"):
        await publish_feature(Workflow(), "feature", transport_factory=transport_factory)
    assert calls == []


def test_missing_policy_fields_are_named_and_transport_is_not_called():
    transport = Transport()
    outcome = Publisher(transport).publish(candidate(), policy(remote_url=""))
    assert outcome.status == "blocked"
    assert outcome.blocker == "publication_unavailable"
    assert transport.calls == []


def test_rejects_remote_branch_and_unverified_candidate_without_transport_call():
    transport = Transport()
    outcome = Publisher(transport).publish(candidate(branch="feature-one"), policy())
    assert outcome.blocker == "policy_blocked:source_branch"
    assert transport.calls == []


class GitRunner:
    def __init__(self, *, dirty="", head="b" * 40):
        self.dirty = dirty
        self.head = head
        self.calls = []

    def __call__(self, argv, cwd):
        self.calls.append((argv, cwd))
        if argv[1:3] == ["check-ref-format", "--branch"]:
            return argv[3] + "\n"
        if argv[1:] == ["remote", "get-url", "origin"]:
            return "https://example.invalid/demo.git\n"
        if argv[1:] == ["status", "--porcelain"]:
            return self.dirty
        if argv[1:] == ["rev-parse", "HEAD"]:
            return self.head + "\n"
        if argv[1:] == ["symbolic-ref", "--short", "HEAD"]:
            return "swarm/feature-one\n"
        if argv[1] == "push":
            return "pushed\n"
        raise AssertionError(argv)


class Http:
    def __init__(self, pipelines=None, mrs=None):
        self.pipelines = pipelines if pipelines is not None else []
        self.mrs = mrs
        self.calls = []

    def request(self, method, path, **kwargs):
        self.calls.append((method, path, kwargs))
        if method == "GET" and path.rsplit("/", 1)[-1].isdigit():
            return {"http_url_to_repo": "https://example.invalid/demo.git"}
        if method == "GET" and "pipelines" in path:
            return self.pipelines
        if method == "GET":
            return [] if self.mrs is None else self.mrs
        if method == "POST":
            return {"web_url": "https://example.invalid/demo/-/merge_requests/1", "iid": 1, "draft": True}
        if method == "PUT":
            return {"draft": False, "sha": "b" * 40}
        raise AssertionError((method, path))


def test_gitlab_transport_verifies_pushes_safe_source_and_marks_ready_only_for_draft_mr_exact_sha():
    runner = GitRunner()
    http = Http(pipelines=[{"sha": "b" * 40, "status": "success"}])
    transport = GitLabTransport(runner=runner, http=http)
    outcome = Publisher(transport).publish(candidate(), policy())
    assert outcome.status == "ready"
    assert any(call[0][1] == "push" for call in runner.calls)
    push = next(call[0] for call in runner.calls if call[0][1] == "push")
    assert push == ["git", "push", "https://example.invalid/demo.git", "b" * 40 + ":refs/heads/swarm/feature-one"]
    assert "--force" not in push and "main" not in push
    create = next(call for call in http.calls if call[0] == "POST")
    assert create[2]["json"]["title"].startswith("Draft:")


def test_gitlab_transport_blocks_dirty_or_remote_mismatch_before_push():
    runner = GitRunner(dirty=" M file.py")
    transport = GitLabTransport(runner=runner, http=Http())
    outcome = Publisher(transport).publish(candidate(), policy())
    assert outcome.blocker == "policy_blocked:dirty_head"
    assert not any(call[0][1] == "push" for call in runner.calls)


def test_pipeline_status_uses_latest_exact_sha_not_an_older_success():
    http = Http(
        pipelines=[
            {"sha": "b" * 40, "status": "running"},
            {"sha": "b" * 40, "status": "success"},
        ]
    )
    status = GitLabTransport(runner=GitRunner(), http=http).pipeline_status(17, "b" * 40)
    assert status == "missing"


def test_gitlab_project_id_must_identify_the_allowlisted_remote():
    class WrongProject(Http):
        def request(self, method, path, **kwargs):
            if method == "GET" and path.rsplit("/", 1)[-1].isdigit():
                return {"http_url_to_repo": "https://example.invalid/unrelated.git"}
            return super().request(method, path, **kwargs)

    runner = GitRunner()
    outcome = Publisher(GitLabTransport(runner=runner, http=WrongProject())).publish(candidate(), policy())
    assert outcome.blocker == "policy_blocked:gitlab_project_remote_mismatch"
    assert not any(call[0][1] == "push" for call in runner.calls)


def test_publisher_queries_mr_before_immutable_push():
    transport = Transport(pipeline="missing")
    Publisher(transport).publish(candidate(), policy())
    assert [call[0] for call in transport.calls][:2] == ["find", "push"]


def test_gitlab_transport_rejects_invalid_branch_before_any_push():
    runner = GitRunner()
    transport = GitLabTransport(runner=runner, http=Http())
    outcome = Publisher(transport).publish(candidate(branch="-bad"), policy())
    assert outcome.blocker == "policy_blocked:source_branch"
    assert not any(call[0][1] == "push" for call in runner.calls)
