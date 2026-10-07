"""Disposable-Postgres round trips for Task5 host verification bindings."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.integration.test_swarm_runtime import db_pool, swarm_dsn  # noqa: F401
from tests.swarm_helpers import git, make_repo, write_registry
from whilly.swarm.product import ProductService
from whilly.swarm.runtime import SwarmService

pytestmark = pytest.mark.integration

POLICY = {
    "test": [["python", "-m", "pytest", "--junitxml", "reports/test.xml"]],
    "lint": [["ruff", "check", "--output-format", "json", "."]],
    "architecture": [["python", "architecture.py", "--json"]],
    "protected_paths": [],
    "toolchain_id": "fixture-toolchain",
}


@pytest.fixture
def verification_registry(tmp_path: Path) -> Path:
    repo = make_repo(tmp_path, "verified", files={"README.md": "fixture\n"})
    return write_registry(
        tmp_path / "registry.json",
        {
            "verified": {
                "path": str(repo),
                "purpose": "Disposable verification fixture",
                "verification_policy": POLICY,
            }
        },
        state_dir=tmp_path / "state",
    )


async def test_registry_snapshot_and_product_binding_round_trip(db_pool, verification_registry) -> None:  # noqa: F811
    service = SwarmService(db_pool)
    session_id = await service.create_session(str(verification_registry), title="verification")
    revision, status, error = await service.propose_plan(
        session_id,
        {
            "summary": "policy-backed task",
            "tasks": [
                {
                    "id": "change",
                    "project": "verified",
                    "role": "implementer",
                    "description": "exercise binding persistence",
                }
            ],
        },
    )
    assert status == "proposed", error
    await service.apply_revision(session_id, revision)

    revision_row = await service.store.get_revision(session_id, revision)
    snapshot = revision_row["registry_snapshot"]
    binding = snapshot["verification_bindings"]["verified"]
    assert binding["base_sha"] == git(Path(snapshot["projects"]["verified"]["path"]), "rev-parse", "HEAD").strip()
    assert len(binding["policy_digest"]) == 64 and len(binding["hook_digest"]) == 64
    assert (await service.applied_registry(session_id)).projects["verified"].verification_policy is not None

    products = ProductService(db_pool, str(verification_registry))
    feature = await products.create_feature("bound", "persist host binding")
    base_sha = binding["base_sha"]
    prepared = await products.prepare(
        feature["id"],
        spec={"document": "host-owned", "execution_binding": {"worker": "must be replaced"}},
        plan_revision=revision,
        base_shas={"verified": base_sha},
        profiles={},
        budget={"max_calls": 1, "max_elapsed_seconds": 60},
        expected_revision=feature["revision"],
    )
    stored_binding = prepared["spec"]["execution_binding"]["verified"]
    assert stored_binding == binding
    assert prepared["spec"]["execution_binding"]["_status"] == "ready"
    assert prepared["spec"]["execution_binding"] != {"worker": "must be replaced"}
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT binding FROM swarm_product_specs WHERE feature_id=$1 AND revision=$2",
            feature["id"],
            prepared["revision"],
        )
    stored = json.loads(row["binding"] if isinstance(row["binding"], str) else json.dumps(row["binding"]))
    assert stored["execution_binding"]["verified"] == binding
    assert stored["execution_binding"]["_status"] == "ready"


async def test_planning_persists_unavailable_binding_but_apply_blocks(db_pool, tmp_path: Path) -> None:  # noqa: F811
    repo = make_repo(tmp_path, "legacy", files={"README.md": "fixture\n"})
    registry_path = write_registry(
        tmp_path / "legacy-registry.json",
        {"legacy": {"path": str(repo), "purpose": "Legacy fixture", "verification": [["true"]]}},
        state_dir=tmp_path / "state",
    )
    products = ProductService(db_pool, str(registry_path))
    feature = await products.create_feature("unavailable", "retain planning output")
    prepared = await products.prepare(
        feature["id"],
        spec={"document": "human-readable plan", "execution_binding": {"fake": "discard"}},
        plan_revision=1,
        base_shas={"legacy": git(repo, "rev-parse", "HEAD").strip()},
        profiles={},
        budget={"max_calls": 1, "max_elapsed_seconds": 60},
        expected_revision=feature["revision"],
    )
    assert prepared["status"] == "planned"
    assert prepared["spec"]["document"] == "human-readable plan"
    assert prepared["spec"]["execution_binding"]["_status"] == "unavailable"
    approved = await products.approve(feature["id"], revision=prepared["revision"], digest=prepared["approval_digest"])
    from whilly.swarm.product_workflow import WorkflowBlocked, require_feature_permission

    with pytest.raises(WorkflowBlocked, match="verification_not_ready"):
        await require_feature_permission(
            db_pool, approved["session_id"], action="apply", revision=prepared["plan_revision"]
        )


async def test_apply_binds_only_projects_referenced_by_plan(db_pool, tmp_path: Path) -> None:  # noqa: F811
    enrolled = make_repo(tmp_path, "enrolled", files={"README.md": "fixture\n"})
    unrelated = make_repo(tmp_path, "unrelated", files={"README.md": "fixture\n"})
    registry_path = write_registry(
        tmp_path / "multi-registry.json",
        {
            "enrolled": {"path": str(enrolled), "purpose": "Enrolled", "verification_policy": POLICY},
            "unrelated": {"path": str(unrelated), "purpose": "Unrelated", "verification": [["true"]]},
        },
        state_dir=tmp_path / "state",
    )
    service = SwarmService(db_pool)
    session_id = await service.create_session(str(registry_path), title="project set")
    revision, status, error = await service.propose_plan(
        session_id,
        {
            "summary": "one project",
            "tasks": [{"id": "task", "project": "enrolled", "role": "implementer", "description": "d"}],
        },
    )
    assert status == "proposed", error
    await service.apply_revision(session_id, revision)
    snapshot = (await service.store.get_revision(session_id, revision))["registry_snapshot"]
    assert set(snapshot["verification_bindings"]) == {"enrolled"}


async def test_plain_session_rechecks_registry_base_and_policy_before_admission(db_pool, verification_registry) -> None:  # noqa: F811
    from whilly.swarm.product_workflow import WorkflowBlocked, require_feature_permission

    service = SwarmService(db_pool)
    session_id = await service.create_session(str(verification_registry), title="plain")
    revision, status, error = await service.propose_plan(
        session_id,
        {
            "summary": "plain",
            "tasks": [{"id": "task", "project": "verified", "role": "implementer", "description": "d"}],
        },
    )
    assert status == "proposed", error
    await service.apply_revision(session_id, revision)
    verification_registry.write_text(
        verification_registry.read_text().replace("fixture-toolchain", "changed-toolchain")
    )
    with pytest.raises(WorkflowBlocked, match="registry_changed"):
        await require_feature_permission(db_pool, session_id, action="call", revision=revision)


async def test_plain_session_rechecks_base_and_hook_before_admission(db_pool, verification_registry) -> None:  # noqa: F811
    from whilly.swarm.product_workflow import WorkflowBlocked, require_feature_permission

    service = SwarmService(db_pool)
    session_id = await service.create_session(str(verification_registry), title="plain mutable")
    revision, status, error = await service.propose_plan(
        session_id,
        {
            "summary": "plain",
            "tasks": [{"id": "task", "project": "verified", "role": "implementer", "description": "d"}],
        },
    )
    assert status == "proposed", error
    await service.apply_revision(session_id, revision)
    repo = Path(json.loads(verification_registry.read_text())["projects"]["verified"]["path"])
    (repo / "later.txt").write_text("later\n")
    git(repo, "add", "later.txt")
    git(repo, "commit", "-qm", "later")
    with pytest.raises(WorkflowBlocked, match="verification_binding_changed|feature_base_changed"):
        await require_feature_permission(db_pool, session_id, action="call", revision=revision)


async def test_plain_session_rechecks_hook_before_admission(db_pool, verification_registry) -> None:  # noqa: F811
    from whilly.swarm.product_workflow import WorkflowBlocked, require_feature_permission

    service = SwarmService(db_pool)
    session_id = await service.create_session(str(verification_registry), title="plain hook")
    revision, status, error = await service.propose_plan(
        session_id,
        {
            "summary": "plain",
            "tasks": [{"id": "task", "project": "verified", "role": "implementer", "description": "d"}],
        },
    )
    assert status == "proposed", error
    await service.apply_revision(session_id, revision)
    repo = Path(json.loads(verification_registry.read_text())["projects"]["verified"]["path"])
    hook = repo / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\nexit 0\n")
    hook.chmod(0o755)
    with pytest.raises(WorkflowBlocked, match="hook_policy_required"):
        await require_feature_permission(db_pool, session_id, action="call", revision=revision)


async def test_product_permission_rechecks_hook(db_pool, verification_registry) -> None:  # noqa: F811
    from whilly.swarm.product_workflow import WorkflowBlocked, require_feature_permission

    products = ProductService(db_pool, str(verification_registry))
    feature = await products.create_feature("mutable", "recheck product binding")
    repo = Path(json.loads(verification_registry.read_text())["projects"]["verified"]["path"])
    prepared = await products.prepare(
        feature["id"],
        spec={"document": "plan"},
        plan_revision=1,
        base_shas={"verified": git(repo, "rev-parse", "HEAD").strip()},
        profiles={},
        budget={"max_calls": 1, "max_elapsed_seconds": 60},
        expected_revision=feature["revision"],
    )
    approved = await products.approve(feature["id"], revision=prepared["revision"], digest=prepared["approval_digest"])
    await require_feature_permission(db_pool, approved["session_id"], action="apply", revision=1)
    hook = repo / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\nexit 0\n")
    hook.chmod(0o755)
    with pytest.raises(WorkflowBlocked, match="hook_policy_required"):
        await require_feature_permission(db_pool, approved["session_id"], action="apply", revision=1)


async def test_product_permission_rechecks_registry_and_base(db_pool, verification_registry) -> None:  # noqa: F811
    from whilly.swarm.product_workflow import WorkflowBlocked, require_feature_permission

    products = ProductService(db_pool, str(verification_registry))
    feature = await products.create_feature("registry", "recheck product registry")
    data = json.loads(verification_registry.read_text())
    repo = Path(data["projects"]["verified"]["path"])
    prepared = await products.prepare(
        feature["id"],
        spec={"document": "plan"},
        plan_revision=1,
        base_shas={"verified": git(repo, "rev-parse", "HEAD").strip()},
        profiles={},
        budget={"max_calls": 1, "max_elapsed_seconds": 60},
        expected_revision=feature["revision"],
    )
    approved = await products.approve(feature["id"], revision=prepared["revision"], digest=prepared["approval_digest"])
    verification_registry.write_text(
        verification_registry.read_text().replace("fixture-toolchain", "changed-toolchain")
    )
    with pytest.raises(WorkflowBlocked, match="registry_changed"):
        await require_feature_permission(db_pool, approved["session_id"], action="apply", revision=1)

    # Prepare and approve against the changed registry, then move the base.
    feature = await products.create_feature("base", "recheck product base")
    prepared = await products.prepare(
        feature["id"],
        spec={"document": "plan"},
        plan_revision=1,
        base_shas={"verified": git(repo, "rev-parse", "HEAD").strip()},
        profiles={},
        budget={"max_calls": 1, "max_elapsed_seconds": 60},
        expected_revision=feature["revision"],
    )
    approved = await products.approve(feature["id"], revision=prepared["revision"], digest=prepared["approval_digest"])
    (repo / "base-moved.txt").write_text("moved\n")
    git(repo, "add", "base-moved.txt")
    git(repo, "commit", "-qm", "move base")
    with pytest.raises(WorkflowBlocked, match="feature_base_changed"):
        await require_feature_permission(db_pool, approved["session_id"], action="apply", revision=1)


async def test_old_snapshot_report_remains_readable(db_pool, verification_registry) -> None:  # noqa: F811
    service = SwarmService(db_pool)
    session_id = await service.create_session(str(verification_registry), title="historical")
    revision, status, error = await service.propose_plan(
        session_id,
        {
            "summary": "historical",
            "tasks": [{"id": "task", "project": "verified", "role": "implementer", "description": "d"}],
        },
    )
    assert status == "proposed", error
    await service.apply_revision(session_id, revision)
    async with db_pool.acquire() as conn:
        await conn.execute(
            "UPDATE swarm_plan_revisions SET registry_snapshot=$3::jsonb WHERE session_id=$1 AND revision=$2",
            session_id,
            revision,
            json.dumps({"name": "old snapshot"}),
        )
    report = await service.report(session_id)
    assert report["verification_readiness"]["outcome"] == "unavailable"
