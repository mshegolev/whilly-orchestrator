"""PostgreSQL integration checks for durable product feature CAS semantics."""

import asyncio

import pytest

from tests.integration.test_swarm_runtime import db_pool, ecosystem, swarm_dsn  # noqa: F401
from whilly.swarm.product import ProductService

pytestmark = pytest.mark.integration


async def test_product_prepare_approval_stale_race_and_schema(db_pool, ecosystem):  # noqa: F811
    service = ProductService(db_pool, str(ecosystem["registry"]))
    await db_pool.execute(
        "TRUNCATE swarm_product_specs, swarm_product_features, swarm_product_messages, swarm_products CASCADE"
    )
    feature = await service.create_feature("demo", "measure")
    prepared = await service.prepare(
        feature["id"],
        spec={"goal": "x"},
        plan_revision=1,
        base_shas={"demo-lib": "abc"},
        profiles={"planner": "strong", "worker": "cheap"},
        budget={"max_calls": 2, "max_elapsed_seconds": 60},
    )
    assert prepared["status"] == "planned"
    digest = prepared["approval_digest"]
    first, second = await asyncio.gather(
        service.approve(feature["id"], revision=1, digest=digest),
        service.approve(feature["id"], revision=1, digest=digest),
        return_exceptions=True,
    )
    assert sum(not isinstance(item, Exception) for item in (first, second)) == 1
    assert (await service.begin_run(feature["id"], digest))["status"] == "running"
    assert await db_pool.fetchval("SELECT to_regclass('swarm_product_specs')") == "swarm_product_specs"


async def test_binding_history_and_concurrent_plan_input_change(db_pool, ecosystem):  # noqa: F811
    import json

    service = ProductService(db_pool, str(ecosystem["registry"]))
    feature = await service.create_feature("demo", "preserve scope")
    spec = await service.prepare(
        feature["id"],
        spec="first",
        plan_revision=1,
        base_shas={"demo-lib": "abc"},
        profiles={"worker": "cheap"},
        budget={"max_calls": 2, "max_elapsed_seconds": 60},
        expected_revision=0,
    )
    await service.set_budget(feature["id"], {"max_calls": 3, "max_elapsed_seconds": 60})
    with pytest.raises(ValueError, match="stale"):
        await service.prepare(
            feature["id"],
            spec="stale model reply",
            plan_revision=2,
            base_shas={},
            profiles={},
            budget={"max_calls": 2, "max_elapsed_seconds": 60},
            expected_revision=spec["revision"],
        )
    binding = await db_pool.fetchval(
        "SELECT binding FROM swarm_product_specs WHERE feature_id=$1 AND revision=1", feature["id"]
    )
    if isinstance(binding, str):
        binding = json.loads(binding)
    assert binding["budget"]["max_calls"] == 2
    assert binding["spec"] == "first"


async def test_stop_revokes_approval_before_late_finish(db_pool, ecosystem):  # noqa: F811
    service = ProductService(db_pool, str(ecosystem["registry"]))
    feature = await service.create_feature("demo", "stop race")
    feature = await service.prepare(
        feature["id"],
        spec="test",
        plan_revision=1,
        base_shas={},
        profiles={},
        budget={"max_calls": 3, "max_elapsed_seconds": 60},
    )
    await service.approve(feature["id"], revision=feature["revision"], digest=feature["approval_digest"])
    await service.begin_run(feature["id"], feature["approval_digest"])
    await service.invalidate(feature["id"], "stop_requested")
    assert await service.store.finish(feature["id"], "mr_ready") is None
    assert (await service.get_feature(feature["id"]))["status"] == "blocked"


async def test_old_publication_digest_cannot_finish_new_binding(db_pool, ecosystem):  # noqa: F811
    from whilly.swarm.product_workflow import WorkflowBlocked, require_feature_permission

    service = ProductService(db_pool, str(ecosystem["registry"]))
    feature = await service.create_feature("demo", "digest race")
    arguments = dict(plan_revision=1, base_shas={}, profiles={}, budget={"max_calls": 3, "max_elapsed_seconds": 60})
    first = await service.prepare(feature["id"], spec="first", **arguments)
    await service.approve(feature["id"], revision=first["revision"], digest=first["approval_digest"])
    newer = await service.prepare(feature["id"], spec="newer", **arguments)
    await service.approve(feature["id"], revision=newer["revision"], digest=newer["approval_digest"])
    await service.begin_run(feature["id"], newer["approval_digest"])
    with pytest.raises(WorkflowBlocked, match="binding_changed"):
        await require_feature_permission(
            db_pool, feature["session_id"], action="publish", revision=1, expected_digest=first["approval_digest"]
        )
    assert await service.store.finish(feature["id"], "mr_ready", expected_digest=first["approval_digest"]) is None
    assert (await service.get_feature(feature["id"]))["status"] == "running"
