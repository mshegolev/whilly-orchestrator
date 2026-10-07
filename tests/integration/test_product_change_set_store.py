"""Real PostgreSQL product change-set persistence and migration contracts."""

import asyncio
import os
from urllib.parse import urlsplit
from uuid import uuid4

import asyncpg
import pytest
from alembic import command
from alembic.script import ScriptDirectory

from tests.conftest import _build_alembic_config
from whilly.swarm.change_set import (
    ChangeSetStatus as C,
    RepoChangeStatus as R,
    ProductChangeSetService,
    Evidence,
    EvidenceOutcome as O,
    ExternalEffectReceipt,
    VersionConflict,
    EffectKeyConflict,
    TransitionError,
)

SHA = "a" * 40
DIGEST = "b" * 64


def proof(kind="gate"):
    return Evidence(kind, O.PASSED, sha=SHA, job_id="job-1")


@pytest.fixture(scope="module")
def change_dsn(request):
    dsn = os.environ.get("WHILLY_CHANGE_SET_TEST_DATABASE_URL")
    if not dsn:
        dsn = request.getfixturevalue("postgres_dsn")
    prior = os.environ.get("WHILLY_DATABASE_URL")
    os.environ["WHILLY_DATABASE_URL"] = dsn
    try:
        command.upgrade(_build_alembic_config(dsn), "head")
    finally:
        if prior is None:
            os.environ.pop("WHILLY_DATABASE_URL", None)
        else:
            os.environ["WHILLY_DATABASE_URL"] = prior
    return dsn


@pytest.fixture
async def pool(change_dsn):
    pool = await asyncpg.create_pool(change_dsn, min_size=1, max_size=8)
    try:
        yield pool
    finally:
        await pool.close()


@pytest.fixture
def store(pool):
    from whilly.swarm.change_set_store import ProductChangeSetStore

    return ProductChangeSetStore(pool)


async def create(store):
    return await ProductChangeSetService(store).create(
        product_id="demo-product",
        goal="Deliver compatible changes",
        acceptance_criteria=("stage chat works",),
        registry_snapshot={"projects": {"library": {"depends_on": []}, "api": {"depends_on": ["library"]}}},
        base_shas={"library": SHA, "api": "c" * 40},
        approval_digest=DIGEST,
    )


def test_041_is_the_linear_successor_and_current_head():
    script = ScriptDirectory.from_config(_build_alembic_config("postgresql://fixture/example"))
    assert script.get_current_head() == "041_product_change_sets"
    assert script.get_revision("041_product_change_sets").down_revision == "040_learning_runs"


@pytest.mark.asyncio
async def test_create_reload_preserves_complete_snapshot_and_creation_events(store):
    value = await create(store)
    current = await store.get(value.change_id)
    assert current == value
    assert {r.repo_id for r in current.repo_changes} == {"library", "api"}
    events = await store.events(value.change_id)
    assert len(events) == 3
    assert {event["repo_id"] for event in events} == {None, "library", "api"}
    assert all(event["evidence"]["outcome"] == "UNAVAILABLE" for event in events)


@pytest.mark.asyncio
async def test_compare_and_swap_race_has_one_transition_and_one_event(store):
    value = await create(store)
    results = await asyncio.gather(
        *(store.transition(value.change_id, 1, C.PLANNED, proof()) for _ in range(2)),
        return_exceptions=True,
    )
    assert sum(isinstance(result, VersionConflict) for result in results) == 1
    current = await store.get(value.change_id)
    assert current.status == C.PLANNED and current.version == 2
    events = [event for event in await store.events(value.change_id) if event["repo_id"] is None]
    assert [event["entity_version"] for event in events] == [1, 2]
    assert events[-1]["evidence"]["sha"] == SHA


@pytest.mark.asyncio
async def test_invalid_done_leaves_state_and_audit_unchanged(store, pool):
    value = await create(store)
    with pytest.raises(TransitionError):
        await store.transition(value.change_id, 1, C.DONE, proof())
    assert (await store.get(value.change_id)).version == 1
    assert len(await store.events(value.change_id)) == 3
    with pytest.raises(asyncpg.CheckViolationError):
        await pool.execute("UPDATE product_change_sets SET status='DONE', version=2 WHERE id=$1", value.change_id)


@pytest.mark.asyncio
async def test_repo_versioning_and_scope(store):
    value = await create(store)
    await store.transition(value.change_id, 1, C.PLANNED, proof())
    await store.transition(value.change_id, 2, C.EXECUTING, proof())
    repo = await store.transition_repo(value.change_id, "library", 1, R.WORKTREE_READY, proof())
    assert repo.version == 2 and repo.status == R.WORKTREE_READY
    with pytest.raises(VersionConflict):
        await store.transition_repo(value.change_id, "library", 1, R.IMPLEMENTING, proof())
    with pytest.raises(KeyError):
        await store.transition_repo(value.change_id, "foreign", 1, R.WORKTREE_READY, proof())
    assert len(await store.events(value.change_id)) == 6


@pytest.mark.asyncio
async def test_repo_work_cannot_bypass_product_execution_or_terminal_state(store, pool):
    value = await create(store)
    with pytest.raises(TransitionError):
        await store.transition_repo(value.change_id, "library", 1, R.WORKTREE_READY, proof())
    assert len(await store.events(value.change_id)) == 3
    await store.transition(
        value.change_id, 1, C.FAILED, Evidence("gate", O.UNAVAILABLE, absence="mandatory_policy_missing")
    )
    with pytest.raises(TransitionError):
        await store.transition_repo(value.change_id, "library", 1, R.WORKTREE_READY, proof())
    assert (await store.get(value.change_id)).repo_changes[1].status == R.PLANNED


@pytest.mark.asyncio
async def test_database_rejects_evidence_without_measurement_or_named_absence(store, pool):
    value = await create(store)
    with pytest.raises(asyncpg.CheckViolationError):
        await pool.execute(
            "INSERT INTO change_set_events (change_id,entity_version,to_status,evidence,idempotency_key) "
            "VALUES ($1,2,'PLANNED','{}'::jsonb,$2)",
            value.change_id,
            "invalid_" + uuid4().hex,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "command_args", [[None], [False], [""], ["tool", None], ["tool", False], ["tool", ""], [{}], [[]]]
)
@pytest.mark.parametrize("mode", ["command", "job", "absence"])
@pytest.mark.parametrize(
    "table", ["product_change_sets", "repo_changes", "change_set_events", "external_effect_receipts"]
)
async def test_native_evidence_constraints_reject_invalid_command_elements(store, pool, command_args, mode, table):
    import json

    if mode == "absence":
        payload = Evidence("gate", O.UNAVAILABLE, absence="probe_unavailable").to_dict()
    else:
        payload = Evidence(
            "gate", O.PASSED, sha=SHA, command=("tool",), exit_code=0, job_id="job-1" if mode == "job" else None
        ).to_dict()
    payload["command"] = command_args
    with pytest.raises(ValueError, match="evidence_command_invalid"):
        Evidence.from_dict(payload)
    value = await create(store)
    encoded = json.dumps(payload)
    with pytest.raises(asyncpg.CheckViolationError):
        if table == "product_change_sets":
            await pool.execute(
                "UPDATE product_change_sets SET last_evidence=$2::jsonb,version=2 WHERE id=$1", value.change_id, encoded
            )
        elif table == "repo_changes":
            await pool.execute(
                "UPDATE repo_changes SET last_evidence=$2::jsonb,version=2 WHERE change_id=$1 AND repo_id='api'",
                value.change_id,
                encoded,
            )
        elif table == "change_set_events":
            await pool.execute(
                "INSERT INTO change_set_events (change_id,entity_version,to_status,evidence,idempotency_key) "
                "VALUES ($1,2,'PLANNED',$2::jsonb,$3)",
                value.change_id,
                encoded,
                "bad_command_" + uuid4().hex,
            )
        else:
            await pool.execute(
                "INSERT INTO external_effect_receipts (effect_key,change_id,operation,request_digest,evidence) "
                "VALUES ($1,$2,'merge',$3,$4::jsonb)",
                "bad_command_" + uuid4().hex,
                value.change_id,
                DIGEST,
                encoded,
            )
    assert await store.get(value.change_id) == value
    assert len(await store.events(value.change_id)) == 3
    assert await store.effects(value.change_id) == ()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        Evidence("gate", O.PASSED, sha=SHA, command=("tool", "argument", " "), exit_code=0).to_dict(),
        Evidence("gate", O.PASSED, sha=SHA, job_id="job-1").to_dict(),
        Evidence("gate", O.UNAVAILABLE, absence="probe_unavailable").to_dict(),
    ],
)
async def test_native_evidence_preserves_valid_commands_and_named_absence(store, pool, payload):
    import json

    value = await create(store)
    await pool.execute(
        "INSERT INTO external_effect_receipts (effect_key,change_id,operation,request_digest,evidence) "
        "VALUES ($1,$2,'probe',$3,$4::jsonb)",
        key := "good_command_" + uuid4().hex,
        value.change_id,
        DIGEST,
        json.dumps(payload),
    )
    assert (await store.get_effect(key)).evidence == Evidence.from_dict(payload)


@pytest.mark.asyncio
async def test_duplicate_effect_key_returns_original_receipt_after_reload(store, pool):
    from whilly.swarm.change_set_store import ProductChangeSetStore

    value = await create(store)
    original = ExternalEffectReceipt("effect_" + uuid4().hex, value.change_id, "library", "merge", DIGEST, proof())
    assert await store.record_effect(original) == original
    retry = ExternalEffectReceipt(
        original.effect_key,
        value.change_id,
        "library",
        "merge",
        DIGEST,
        Evidence("gate", O.PASSED, sha="f" * 40, job_id="different-response"),
    )
    assert await ProductChangeSetStore(pool).record_effect(retry) == original
    assert await ProductChangeSetStore(pool).get_effect(original.effect_key) == original
    assert await store.effects(value.change_id) == (original,)
    assert (
        await pool.fetchval("SELECT count(*) FROM external_effect_receipts WHERE effect_key=$1", original.effect_key)
        == 1
    )


@pytest.mark.asyncio
async def test_effect_key_cannot_cross_scope_operation_or_request(store):
    value = await create(store)
    foreign = await create(store)
    key = "effect_" + uuid4().hex
    await store.record_effect(ExternalEffectReceipt(key, value.change_id, "library", "merge", DIGEST, proof()))
    for change_id, repo, operation, digest in (
        (foreign.change_id, "library", "merge", DIGEST),
        (value.change_id, "api", "merge", DIGEST),
        (value.change_id, "library", "revert", DIGEST),
        (value.change_id, "library", "merge", "f" * 64),
    ):
        with pytest.raises(EffectKeyConflict):
            await store.record_effect(ExternalEffectReceipt(key, change_id, repo, operation, digest, proof()))


@pytest.mark.asyncio
async def test_transition_event_keys_do_not_alias_opaque_change_and_repo_ids(store):
    prefix = "scope_" + uuid4().hex
    service = ProductChangeSetService(store)
    common = dict(
        product_id="demo",
        goal="scope",
        acceptance_criteria=("accepted",),
        registry_snapshot={"projects": {"product": {"depends_on": []}}},
        base_shas={"product": SHA},
    )
    first = await service.create(change_id=prefix + ":repo", **common)
    second = await service.create(change_id=prefix, **common)
    assert len(await store.events(first.change_id)) == 2
    assert len(await store.events(second.change_id)) == 2


@pytest.mark.asyncio
async def test_append_only_events_receipts_and_immutable_definition(store, pool):
    value = await create(store)
    receipt = ExternalEffectReceipt("effect_" + uuid4().hex, value.change_id, "api", "merge", DIGEST, proof())
    await store.record_effect(receipt)
    statements = (
        ("UPDATE change_set_events SET evidence='{}'::jsonb WHERE change_id=$1", value.change_id),
        ("DELETE FROM change_set_events WHERE change_id=$1", value.change_id),
        ("UPDATE external_effect_receipts SET evidence='{}'::jsonb WHERE change_id=$1", value.change_id),
        ("DELETE FROM external_effect_receipts WHERE change_id=$1", value.change_id),
        ("UPDATE product_change_sets SET registry_digest=repeat('c',64) WHERE id=$1", value.change_id),
        ("UPDATE repo_changes SET base_sha=repeat('c',40) WHERE change_id=$1", value.change_id),
    )
    for sql, identity in statements:
        with pytest.raises(asyncpg.PostgresError):
            await pool.execute(sql, identity)
    for table in ("change_set_events", "external_effect_receipts"):
        with pytest.raises(asyncpg.PostgresError):
            await pool.execute(f"TRUNCATE {table}")
    assert (await store.get(value.change_id)) == value
    assert len(await store.events(value.change_id)) == 3


@pytest.mark.asyncio
async def test_event_insert_failure_rolls_back_state_transition(store, pool):
    import json

    value = await create(store)
    await pool.execute(
        "INSERT INTO change_set_events (change_id, entity_version, from_status, to_status, evidence, idempotency_key) "
        "VALUES ($1,2,'DRAFT','PLANNED',$2::jsonb,$3)",
        value.change_id,
        json.dumps(proof().to_dict()),
        "injected_" + uuid4().hex,
    )
    with pytest.raises(asyncpg.UniqueViolationError):
        await store.transition(value.change_id, 1, C.PLANNED, proof())
    assert (await store.get(value.change_id)).status == C.DRAFT
    assert (await store.get(value.change_id)).version == 1


def test_041_roundtrip_only_in_a_unique_sibling_database(change_dsn, monkeypatch):
    name = "whilly_change_schema_" + uuid4().hex
    sibling = urlsplit(change_dsn)._replace(path="/" + name).geturl()

    async def sql(dsn, statement):
        conn = await asyncpg.connect(dsn)
        try:
            return await conn.fetchval(statement)
        finally:
            await conn.close()

    asyncio.run(sql(change_dsn, f'CREATE DATABASE "{name}"'))
    try:
        monkeypatch.setenv("WHILLY_DATABASE_URL", sibling)
        config = _build_alembic_config(sibling)
        command.upgrade(config, "040_learning_runs")
        command.upgrade(config, "head")
        assert asyncio.run(sql(sibling, "SELECT version_num FROM alembic_version")) == "041_product_change_sets"
        assert asyncio.run(sql(sibling, "SELECT to_regclass('product_change_sets')")) == "product_change_sets"
        assert (
            asyncio.run(
                sql(
                    sibling,
                    'SELECT valid_change_set_evidence(\'{"kind":"gate","outcome":"PASSED","sha":"'
                    + SHA
                    + '","command":[null],"exit_code":0,"details":{}}\'::jsonb)',
                )
            )
            is False
        )
        command.downgrade(config, "040_learning_runs")
        assert asyncio.run(sql(sibling, "SELECT to_regclass('product_change_sets')")) is None
        command.upgrade(config, "head")
        command.upgrade(config, "head")
        assert asyncio.run(sql(sibling, "SELECT count(*) FROM product_change_sets")) == 0
        assert (
            asyncio.run(
                sql(
                    sibling,
                    'SELECT valid_change_set_evidence(\'{"kind":"gate","outcome":"UNAVAILABLE","absence":"probe_unavailable","command":[],"details":{}}\'::jsonb)',
                )
            )
            is True
        )
    finally:
        asyncio.run(sql(change_dsn, f'DROP DATABASE "{name}"'))
