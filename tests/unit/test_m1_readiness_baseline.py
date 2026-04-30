"""Unit tests for the M1 readiness-baseline fixtures (mission v5.0).

These tests pin the *shape* of the baseline artifacts written by
``scripts/m1_baseline_fixtures.py``:

* ``tests/fixtures/v3_tasks.json`` — pre-key_files plan.
* ``tests/fixtures/v4_tasks.json`` — v4.0-era plan with key_files +
  dependencies + plan_id.
* ``tests/fixtures/baselines/events_payload_v4.3.1.json`` — JSON-Schema
  baseline for the v4.3.1 ``events.payload`` jsonb column.
* ``tests/fixtures/whilly_state-v4.3.json`` — frozen state-store snapshot
  with the v4.3.x field set.

They also exercise :func:`tests.conftest.load_fixture` so the helper's
contract is locked in.
"""

from __future__ import annotations

import pytest

from tests.conftest import FIXTURES_DIR, load_fixture


def test_load_fixture_returns_parsed_json_for_json_files() -> None:
    """``.json`` fixtures are returned already parsed (dict / list)."""
    data = load_fixture("v3_tasks.json")
    assert isinstance(data, dict)
    assert "tasks" in data and isinstance(data["tasks"], list)


def test_load_fixture_supports_subdirectory_paths() -> None:
    """Names may include sub-paths under ``tests/fixtures/``."""
    data = load_fixture("baselines/events_payload_v4.3.1.json")
    assert isinstance(data, dict)
    assert data.get("version") == "4.3.1"


def test_load_fixture_raises_for_missing_file() -> None:
    """Missing fixtures raise :class:`FileNotFoundError` with a clear path."""
    with pytest.raises(FileNotFoundError):
        load_fixture("does-not-exist.json")


def test_load_fixture_returns_text_for_non_json_files(tmp_path) -> None:
    """Non-``.json`` files are returned as a UTF-8 string.

    Uses a temp file copied into FIXTURES_DIR via monkeypatching is
    overkill; instead we pick an existing markdown asset under
    ``docs/distributed-audit/`` via the public copy path, but since we
    don't want to depend on doc names, this test creates a one-off
    fixture under the existing ``tests/fixtures/`` tree and removes it.
    """
    target = FIXTURES_DIR / "_unit_smoke.txt"
    target.write_text("hello\n", encoding="utf-8")
    try:
        text = load_fixture("_unit_smoke.txt")
    finally:
        target.unlink(missing_ok=True)
    assert isinstance(text, str)
    assert text == "hello\n"


# ─── v3 tasks fixture ────────────────────────────────────────────────────


def test_v3_tasks_fixture_has_no_key_files_per_task() -> None:
    """v3-era plans must not carry the ``key_files`` field on tasks."""
    data = load_fixture("v3_tasks.json")
    assert all("key_files" not in t for t in data["tasks"])


def test_v3_tasks_fixture_has_legacy_required_task_fields() -> None:
    """v3 tasks still carry id/status/priority/dependencies."""
    expected = {"id", "phase", "category", "priority", "description", "status", "dependencies"}
    for task in load_fixture("v3_tasks.json")["tasks"]:
        assert expected.issubset(task.keys())


# ─── v4 tasks fixture ────────────────────────────────────────────────────


def test_v4_tasks_fixture_has_plan_id() -> None:
    """v4.0-era plans gained a top-level ``plan_id`` field."""
    data = load_fixture("v4_tasks.json")
    assert isinstance(data.get("plan_id"), str) and data["plan_id"]


def test_v4_tasks_fixture_has_key_files_and_dependencies_per_task() -> None:
    """v4 tasks must carry both key_files and dependencies."""
    for task in load_fixture("v4_tasks.json")["tasks"]:
        assert "key_files" in task and isinstance(task["key_files"], list)
        assert "dependencies" in task and isinstance(task["dependencies"], list)


def test_v4_tasks_fixture_has_at_least_one_dependent_task() -> None:
    """At least one task must depend on another to exercise the planner."""
    deps_present = [t for t in load_fixture("v4_tasks.json")["tasks"] if t["dependencies"]]
    assert deps_present, "v4 fixture must include at least one dependent task"


# ─── events.payload baseline ─────────────────────────────────────────────


def test_events_payload_baseline_pins_v4_3_1_event_types() -> None:
    """Baseline must enumerate all canonical v4.3.1 event_type entries."""
    data = load_fixture("baselines/events_payload_v4.3.1.json")
    types = set(data["event_types"].keys())
    expected_subset = {
        "CLAIM",
        "COMPLETE",
        "FAIL",
        "RELEASE",
        "RESET",
        "task.created",
        "task.skipped",
        "plan.applied",
        "plan.budget_exceeded",
        "triz.contradiction",
        "triz.error",
    }
    assert expected_subset.issubset(types), f"missing event_types: {expected_subset - types}"


def test_events_payload_baseline_each_entry_has_object_schema() -> None:
    """Every event_type entry must be an object-typed JSON-Schema fragment."""
    data = load_fixture("baselines/events_payload_v4.3.1.json")
    for name, schema in data["event_types"].items():
        assert schema.get("type") == "object", f"{name}: type != object"


def test_events_payload_baseline_release_reasons_pinned() -> None:
    """RELEASE.reason enum must include the canonical v4.3.1 reasons."""
    data = load_fixture("baselines/events_payload_v4.3.1.json")
    release = data["event_types"]["RELEASE"]
    enum = release["properties"]["reason"]["enum"]
    assert {"visibility_timeout", "worker_offline"}.issubset(enum)


# ─── whilly_state-v4.3.json snapshot ─────────────────────────────────────


def test_whilly_state_snapshot_has_v4_3_field_set() -> None:
    """Frozen state snapshot must carry every field the StateStore writes."""
    data = load_fixture("whilly_state-v4.3.json")
    expected = {
        "plan_file",
        "iteration",
        "cost_usd",
        "active_agents",
        "task_status",
        "paused",
        "pause_reason",
        "paused_at",
        "saved_at",
    }
    assert expected.issubset(data.keys()), f"missing: {expected - data.keys()}"


def test_whilly_state_snapshot_active_agents_have_session_name() -> None:
    """Active-agents entries need session_name + task_id (v4.3 contract)."""
    data = load_fixture("whilly_state-v4.3.json")
    for agent in data["active_agents"]:
        assert {"task_id", "session_name"}.issubset(agent.keys())


def test_whilly_state_snapshot_round_trips_through_state_store() -> None:
    """The snapshot must load cleanly through :class:`whilly.state_store.StateStore`."""
    import json
    import time

    from whilly.state_store import StateStore

    data = load_fixture("whilly_state-v4.3.json")

    # The on-disk snapshot is stale by design; bump saved_at to "now" so
    # StateStore.load doesn't reject it as >24h old.
    data["saved_at"] = time.time()

    store = StateStore(state_file=str(FIXTURES_DIR / "_unit_smoke_state.json"))
    try:
        store.state_file.write_text(json.dumps(data), encoding="utf-8")
        loaded = store.load()
        assert loaded is not None
        assert loaded["iteration"] == data["iteration"]
        assert loaded["task_status"] == data["task_status"]
        assert loaded["active_agents"] == data["active_agents"]
    finally:
        store.clear()


# ─── docs/distributed-audit mirror ───────────────────────────────────────


def test_distributed_audit_docs_mirror_planning_directory() -> None:
    """``docs/distributed-audit/`` must mirror ``.planning/distributed-audit/``."""
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[2]
    src = repo_root / ".planning" / "distributed-audit"
    dst = repo_root / "docs" / "distributed-audit"
    assert dst.is_dir(), f"missing docs mirror: {dst}"
    src_names = {p.name for p in src.iterdir() if p.is_file()}
    dst_names = {p.name for p in dst.iterdir() if p.is_file()}
    assert src_names == dst_names, f"only-in-src: {src_names - dst_names}, only-in-dst: {dst_names - src_names}"
    for name in src_names:
        assert (src / name).read_bytes() == (dst / name).read_bytes(), f"drift: {name}"
