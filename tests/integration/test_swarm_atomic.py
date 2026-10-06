"""Real PostgreSQL: acceptance rollback and concurrent call-budget reservations."""
# ruff: noqa: F811 -- imported pytest fixtures are requested by parameter name.

import asyncio

import pytest

from tests.integration.test_swarm_runtime import (  # noqa: F401
    _session_with_plan,
    _task,
    db_pool,
    ecosystem,
    service,
    swarm_dsn,
)
from whilly.swarm.store import SwarmStoreError


async def test_call_budget_is_atomic_and_survives_new_store(service, ecosystem):
    sid, _ = await _session_with_plan(service, ecosystem["registry"], [_task("one")])
    results = await asyncio.gather(
        *[
            service.store.reserve_agent_call(sid, engine="codex", mode="worker", prompt_chars=10, max_calls=2)
            for _ in range(5)
        ],
        return_exceptions=True,
    )
    assert sum(isinstance(x, int) for x in results) == 2
    assert sum(isinstance(x, SwarmStoreError) for x in results) == 3
    report = await type(service.store)(service.pool).agent_usage(sid)
    assert report["calls"] == 2
    assert report["unknown_cost_calls"] == 2
    assert report["input_tokens"] is None
    assert report["unknown_input_tokens_calls"] == 2


async def test_completion_hook_failure_rolls_back_done_and_evidence(service, ecosystem, db_pool):
    sid, revision = await _session_with_plan(service, ecosystem["registry"], [_task("one")])
    await service.apply_revision(sid, revision)
    from whilly.swarm.store import session_tag

    await service.repo.register_worker("atomic-worker", "localhost", "atomic-token", tags=[session_tag(sid)])
    task = await service.repo.claim_task("atomic-worker", f"swarm-{sid}")
    task = await service.repo.start_task(task.id, task.version)

    async def failing_hook(conn):
        await conn.execute("UPDATE swarm_task_context SET outcome='accepted' WHERE task_id=$1", task.id)
        raise RuntimeError("injected evidence failure")

    with pytest.raises(RuntimeError, match="injected evidence"):
        await service.repo.complete_task(task.id, task.version, on_complete=failing_hook)
    async with db_pool.acquire() as conn:
        assert await conn.fetchval("SELECT status FROM tasks WHERE id=$1", task.id) == "IN_PROGRESS"
        assert await conn.fetchval("SELECT outcome FROM swarm_task_context WHERE task_id=$1", task.id) is None
        assert (
            await conn.fetchval("SELECT count(*) FROM events WHERE task_id=$1 AND event_type='COMPLETE'", task.id) == 0
        )


async def test_acceptance_requires_matching_evidence_and_preserves_unknown_cost(service, ecosystem, db_pool):
    sid, revision = await _session_with_plan(service, ecosystem["registry"], [_task("one")])
    await service.apply_revision(sid, revision)
    from whilly.swarm.store import session_tag

    await service.repo.register_worker("accept-worker", "localhost", "accept-token", tags=[session_tag(sid)])
    task = await service.repo.claim_task("accept-worker", f"swarm-{sid}")
    task = await service.repo.start_task(task.id, task.version)
    attempt, _ = await service.store.start_attempt(
        task.id,
        coordinator_id="test",
        host="localhost",
        worktree_path="/tmp/test",
        branch="test",
        log_dir="/tmp/test-log",
        base_sha="a" * 40,
    )
    evidence = dict(
        head_sha="b" * 40,
        result={"summary": "done"},
        verification=[{"passed": True}],
        review={"verdict": "approve"},
        cost_usd=None,
    )
    with pytest.raises(SwarmStoreError, match="matching running"):
        await service.store.accept_task(service.repo, task.id, task.version, attempt + 1, **evidence)
    async with db_pool.acquire() as conn:
        assert await conn.fetchval("SELECT status FROM tasks WHERE id=$1", task.id) == "IN_PROGRESS"
    await service.store.accept_task(service.repo, task.id, task.version, attempt, **evidence)
    async with db_pool.acquire() as conn:
        assert await conn.fetchval("SELECT status FROM tasks WHERE id=$1", task.id) == "DONE"
        row = await conn.fetchrow("SELECT status,cost_usd,review FROM swarm_task_attempts WHERE id=$1", attempt)
        assert row["status"] == "accepted"
        assert row["cost_usd"] is None


async def test_call_usage_and_scoped_delivery(service, ecosystem):
    sid, revision = await _session_with_plan(service, ecosystem["registry"], [_task("one"), _task("two")])
    await service.apply_revision(sid, revision)
    call = await service.store.reserve_agent_call(sid, engine="codex", mode="worker", prompt_chars=20, max_calls=3)
    await service.store.finish_agent_call(call, usage={"input_tokens": 12, "cached_input_tokens": 10}, cost_usd=None)
    summary = await service.store.agent_usage(sid)
    assert summary["unknown_cost_calls"] == 1
    assert summary["input_tokens"] == 12
    message = await service.store.send_message(sid, sender="user", recipient="one", body="hello")
    await service.store.mark_delivered([message], session_id=sid, recipient="two")
    inbox = await service.store.inbox(sid, "one")
    assert inbox[0]["delivered_at"] is None


async def test_offline_mailbox_peer_roundtrip_to_postgres(service, ecosystem, tmp_path):
    from whilly.swarm.mailbox import enqueue, read_inbox, sync_mailbox

    sid, revision = await _session_with_plan(service, ecosystem["registry"], [_task("one"), _task("two")])
    await service.apply_revision(sid, revision)
    first, second = tmp_path / "one-mail", tmp_path / "two-mail"
    await sync_mailbox(service.store, first, session_id=sid, recipient="one")
    enqueue(first, {"op": "send", "sender": "one", "recipient": "two", "body": "contract v1"})
    await sync_mailbox(service.store, first, session_id=sid, recipient="one")
    await sync_mailbox(service.store, second, session_id=sid, recipient="two")
    messages = read_inbox(second, sid, "two")
    assert messages[0]["body"] == "contract v1"
    enqueue(second, {"op": "ack", "sender": "two", "ids": [messages[0]["id"]]})
    await sync_mailbox(service.store, second, session_id=sid, recipient="two")
    assert read_inbox(second, sid, "two") == []
    assert (await service.store.inbox(sid, "two", include_acked=True))[0]["acked_at"] is not None


async def test_recovery_refuses_unknown_launch_identity(service, ecosystem, db_pool):
    from whilly.swarm.runtime import Coordinator, OrphanProcessError
    from whilly.swarm.store import session_tag

    sid, revision = await _session_with_plan(service, ecosystem["registry"], [_task("one")])
    await service.apply_revision(sid, revision)
    await service.repo.register_worker("unknown-worker", "localhost", "unknown-token", tags=[session_tag(sid)])
    task = await service.repo.claim_task("unknown-worker", f"swarm-{sid}")
    task = await service.repo.start_task(task.id, task.version)
    await service.store.start_attempt(
        task.id,
        coordinator_id="crashed",
        host="localhost",
        worktree_path="/tmp/unknown",
        branch="unknown",
        log_dir="/tmp/unknown-log",
        base_sha="a" * 40,
    )
    with pytest.raises(OrphanProcessError, match="unknown process identity"):
        await Coordinator(service, sid).run()
    async with db_pool.acquire() as conn:
        assert await conn.fetchval("SELECT status FROM tasks WHERE id=$1", task.id) == "IN_PROGRESS"
