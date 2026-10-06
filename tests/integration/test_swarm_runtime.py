"""PostgreSQL integration tests for the local swarm runtime.

Real tables (``alembic upgrade head`` via the shared testcontainers fixture),
real temporary Git repositories (including a bare repository) and real
worktrees. The agent is the injected deterministic stand-in in
``swarm_fake_agent.py``; these tests verify orchestration, persistence and
safety properties, not live-model behaviour.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import socket
import subprocess
import sys
from pathlib import Path
from typing import Any

import asyncpg
import pytest

from tests.swarm_helpers import git, make_bare_repo, make_repo, write_registry
from whilly.swarm.agent import process_group_alive
from whilly.swarm.runtime import Coordinator, OrphanProcessError, SwarmService
from whilly.swarm.store import CoordinatorActiveError, RevisionConflictError, SwarmStoreError, WAITING_TAG

pytestmark = pytest.mark.integration

FAST_LIMITS = {"heartbeat_seconds": 1, "lease_seconds": 3, "agent_timeout_seconds": 60, "max_parallel": 2}

#: Optional DISPOSABLE database for environments without Docker. Every test
#: truncates Whilly and swarm tables in it — never point this at real data.
EXTERNAL_DSN_ENV = "WHILLY_SWARM_TEST_DATABASE_URL"
_TRUNCATE_SQL = (
    "TRUNCATE swarm_model_admissions, swarm_task_attempts, swarm_task_context, swarm_messages, swarm_plan_revisions, swarm_sessions, "
    "events, tasks, plans, workers, control_state RESTART IDENTITY CASCADE"
)
_migrated: set[str] = set()


@pytest.fixture
def swarm_dsn(request: pytest.FixtureRequest) -> str:
    external = os.environ.get(EXTERNAL_DSN_ENV)
    if not external:
        return request.getfixturevalue("postgres_dsn")
    if external not in _migrated:
        from alembic import command

        from tests.conftest import _build_alembic_config

        prior = os.environ.get("WHILLY_DATABASE_URL")
        os.environ["WHILLY_DATABASE_URL"] = external  # env.py reads this before sqlalchemy.url
        try:
            command.upgrade(_build_alembic_config(external), "head")
        finally:
            if prior is None:
                os.environ.pop("WHILLY_DATABASE_URL", None)
            else:
                os.environ["WHILLY_DATABASE_URL"] = prior
        _migrated.add(external)
    return external


@pytest.fixture
async def db_pool(swarm_dsn: str):
    from whilly.adapters.db.pool import close_pool, create_pool

    pool = await create_pool(swarm_dsn, min_size=1, max_size=20)
    try:
        async with pool.acquire() as conn:
            await conn.execute(_TRUNCATE_SQL)
        yield pool
    finally:
        await close_pool(pool)


@pytest.fixture
async def service(db_pool: asyncpg.Pool) -> SwarmService:
    return SwarmService(db_pool)


@pytest.fixture
def ecosystem(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    lib = make_repo(tmp_path, "demo-lib", files={"README.md": "# lib\n", "AGENTS.md": "Lib rules.\n"})
    api = make_bare_repo(tmp_path, "demo-api")
    argv_log = tmp_path / "argv.log"
    monkeypatch.setenv("FAKE_ARGV_LOG", str(argv_log))
    registry = write_registry(
        tmp_path / "registry.json",
        {
            "demo-lib": {
                "path": str(lib),
                "purpose": "Library",
                "verification": [["test", "-f", "README.md"]],
                "verification_policy": {
                    "test": [
                        [
                            sys.executable,
                            "-c",
                            'print(\'<testsuite tests=\\"1\\" failures=\\"0\\" errors=\\"0\\" skipped=\\"0\\"><testcase /></testsuite>\')',
                        ]
                    ],
                    "lint": [[sys.executable, "-c", "print('[]')"]],
                    "architecture": [
                        [sys.executable, "-c", 'print(\'{\\"evaluated_rules\\": 1, \\"violations\\": []}\')']
                    ],
                    "protected_paths": ["AGENTS.md"],
                    "toolchain_id": "test",
                },
            },
            "demo-api": {
                "path": str(api),
                "purpose": "API",
                "depends_on": ["demo-lib"],
                "verification_policy": {
                    "test": [
                        [
                            sys.executable,
                            "-c",
                            'print(\'<testsuite tests=\\"1\\" failures=\\"0\\" errors=\\"0\\" skipped=\\"0\\"><testcase /></testsuite>\')',
                        ]
                    ],
                    "lint": [[sys.executable, "-c", "print('[]')"]],
                    "architecture": [
                        [sys.executable, "-c", 'print(\'{\\"evaluated_rules\\": 1, \\"violations\\": []}\')']
                    ],
                    "protected_paths": ["AGENTS.md"],
                    "toolchain_id": "test",
                },
            },
        },
        limits=FAST_LIMITS,
        state_dir=tmp_path / "state",
    )
    data = json.loads(registry.read_text())
    roots = [
        str(tmp_path),
        str(Path(__file__).resolve().parents[2]),
        str(Path(sys.executable).resolve().parent),
        str(Path(sys.executable).resolve().parents[4]),
        "/opt/homebrew/opt/gettext/lib",
        "/Applications/Xcode.app/Contents/Developer",
        "/usr",
        "/bin",
    ]
    data["execution"] = {
        "toolchains": {
            "test": {
                "read_roots": roots,
                "path": os.environ.get("PATH", "/usr/bin:/bin"),
                "home": str(tmp_path / "execution-home"),
                "temporary": str(tmp_path / "execution-tmp"),
                "auth": {},
            }
        },
        "phases": {
            phase: "test" for phase in ("planner", "escalation", "worker", "review", "verify", "git", "host_script")
        },
    }
    registry.write_text(json.dumps(data))
    return {"lib": lib, "api": api, "registry": registry, "state": tmp_path / "state", "argv_log": argv_log}


def _task(task_id: str, project: str = "demo-lib", *, deps=(), description: str = "do work", verification=None):
    task: dict[str, Any] = {
        "id": task_id,
        "project": project,
        "role": "implementer",
        "description": description,
        "depends_on": list(deps),
    }
    task["verification"] = verification if verification is not None else [["test", "-f", f"{task_id}.txt"]]
    return task


async def _session_with_plan(service: SwarmService, registry: Path, tasks: list[dict[str, Any]]) -> tuple[str, int]:
    session_id = await service.create_session(str(registry), title="test")
    revision, status, error = await service.propose_plan(session_id, {"summary": "t", "tasks": tasks})
    assert status == "proposed", error
    return session_id, revision


async def _tasks(pool: asyncpg.Pool, session_id: str) -> dict[str, asyncpg.Record]:
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT c.local_id, t.* FROM tasks t JOIN swarm_task_context c ON c.task_id = t.id WHERE c.session_id = $1",
            session_id,
        )
    return {row["local_id"]: row for row in rows}


async def test_happy_path_dependency_handoff_bare_repo_and_parallel(service, ecosystem, db_pool) -> None:
    lib_head = git(ecosystem["lib"], "rev-parse", "HEAD").strip()
    session_id, revision = await _session_with_plan(
        service,
        ecosystem["registry"],
        [
            _task("lib-a"),
            _task("lib-b"),
            _task(
                "api-c", "demo-api", deps=["lib-a"], verification=[["grep", "-q", "dep:summary-of-lib-a", "api-c.txt"]]
            ),
        ],
    )
    await service.apply_revision(session_id, revision)
    await service.store.send_message(session_id, sender="user", recipient="api-c", body="use the v2 schema")
    await service.store.send_message(session_id, sender="user", recipient="lib-a", body="private to lib-a")
    rows = await _tasks(db_pool, session_id)
    assert WAITING_TAG in rows["api-c"]["required_tags"]
    assert rows["api-c"]["plan_id"] == f"swarm-{session_id}"

    summary = await Coordinator(service, session_id, poll_seconds=0.1).run()
    assert summary.status == "finished" and summary.ok, summary
    rows = await _tasks(db_pool, session_id)
    assert {k: r["status"] for k, r in rows.items()} == {"lib-a": "DONE", "lib-b": "DONE", "api-c": "DONE"}

    report = await service.report(session_id)
    attempts = {a["task"]: a for a in report["attempts"]}
    api = attempts["api-c"]
    content = (Path(api["worktree"]) / "api-c.txt").read_text()
    assert "dep:summary-of-lib-a" in content  # dependency result hydrated into the prompt
    assert "inbox:use the v2 schema" in content and "private to lib-a" not in content  # peer isolation
    assert api["head_sha"] != api["base_sha"] and api["review_verdict"] == "approve"
    assert api["result"]["changed_files"] == ["api-c.txt"]
    assert all(v["exit_code"] == 0 for v in api["verification"])
    assert report["summary"]["total_cost_usd"] > 0
    assert Path(report["report_path"]).exists()
    # Branches live in the registered repositories, but their checkouts are untouched.
    assert git(ecosystem["lib"], "rev-parse", "HEAD").strip() == lib_head
    assert git(ecosystem["lib"], "status", "--porcelain") == ""
    assert api["branch"] == f"swarm/{session_id}/r1-api-c-a1"
    # Planner/reviewer were read-only; inspect host-owned launch records
    # because ambient child env is intentionally stripped by the sandbox.
    launch_records = []
    for argv_path in sorted((ecosystem["state"] / "sessions" / session_id).rglob("*.argv.json")):
        argv = json.loads(argv_path.read_text())
        prompt_path = argv_path.with_name(argv_path.name.replace(".argv.json", ".prompt.md"))
        prompt = prompt_path.read_text() if prompt_path.exists() else ""
        launch_records.append((argv, prompt))
    reviews = [(argv, prompt) for argv, prompt in launch_records if "independent read-only reviewer" in prompt]
    assert len(reviews) == 3 and all("Read,Grep,Glob" in argv and "--strict-mcp-config" in argv for argv, _ in reviews)
    async with db_pool.acquire() as conn:
        assert await conn.fetchval("SELECT COUNT(*) FROM tasks WHERE plan_id = $1", f"swarm-{session_id}") == 3


async def test_invalid_plan_creates_no_tasks_and_keeps_history(service, ecosystem, db_pool) -> None:
    session_id = await service.create_session(str(ecosystem["registry"]))
    revision, status, error = await service.propose_plan(
        session_id, {"tasks": [_task("a", deps=["ghost"]), _task("b", "no-such-project")]}
    )
    assert status == "invalid" and "unknown task 'ghost'" in error and "unknown project" in error
    with pytest.raises(SwarmStoreError, match="no proposed revision"):
        await service.apply_revision(session_id, None)
    async with db_pool.acquire() as conn:
        assert await conn.fetchval("SELECT COUNT(*) FROM tasks") == 0
    history = await service.store.chat_history(session_id)
    assert any("rejected" in m["body"] for m in history)
    status_view = await service.status(session_id)
    assert status_view["revisions"][0]["status"] == "invalid"


async def test_chat_is_discussion_until_plan_requested(service, ecosystem, tmp_path, monkeypatch) -> None:
    plan_file = tmp_path / "plan.json"
    plan_file.write_text(json.dumps({"summary": "p", "tasks": [_task("lib-a")]}))
    monkeypatch.setenv("FAKE_PLAN_FILE", str(plan_file))
    session_id = await service.create_session(str(ecosystem["registry"]))
    reply = await service.chat(session_id, "what could we improve?")
    assert reply.revision is None and "discuss" in reply.reply
    reply = await service.chat(session_id, "give me the plan", request_plan=True)
    assert reply.revision == 1 and reply.revision_status == "proposed"
    assert (await service.status(session_id))["tasks"] == []  # nothing queued without an explicit run
    senders = [m["sender"] for m in await service.store.chat_history(session_id)]
    assert senders[:4] == ["user", "planner", "user", "planner"]
    planner_calls = [
        json.loads(path.read_text())
        for path in sorted((ecosystem["state"] / "sessions" / session_id / "planner").glob("*.argv.json"))
    ]
    assert all("--tools" in c and "--setting-sources" in c for c in planner_calls)


async def test_revision_atomicity_staleness_and_live_coordinator(service, ecosystem, db_pool) -> None:
    session_id, rev1 = await _session_with_plan(service, ecosystem["registry"], [_task("a"), _task("b", deps=["a"])])
    rev2, _, _ = await service.propose_plan(session_id, {"tasks": [_task("stale")]})
    rev3, _, _ = await service.propose_plan(session_id, {"tasks": [_task("c"), _task("d")]})
    await service.apply_revision(session_id, rev1)

    # A conflicting row makes the second insert of revision 3 fail mid-transaction.
    async with db_pool.acquire() as conn:
        await conn.execute("INSERT INTO plans (id, name) VALUES ('other', 'other')")
        await conn.execute(
            "INSERT INTO tasks (id, plan_id, status) VALUES ($1, 'other', 'PENDING')", f"{session_id}.r3.d"
        )
    with pytest.raises(asyncpg.UniqueViolationError):
        await service.apply_revision(session_id, rev3)
    assert (await service.store.get_revision(session_id, rev3))["status"] == "proposed"
    rows = await _tasks(db_pool, session_id)
    assert sorted(rows) == ["a", "b"] and rows["a"]["status"] == "PENDING"  # nothing superseded, nothing partial
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM tasks WHERE id = $1", f"{session_id}.r3.d")

    assert await service.store.acquire_lease(session_id, "someone-else", host="h", pid=1, lease_seconds=60)
    with pytest.raises(CoordinatorActiveError):
        await service.apply_revision(session_id, rev3)
    with pytest.raises(CoordinatorActiveError):
        await Coordinator(service, session_id).run()
    await service.store.release_lease(session_id, "someone-else", status="stopped")

    await service.apply_revision(session_id, rev3)
    rows = await _tasks(db_pool, session_id)
    assert rows["a"]["status"] == "SKIPPED" and rows["b"]["status"] == "SKIPPED"
    assert rows["c"]["status"] == "PENDING" and rows["d"]["status"] == "PENDING"
    assert (await service.store.get_revision(session_id, rev1))["status"] == "superseded"
    with pytest.raises(RevisionConflictError, match="older than applied"):
        await service.apply_revision(session_id, rev2)
    with pytest.raises(SwarmStoreError, match="only proposed"):
        await service.apply_revision(session_id, rev1)


async def test_failures_are_named_and_block_dependents(service, ecosystem, db_pool) -> None:
    session_id, revision = await _session_with_plan(
        service,
        ecosystem["registry"],
        [
            _task("crash", description="BEHAVIOR:nonzero"),
            _task("after-crash", deps=["crash"]),
            _task("garbled", description="BEHAVIOR:invalid"),
            _task("dirty", description="BEHAVIOR:dirty"),
            _task("red", verification=[["false"]]),
            _task("rejected", description="REVIEW:reject"),
            _task("blocked", description="BEHAVIOR:blocked"),
        ],
    )
    await service.apply_revision(session_id, revision)
    summary = await Coordinator(service, session_id, max_parallel=3, poll_seconds=0.1).run()
    assert not summary.ok
    status = {t["task"]: t for t in (await service.status(session_id))["tasks"]}
    expected = {
        "crash": "agent_failed",
        "garbled": "invalid_result",
        "red": "verification_failed",
        "rejected": "review_rejected",
        "blocked": "agent_blocked",
    }
    for task, outcome in expected.items():
        assert status[task]["status"] == "FAILED" and status[task]["outcome"] == outcome, status[task]
        assert status[task]["attempts"] == 1  # no automatic retry
    assert status["dirty"]["status"] == "DONE"
    assert status["after-crash"]["status"] == "PENDING"
    assert status["after-crash"]["blocker"] == "dependency failed: crash"
    report = await service.report(session_id)
    rejected = next(a for a in report["attempts"] if a["task"] == "rejected")
    assert rejected["review"]["verdict"] == "reject" and rejected["cost_usd"] > 0
    red = next(a for a in report["attempts"] if a["task"] == "red")
    failed_check = next(check for check in red["verification"] if check["argv"] == ["false"])
    assert failed_check["exit_code"] == 1 and not failed_check["passed"]
    async with db_pool.acquire() as conn:
        reason = await conn.fetchval(
            "SELECT payload->>'reason' FROM events WHERE task_id = $1 AND event_type = 'FAIL'", f"{session_id}.r1.red"
        )
    assert reason == "verification_failed"

    # Explicit rerun grants exactly one more attempt.
    await service.store.rerun_task(session_id, "crash")
    await Coordinator(service, session_id, poll_seconds=0.1).run()
    crash = next(t for t in (await service.status(session_id))["tasks"] if t["task"] == "crash")
    assert crash["status"] == "FAILED" and crash["attempts"] == 2
    with pytest.raises(RevisionConflictError):
        await service.store.rerun_task(session_id, "after-crash")


async def test_agent_timeout(service, ecosystem, db_pool, monkeypatch, tmp_path) -> None:
    data = json.loads(ecosystem["registry"].read_text())
    data["limits"]["agent_timeout_seconds"] = 1
    ecosystem["registry"].write_text(json.dumps(data))
    flag = tmp_path / "sleep.flag"
    flag.write_text("x")
    monkeypatch.setenv("FAKE_SLEEP_FLAG", str(flag))
    session_id, revision = await _session_with_plan(
        service, ecosystem["registry"], [_task("slow", description=f"BEHAVIOR:sleep {flag}")]
    )
    await service.apply_revision(session_id, revision)
    await Coordinator(service, session_id, poll_seconds=0.1).run()
    task = (await service.status(session_id))["tasks"][0]
    assert task["status"] == "FAILED" and task["outcome"] == "agent_timeout"


async def _wait_running_attempt(service: SwarmService, session_id: str) -> dict[str, Any]:
    for _ in range(200):
        running = await service.store.running_attempts(session_id)
        if running and running[0].get("agent_pgid"):
            return running[0]
        await asyncio.sleep(0.05)
    raise AssertionError("attempt never started")


async def test_stop_cancels_agent_and_restart_completes(service, ecosystem, db_pool, monkeypatch, tmp_path) -> None:
    flag = tmp_path / "sleep.flag"
    flag.write_text("x")
    monkeypatch.setenv("FAKE_SLEEP_FLAG", str(flag))
    session_id, revision = await _session_with_plan(
        service, ecosystem["registry"], [_task("slow", description=f"BEHAVIOR:sleep {flag}")]
    )
    await service.apply_revision(session_id, revision)
    run = asyncio.create_task(Coordinator(service, session_id, poll_seconds=0.1).run())
    attempt = await _wait_running_attempt(service, session_id)
    with pytest.raises(CoordinatorActiveError):
        await Coordinator(service, session_id).run()  # second coordinator refused
    await service.stop(session_id)
    summary = await asyncio.wait_for(run, timeout=30)
    assert summary.status == "stopped"
    assert not process_group_alive(attempt["agent_pgid"])
    rows = await _tasks(db_pool, session_id)
    assert rows["slow"]["status"] == "PENDING"
    assert (await service.store.attempts_for_session(session_id))[0]["status"] == "cancelled"

    flag.unlink()
    summary = await Coordinator(service, session_id, poll_seconds=0.1).run()
    assert summary.ok, summary
    task = (await service.status(session_id))["tasks"][0]
    assert task["status"] == "DONE" and task["attempts"] == 1  # cancelled attempts are not counted


async def test_crash_recovery_refuses_unverified_pgids_even_with_kill_flag(
    service, ecosystem, db_pool, tmp_path
) -> None:
    session_id, revision = await _session_with_plan(service, ecosystem["registry"], [_task("t1")])
    await service.apply_revision(session_id, revision)
    # Simulate a coordinator that crashed mid-attempt: claimed task, running
    # attempt with a live process group, expired lease.
    orphan = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"], start_new_session=True)
    try:
        await service.repo.register_worker("dead-coordinator", "host", "hash", tags=[f"swarm:{session_id}"])
        claimed = await service.repo.claim_task("dead-coordinator", f"swarm-{session_id}")
        await service.repo.start_task(claimed.id, claimed.version)
        attempt_id, _ = await service.store.start_attempt(
            claimed.id,
            coordinator_id="dead-coordinator",
            host=socket.gethostname(),
            worktree_path="/nonexistent",
            branch="b",
            log_dir="/nonexistent",
            base_sha="0" * 40,
        )
        await service.store.set_attempt_pgid(attempt_id, orphan.pid)
        assert await service.store.acquire_lease(session_id, "dead-coordinator", host="h", pid=1, lease_seconds=60)
        async with db_pool.acquire() as conn:
            await conn.execute(
                "UPDATE swarm_sessions SET lease_expires_at = NOW() - interval '1 second' WHERE id = $1", session_id
            )

        with pytest.raises(OrphanProcessError, match="still alive"):
            await Coordinator(service, session_id).run()
        assert orphan.poll() is None
        with pytest.raises(OrphanProcessError, match="refusing to kill by PGID"):
            await Coordinator(service, session_id, kill_orphans=True, poll_seconds=0.1).run()
        assert orphan.poll() is None
        # The fixture owns this exact Popen handle, unlike crash recovery
        # which only knows an integer that may have been recycled.
        orphan.terminate()
        assert orphan.wait(timeout=10) == -signal.SIGTERM
        summary = await Coordinator(service, session_id, poll_seconds=0.1).run()
        assert summary.ok, summary
        attempts = await service.store.attempts_for_session(session_id)
        assert [a["status"] for a in attempts] == ["abandoned", "accepted"]
    finally:
        if orphan.poll() is None:
            orphan.kill()


async def test_peer_message_validation_and_ack(service, ecosystem) -> None:
    session_id, revision = await _session_with_plan(service, ecosystem["registry"], [_task("a"), _task("b")])
    with pytest.raises(SwarmStoreError, match="unknown recipient"):
        await service.store.send_message(session_id, sender="user", recipient="a", body="before apply")
    await service.apply_revision(session_id, revision)
    mid = await service.store.send_message(session_id, sender="a", recipient="b", body="hello b", task_ref="a")
    for kwargs, match in (
        ({"sender": "a", "recipient": "ghost"}, "unknown recipient"),
        ({"sender": "a", "recipient": "a"}, "must differ"),
        ({"sender": "a", "recipient": "b", "task_ref": "ghost"}, "unknown task"),
    ):
        with pytest.raises(SwarmStoreError, match=match):
            await service.store.send_message(session_id, body="x", **kwargs)
    with pytest.raises(SwarmStoreError, match="unknown session"):
        await service.store.send_message("s-missing", sender="user", recipient="a", body="x")
    assert await service.store.inbox(session_id, "a") == []
    with pytest.raises(SwarmStoreError, match="not addressed"):
        await service.store.ack_messages(session_id, "a", [mid])
    assert await service.store.ack_messages(session_id, "b", [mid]) == [mid]
    assert await service.store.inbox(session_id, "b") == []
    assert len(await service.store.inbox(session_id, "b", include_acked=True)) == 1


async def test_same_repository_dependency_uses_accepted_commit(service, ecosystem):
    sid, revision = await _session_with_plan(
        service,
        ecosystem["registry"],
        [
            _task("first"),
            _task("second", deps=["first"], verification=[["test", "-f", "first.txt"], ["test", "-f", "second.txt"]]),
        ],
    )
    await service.apply_revision(sid, revision)
    result = await Coordinator(service, sid, poll_seconds=0.1).run()
    assert result.ok, result
    attempts = await service.store.attempts_for_session(sid)
    first = next(a for a in attempts if a["task_id"].endswith(".first"))
    second = next(a for a in attempts if a["task_id"].endswith(".second"))
    assert second["base_sha"] == first["head_sha"]
    assert (Path(second["worktree_path"]) / "first.txt").is_file()
    assert second["result"]["base_sha"] == first["head_sha"]
    assert second["result"]["changed_files"] == ["second.txt"]


async def test_divergent_same_repository_heads_require_integration(service, ecosystem):
    sid, revision = await _session_with_plan(
        service,
        ecosystem["registry"],
        [
            _task("left"),
            _task("right"),
            _task("join", deps=["left", "right"]),
        ],
    )
    await service.apply_revision(sid, revision)
    result = await Coordinator(service, sid, poll_seconds=0.1).run()
    assert not result.ok
    tasks = {t["task"]: t for t in (await service.status(sid))["tasks"]}
    assert tasks["left"]["status"] == tasks["right"]["status"] == "DONE"
    assert tasks["join"]["outcome"] == "integration_required"


def test_cli_end_to_end(swarm_dsn: str, ecosystem, tmp_path: Path) -> None:
    """Drive the real ``python -m whilly swarm`` entry point against Postgres."""

    async def truncate() -> None:
        conn = await asyncpg.connect(swarm_dsn.replace("postgresql+asyncpg://", "postgresql://"))
        try:
            await conn.execute(_TRUNCATE_SQL)
        finally:
            await conn.close()

    asyncio.run(truncate())
    env = {**os.environ, "WHILLY_DATABASE_URL": swarm_dsn}
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[2]) + os.pathsep + env.get("PYTHONPATH", "")

    def swarm(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "whilly", "swarm", *args], env=env, capture_output=True, text=True, timeout=180
        )

    assert swarm("registry", "validate", "--registry", str(ecosystem["registry"])).returncode == 0
    new = swarm("new", "--registry", str(ecosystem["registry"]), "--title", "cli")
    assert new.returncode == 0, new.stderr
    session_id = new.stdout.strip()
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps({"summary": "cli", "tasks": [_task("one"), _task("two", deps=["one"])]}))
    assert swarm("propose", "--session", session_id, "--plan-file", str(plan)).returncode == 0
    no_revision = swarm("run", "--session", session_id)
    assert no_revision.returncode == 2 and "--latest" in no_revision.stderr
    run = swarm("run", "--session", session_id, "--latest", "--workers", "2")
    assert run.returncode == 0, run.stdout + run.stderr
    assert "accepted: one" in run.stdout and "accepted: two" in run.stdout
    status = json.loads(swarm("status", "--session", session_id, "--json").stdout)
    assert [t["status"] for t in status["tasks"]] == ["DONE", "DONE"]
    assert status["dashboard"] == f"whilly dashboard --plan swarm-{session_id}"
    sent = swarm("message", "--session", session_id, "--from", "user", "--to", "two", "follow-up")
    assert sent.returncode == 0, sent.stderr
    inbox = json.loads(swarm("inbox", "--session", session_id, "--recipient", "two", "--json").stdout)
    assert inbox[0]["body"] == "follow-up"
    assert swarm("inbox", "--session", session_id, "--recipient", "one", "--ack", str(inbox[0]["id"])).returncode == 2
    assert swarm("inbox", "--session", session_id, "--recipient", "two", "--ack", str(inbox[0]["id"])).returncode == 0
    bad = swarm("message", "--session", session_id, "--from", "user", "--to", "ghost", "x")
    assert bad.returncode == 2 and "unknown recipient" in bad.stderr
    report = swarm("report", "--session", session_id)
    assert report.returncode == 0 and "accepted: one, two" in report.stdout
    assert swarm("stop", "--session", session_id).returncode == 0
