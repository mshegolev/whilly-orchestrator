"""Real DB, Git worktrees and fake models for product approval gates."""

# ruff: noqa: F811
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from tests.integration.test_swarm_runtime import db_pool, swarm_dsn  # noqa: F401
from tests.swarm_helpers import make_repo, write_registry, git
from whilly.swarm.product_workflow import ProductWorkflow, WorkflowBlocked, require_feature_permission
from whilly.swarm.runtime import SwarmService

pytestmark = pytest.mark.integration


@pytest.fixture
def product_registry(tmp_path):
    repo = make_repo(tmp_path, "demo")
    python = str(Path(sys.executable).resolve())
    verification_policy = {
        "test": [
            [
                python,
                "-c",
                'print(\'<testsuite tests="1" failures="0" errors="0" skipped="0"><testcase /></testsuite>\')',
            ]
        ],
        "lint": [[python, "-c", "print('[]')"]],
        "architecture": [[python, "-c", 'print(\'{\\"evaluated_rules\\": 1, \\"violations\\": []}\')']],
        "protected_paths": ["AGENTS.md", "pyproject.toml"],
        "toolchain_id": "test",
    }
    path = write_registry(
        tmp_path / "registry.json",
        {
            "demo": {
                "path": str(repo),
                "purpose": "Demo",
                "verification": [["test", "-f", "README.md"]],
                "verification_policy": verification_policy,
            }
        },
        state_dir=tmp_path / "state",
        limits={"heartbeat_seconds": 1, "lease_seconds": 3, "agent_timeout_seconds": 30},
    )
    data = json.loads(path.read_text())
    data["profiles"] = {
        name: {
            "engine": "claude",
            "model": model,
            "reasoning_effort": "low",
            "max_turns": 5,
            "timeout_seconds": 30,
            "budget_usd": 1.0,
        }
        for name, model in [
            ("planner-strong", "test-strong"),
            ("planner-escalation", "test-strong"),
            ("worker-cheap", "test-cheap"),
            ("reviewer-cheap", "test-review"),
        ]
    }
    root = tmp_path / "skills"
    (root / "demo-spec").mkdir(parents=True)
    (root / "demo-spec" / "SKILL.md").write_text("Specify goal, capabilities and acceptance criteria.")
    data["bmad"] = {"skill_root": str(root), "workflow_skills": ["demo-spec"]}
    data["execution"] = {
        "toolchains": {
            "test": {
                "read_roots": [
                    str(tmp_path),
                    str(Path(__file__).resolve().parents[2]),
                    str(Path(sys.executable).resolve().parent),
                    str(Path(sys.executable).resolve().parents[4]),
                    "/opt/homebrew/opt/gettext/lib",
                    "/Applications/Xcode.app/Contents/Developer",
                    "/usr",
                    "/bin",
                ],
                "path": os.environ.get("PATH", "/usr/bin:/bin"),
                "home": str(tmp_path / "execution-home"),
                "temporary": str(tmp_path / "execution-tmp"),
                "auth": {},
            }
        },
        "phases": {
            "planner": "test",
            "escalation": "test",
            "worker": "test",
            "review": "test",
            "verify": "test",
            "git": "test",
            "host_script": "test",
        },
    }
    path.write_text(json.dumps(data))
    return path, repo


async def planned(workflow, registry, tmp_path, monkeypatch, *, description="Write output"):
    feature = await workflow.products.create_feature("Feature", "Add measured behavior")
    plan_file = tmp_path / "plan.json"
    plan_file.write_text(
        json.dumps(
            {
                "summary": "Demo",
                "tasks": [
                    {
                        "id": "one",
                        "project": "demo",
                        "role": "implementer",
                        "description": description,
                        "acceptance": ["output committed"],
                        "verification": [["test", "-f", "one.txt"]],
                    }
                ],
            }
        )
    )
    monkeypatch.setenv("FAKE_PLAN_FILE", str(plan_file))
    feature = await workflow.plan(feature["id"], "Plan it")
    return feature


async def test_plan_approve_execute_fake_models(db_pool, product_registry, tmp_path, monkeypatch):
    path, _ = product_registry
    workflow = ProductWorkflow(db_pool, str(path))
    feature = await planned(workflow, path, tmp_path, monkeypatch)
    assert feature["status"] == "planned"
    assert feature["approval_digest"] and not feature["approved_digest"]
    with pytest.raises(WorkflowBlocked, match="approval_required"):
        await SwarmService(db_pool).apply_revision(feature["session_id"], feature["plan_revision"])
    feature = await workflow.products.approve(
        feature["id"], revision=feature["revision"], digest=feature["approval_digest"]
    )
    result = await workflow.execute(feature["id"], 2)
    assert result["status"] == "review"
    assert "publication_unavailable" in result["blocker"]
    status = await SwarmService(db_pool).status(feature["session_id"])
    assert status["tasks"][0]["status"] == "DONE", status["tasks"][0]
    assert status["tasks"][0]["attempts"] == 1


async def test_product_stop_cancels_active_fake_run_and_drains(db_pool, product_registry, tmp_path, monkeypatch):
    import asyncio

    from whilly.api.product_workflow import ExecuteRequest, build_product_workflow_router
    from whilly.swarm.agent import process_group_alive

    path, _ = product_registry
    workflow = ProductWorkflow(db_pool, str(path))
    flag = tmp_path / "fake-worker-sleep.flag"
    flag.write_text("keep worker active until product stop")
    monkeypatch.setenv("FAKE_SLEEP_FLAG", str(flag))
    feature = await planned(
        workflow,
        path,
        tmp_path,
        monkeypatch,
        description=f"BEHAVIOR:sleep {flag}",
    )
    feature = await workflow.products.approve(
        feature["id"], revision=feature["revision"], digest=feature["approval_digest"]
    )

    router = build_product_workflow_router(db_pool, b"integration-test-secret", str(path))
    run_endpoint = next(route.endpoint for route in router.routes if route.path.endswith("/run"))
    stop_endpoint = next(route.endpoint for route in router.routes if route.path.endswith("/stop"))
    queued = await run_endpoint(feature["id"], ExecuteRequest(workers=1), _principal={})
    assert queued["status"] == "queued"

    service = SwarmService(db_pool)
    attempt = None
    for _ in range(200):
        running = await service.store.running_attempts(feature["session_id"])
        if running and running[0].get("agent_pgid"):
            attempt = running[0]
            break
        await asyncio.sleep(0.05)
    assert attempt is not None, "fake worker process did not start"

    stopped = await stop_endpoint(feature["id"], _principal={})

    assert stopped["status"] == "stop_requested"
    assert stopped["drained"] is True
    assert stopped["feature"]["status"] == "blocked"
    assert stopped["feature"]["approved_digest"] is None
    assert not process_group_alive(attempt["agent_pgid"])
    attempts = await service.store.attempts_for_session(feature["session_id"])
    assert attempts[0]["status"] == "cancelled"


async def test_two_failed_cheap_attempts_escalate_and_require_reapproval(
    db_pool, product_registry, tmp_path, monkeypatch
):
    path, _ = product_registry
    workflow = ProductWorkflow(db_pool, str(path))
    feature = await planned(
        workflow,
        path,
        tmp_path,
        monkeypatch,
        description="BEHAVIOR:nonzero",
    )
    escalation_plan = tmp_path / "escalation-plan.json"
    escalation_plan.write_text(
        json.dumps(
            {
                "summary": "Replanned after two failed cheap attempts",
                "tasks": [
                    {
                        "id": "one",
                        "project": "demo",
                        "role": "implementer",
                        "description": "Write output after escalation",
                        "acceptance": ["output committed"],
                        "verification": [["test", "-f", "one.txt"]],
                    }
                ],
            }
        )
    )
    monkeypatch.setenv("FAKE_ESCALATION_PLAN_FILE", str(escalation_plan))
    feature = await workflow.products.approve(
        feature["id"], revision=feature["revision"], digest=feature["approval_digest"]
    )

    replanned = await workflow.execute(feature["id"], 1)
    assert replanned["status"] == "planned"
    assert replanned["approval_digest"] and not replanned["approved_digest"]
    assert replanned["plan_revision"] != feature["plan_revision"]

    replanned = await workflow.products.approve(
        replanned["id"], revision=replanned["revision"], digest=replanned["approval_digest"]
    )
    result = await workflow.execute(replanned["id"], 1)
    assert result["status"] == "review"
    assert "publication_unavailable" in result["blocker"]
    status = await SwarmService(db_pool).status(result["session_id"])
    current = [task for task in status["tasks"] if task["revision"] == status["session"]["applied_revision"]]
    assert len(current) == 1 and current[0]["status"] == "DONE", current


async def test_changed_base_cannot_execute(db_pool, product_registry, tmp_path, monkeypatch):
    path, repo = product_registry
    workflow = ProductWorkflow(db_pool, str(path))
    feature = await planned(workflow, path, tmp_path, monkeypatch)
    await workflow.products.approve(feature["id"], revision=feature["revision"], digest=feature["approval_digest"])
    git(repo, "commit", "--allow-empty", "-qm", "changed base")
    with pytest.raises(WorkflowBlocked, match="feature_base_changed"):
        await require_feature_permission(
            db_pool, feature["session_id"], action="apply", revision=feature["plan_revision"]
        )


async def test_feature_call_cap_includes_planning(db_pool, product_registry):
    path, _ = product_registry
    workflow = ProductWorkflow(db_pool, str(path))
    feature = await workflow.products.create_feature("Feature", "Intent")
    await workflow.products.set_budget(feature["id"], {"max_calls": 1, "max_elapsed_seconds": 100})
    store = SwarmService(db_pool).store
    await store.reserve_agent_call(feature["session_id"], engine="claude", mode="planner", prompt_chars=1, max_calls=90)
    with pytest.raises(Exception, match="agent_call_budget_exhausted"):
        await store.reserve_agent_call(
            feature["session_id"], engine="claude", mode="planner", prompt_chars=1, max_calls=90
        )


async def test_two_repository_delivery_reuses_mrs(db_pool, product_registry, tmp_path, monkeypatch):
    from whilly.swarm.product_publication import publish_feature, run_git
    from whilly.swarm.publication import GitLabTransport

    path, repo = product_registry
    second = make_repo(tmp_path, "other")
    data = json.loads(path.read_text())
    data["projects"]["other"] = {
        "path": str(second),
        "purpose": "Consumer",
        "verification": [["test", "-f", "README.md"]],
        "verification_policy": data["projects"]["demo"]["verification_policy"],
    }
    data["roles"]["implementer"]["projects"].append("other")
    data["publication"] = {}
    for index, (name, local) in enumerate([("demo", repo), ("other", second)], start=1):
        remote = tmp_path / (name + "-remote.git")
        git(tmp_path, "clone", "--bare", str(local), str(remote))
        git(local, "remote", "add", "origin", str(remote))
        data["publication"][name] = {
            "remote_url": str(remote),
            "project_id": index,
            "target_branch": "main",
            "branch_prefix": "swarm/",
            "ci_safe": True,
            "enabled": True,
            "gitlab_url": "https://gitlab.example.com",
            "token_env": "DEMO_TEST_TOKEN_NOT_CONFIGURED",
        }
    path.write_text(json.dumps(data))
    plan_file = tmp_path / "two-repo-plan.json"
    plan_file.write_text(
        json.dumps(
            {
                "summary": "Producer and consumer",
                "tasks": [
                    {
                        "id": "produce",
                        "project": "demo",
                        "role": "implementer",
                        "description": "Write contract",
                        "acceptance": ["contract"],
                        "verification": [["test", "-f", "produce.txt"]],
                    },
                    {
                        "id": "consume",
                        "project": "other",
                        "role": "implementer",
                        "description": "Consume contract",
                        "depends_on": ["produce"],
                        "acceptance": ["consumer"],
                        "verification": [["test", "-f", "consume.txt"]],
                    },
                ],
            }
        )
    )
    monkeypatch.setenv("FAKE_PLAN_FILE", str(plan_file))
    monkeypatch.delenv("DEMO_TEST_TOKEN_NOT_CONFIGURED", raising=False)
    workflow = ProductWorkflow(db_pool, str(path))
    feature = await workflow.products.create_feature("Two repositories", "Producer consumer integration")
    feature = await workflow.plan(feature["id"], "Create plan")
    await workflow.products.approve(feature["id"], revision=feature["revision"], digest=feature["approval_digest"])
    feature = await workflow.execute(feature["id"], 2)
    assert feature["status"] == "review"
    with pytest.raises(WorkflowBlocked, match="publication_unavailable"):
        await publish_feature(workflow, feature["id"])
    return

    class FakeGitLab:
        def __init__(self, project):
            self.project = project
            self.mr = None
            self.created = 0

        def request(self, method, path, **kwargs):
            if method == "GET" and path.rsplit("/", 1)[-1].isdigit():
                return {"http_url_to_repo": data["publication"][self.project]["remote_url"]}
            if method == "GET" and path.endswith("/pipelines"):
                return [{"sha": kwargs["params"]["sha"], "status": "success", "id": 1}]
            if method == "GET":
                return [self.mr] if self.mr else []
            if method == "POST":
                self.created += 1
                payload = kwargs["json"]
                remote = data["publication"][self.project]["remote_url"]
                sha = git(tmp_path, "--git-dir", remote, "rev-parse", payload["source_branch"]).strip()
                self.mr = {
                    "iid": 1,
                    "sha": sha,
                    "draft": True,
                    "title": payload["title"],
                    "web_url": f"https://gitlab.example.com/{self.project}/-/merge_requests/1",
                }
            if method == "PUT":
                self.mr = {**self.mr, **kwargs["json"], "draft": False}
            return self.mr

    http = {name: FakeGitLab(name) for name in ("demo", "other")}

    def factory(name):
        return GitLabTransport(runner=run_git, http=http[name])

    first = await publish_feature(workflow, feature["id"], transport_factory=factory)
    assert first["feature"]["status"] == "mr_ready"
    second_result = await publish_feature(workflow, feature["id"], transport_factory=factory)
    assert second_result["feature"]["status"] == "mr_ready"
    assert len(second_result["publications"]) == 2
    assert all(client.created == 1 for client in http.values())
    assert await db_pool.fetchval("SELECT COUNT(*) FROM swarm_publications WHERE feature_id=$1", feature["id"]) == 2


async def test_host_bmad_artifacts_bind_to_feature(db_pool, product_registry, tmp_path, monkeypatch):
    path, _ = product_registry
    data = json.loads(path.read_text())
    root = tmp_path / "bmad-project"
    scripts = root / "_bmad" / "scripts"
    scripts.mkdir(parents=True)
    for name in ("resolve_config.py", "resolve_customization.py", "memlog.py"):
        (scripts / name).write_text('print("{}")\n')
    skill = tmp_path / "skills" / "bmad-spec"
    skill.mkdir()
    (skill / "SKILL.md").write_text("Use the host to run _bmad/scripts/memlog.py and derive SPEC.md.")
    data["bmad"] = {
        "mode": "host-spec",
        "project_root": str(root),
        "skill_root": str(tmp_path / "skills"),
        "workflow_skills": ["bmad-spec"],
    }
    path.write_text(json.dumps(data))
    kernel = "\n".join(
        "## " + name + "\nProtocol fixture"
        for name in ("Why", "Capabilities", "Constraints", "Non-goals", "Success signal")
    )
    artifact = tmp_path / "artifact.json"
    artifact.write_text(
        json.dumps(
            {
                "SPEC.md": kernel,
                "companions": {"contract.md": "Versioned contract"},
                "decisions": ["Keep scope bounded."],
                "coherence": "pass",
                "preservation": "pass",
            }
        )
    )
    monkeypatch.setenv("FAKE_BMAD_ARTIFACT_FILE", str(artifact))
    workflow = ProductWorkflow(db_pool, str(path))
    feature = await planned(workflow, path, tmp_path, monkeypatch)
    assert feature["status"] == "planned"
    assert feature["spec"]["document"] == kernel
    assert feature["spec"]["artifacts"]["contract.md"] == "Versioned contract"
    binding = await db_pool.fetchval("SELECT binding FROM swarm_product_specs WHERE feature_id=$1", feature["id"])
    if isinstance(binding, str):
        binding = json.loads(binding)
    assert binding["spec"]["artifacts"]["contract.md"] == "Versioned contract"
