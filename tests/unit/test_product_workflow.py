"""Approval gates, planner configuration and worker transport must fail closed."""

import sys
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.swarm_helpers import make_repo, write_registry
from whilly.swarm.learning.domain import ContextPackage, KnowledgeRevision, SourceCheck
from whilly.swarm.learning_binding import bound_worker_prompt, make_binding
from whilly.core.swarm_execution import ExecutionPolicy
from whilly.swarm.execution import GuardedExecutor
from whilly.swarm.registry import EngineConfig, Project, Registry, load_registry
from whilly.swarm.product_workflow import (
    WorkflowBlocked,
    apply_or_resume_revision,
    planner_registry,
    validate_budget,
)


@pytest.mark.parametrize(
    "budget",
    [
        {},
        {"max_calls": True, "max_elapsed_seconds": 1},
        {"max_calls": 0, "max_elapsed_seconds": 20},
        {"max_calls": 4, "max_elapsed_seconds": float("inf")},
    ],
)
def test_invalid_budget(budget):
    with pytest.raises(WorkflowBlocked):
        validate_budget(budget)


def test_budget_explicit():
    assert validate_budget({"max_calls": 60, "max_elapsed_seconds": 7200})["max_calls"] == 60


def test_no_planner_fallback(tmp_path):
    repo = make_repo(tmp_path, "demo")
    registry = load_registry(
        write_registry(tmp_path / "registry.json", {"demo": {"path": str(repo), "purpose": "Demo"}})
    )
    with pytest.raises(Exception, match="profile"):
        planner_registry(registry, "planner-strong")


def test_worker_profile_cannot_plan(tmp_path):
    repo = make_repo(tmp_path, "demo")
    registry = load_registry(
        write_registry(tmp_path / "registry.json", {"demo": {"path": str(repo), "purpose": "Demo"}})
    )
    with pytest.raises(WorkflowBlocked, match="planner_profile_required"):
        planner_registry(registry, "worker-cheap")


@pytest.mark.asyncio
async def test_plain_execution_permission_rechecks_host_binding(monkeypatch):
    from whilly.swarm import product_workflow

    calls = []

    async def recheck(pool, session_id, revision):
        calls.append((pool, session_id, revision))

    class Connection:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def fetchrow(self, query, *args):
            return None

        async def fetchval(self, query, *args):
            return None

    class Pool:
        def acquire(self):
            return Connection()

    monkeypatch.setattr(product_workflow, "require_plain_execution_binding", recheck)
    pool = Pool()
    await product_workflow.require_feature_permission(pool, "plain", action="accept", revision=7)
    assert calls == [(pool, "plain", 7)]


def test_bmad_artifact_fence_does_not_hide_following_plan():
    from whilly.swarm.plan import extract_plan_json

    reply = '```bmad-artifacts\n{"SPEC.md":"kernel"}\n```\n```json\n{"summary":"plan","tasks":[]}\n```'
    assert extract_plan_json(reply) == {"summary": "plan", "tasks": []}


@pytest.mark.asyncio
async def test_product_execution_resumes_already_applied_revision() -> None:
    calls = []

    class Store:
        async def get_revision(self, session_id, revision):
            return {"status": "applied"}

    class Service:
        store = Store()

        async def require_session(self, session_id):
            return {"applied_revision": 7}

        async def apply_revision(self, session_id, revision):
            calls.append((session_id, revision))

    await apply_or_resume_revision(Service(), "session", 7)

    assert calls == []


@pytest.mark.skipif(sys.platform != "darwin", reason="requires macOS sandbox-exec")
@pytest.mark.asyncio
async def test_both_engines_receive_same_approved_binding_and_expiry_is_checked_on_launch(tmp_path, monkeypatch):
    """Exercise both real transport adapters with local children, never a paid CLI."""
    from whilly.swarm import learning_binding, product_workflow
    from whilly.swarm.runtime import Coordinator

    child = tmp_path / "fake_engine.py"
    child.write_text(
        "#!/bin/sh\n"
        'cat > "$CAPTURE"\n'
        'if [ "$ENGINE" = claude ]; then\n'
        '  echo \'{"type":"result","result":"done","usage":{}}\'\n'
        "else\n"
        '  echo \'{"type":"item.completed","item":{"type":"agent_message","text":"done"}}\'\n'
        '  echo \'{"type":"turn.completed","usage":{}}\'\n'
        "fi\n",
        encoding="utf-8",
    )
    child.chmod(0o755)

    now = datetime.now(timezone.utc)

    def revision(revision_id: str, project_id: str) -> KnowledgeRevision:
        return KnowledgeRevision(
            id=revision_id,
            product_id="default",
            project_id=project_id,
            kind="fact",
            body=f"approved-{revision_id}",
            source_uri="git:README.md",
            source_sha="a" * 40,
            evidence_hash=f"e-{revision_id}",
            observed_at=now,
            verified_at=now,
            expires_at=now + timedelta(hours=1),
            classification="internal",
            status="verified",
            author_id="author",
            verifier_id="verifier",
            policy_version="1",
        )

    items = (revision("api", "api"), revision("dependency", "dependency"), revision("other", "other"))
    current_items = list(items)

    class Store:
        async def visible(self, *args):
            return list(current_items)

    class Verifier:
        async def check(self, item):
            return SourceCheck("verified", item.source_sha)

    monkeypatch.setattr(learning_binding, "PostgresMemoryStore", lambda pool: Store())
    monkeypatch.setattr(learning_binding, "GitSourceVerifier", lambda projects: Verifier())
    registry = Registry(
        "demo",
        "demo",
        {
            "api": Project("api", str(tmp_path), "main", "API", depends_on=("dependency",)),
            "dependency": Project("dependency", str(tmp_path), "main", "Dependency"),
            "other": Project("other", str(tmp_path), "main", "Other"),
        },
        {},
    )
    binding = make_binding(ContextPackage(items, (), (), tuple(item.id for item in items)))
    prompt = await bound_worker_prompt(None, registry, "default", binding, "api")
    assert "approved-api" in prompt and "approved-dependency" in prompt
    assert "approved-other" not in prompt

    class Connection:
        def transaction(self):
            return self

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def execute(self, *args):
            return "OK"

        async def fetchval(self, query, *args):
            if "COUNT" in query:
                return 0
            return 1

    class Pool:
        def acquire(self):
            return Connection()

    gate_calls = []

    async def launch_gate(pool, session_id, *, action, revision):
        gate_calls.append((action, revision))
        await learning_binding.validate_binding(None, registry, "default", binding)

    monkeypatch.setattr(product_workflow, "require_feature_permission", launch_gate)
    coordinator = Coordinator.__new__(Coordinator)
    coordinator.service = SimpleNamespace(pool=Pool())
    coordinator.executor = GuardedExecutor()
    coordinator.session_id = "session"
    coordinator.applied_revision = 7
    common = {
        "mode": "worker",
        "prompt": prompt,
        "cwd": tmp_path,
        "timeout_seconds": 5,
        "max_turns": 1,
        "budget_usd": 0,
        "empty_mcp_config": None,
        "add_dirs": (),
        "executor": coordinator.executor,
        "policy": ExecutionPolicy(
            phase="worker",
            read_roots=(str(tmp_path), str(Path(sys.executable).resolve().parent), str(Path(sys.prefix)), "/bin"),
            write_roots=(str(tmp_path),),
            denied_roots=(),
            network=True,
            timeout_seconds=5,
            max_output_bytes=1024 * 1024,
        ),
    }
    claude_capture = tmp_path / "claude.prompt"
    codex_capture = tmp_path / "codex.prompt"
    host_log_root = tmp_path.parent / f"{tmp_path.name}-host-logs"
    claude_env = {"PATH": str(tmp_path), "CAPTURE": str(claude_capture), "ENGINE": "claude"}
    codex_env = {"PATH": str(tmp_path), "CAPTURE": str(codex_capture), "ENGINE": "codex"}
    claude = EngineConfig((str(child),))
    codex = EngineConfig((str(child),))
    process, result = await coordinator._launch_worker_engine(
        "claude", claude, log_dir=host_log_root / "claude", name="worker", env=claude_env, **common
    )
    assert process.ok and result.error is None
    process, result = await coordinator._launch_worker_engine(
        "codex", codex, log_dir=host_log_root / "codex", name="worker", env=codex_env, **common
    )
    assert process.ok and result.error is None
    assert claude_capture.read_text(encoding="utf-8") == codex_capture.read_text(encoding="utf-8")

    current_items[0] = replace(items[0], expires_at=now - timedelta(seconds=1))
    expired_claude_capture = tmp_path / "expired-claude.prompt"
    expired_codex_capture = tmp_path / "expired-codex.prompt"
    with pytest.raises(PermissionError, match="no longer valid"):
        await coordinator._launch_worker_engine(
            "claude",
            claude,
            log_dir=tmp_path / "expired-claude",
            name="worker",
            env={**claude_env, "CAPTURE": str(expired_claude_capture)},
            **common,
        )
    with pytest.raises(PermissionError, match="no longer valid"):
        await coordinator._launch_worker_engine(
            "codex",
            codex,
            log_dir=tmp_path / "expired-codex",
            name="worker",
            env={**codex_env, "CAPTURE": str(expired_codex_capture)},
            **common,
        )
    assert not expired_claude_capture.exists()
    assert not expired_codex_capture.exists()
    assert len(gate_calls) == 4
