"""Unit tests for :mod:`whilly.adapters.filesystem.plan_io` (TASK-010a).

Covers the AC for TASK-010a:

* :func:`parse_plan` reads JSON and returns a ``Plan`` + ``list[Task]`` made of
  core models (no ad-hoc dicts, no I/O leaks).
* :func:`serialize_plan` returns a dict that ``json.dumps`` accepts.
* Missing required fields surface a :class:`PlanParseError` whose message
  references the offending ``task.id`` so plan authors can locate the row.
* Round-trip ``parse → serialize → parse`` reproduces ``==``-equal core models
  even when the input JSON carried extra keys (``prd_file``,
  ``agent_instructions``).
* ``status`` is normalised case-insensitively (v3 fixtures use lowercase
  ``"pending"``; the v4 :class:`TaskStatus` enum is uppercase).

Tests live under ``tests/unit/`` per the v4 layout (TASK-006). They use only
``tmp_path`` for I/O — no testcontainers, no async — so the suite stays fast
and runs on every commit.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from whilly.adapters.filesystem.plan_io import PlanParseError, parse_plan, parse_plan_dict, serialize_plan
from whilly.core.models import Plan, Priority, Task, TaskStatus


def _write_json(tmp_path: Path, payload: dict[str, Any]) -> Path:
    """Write ``payload`` to a fresh ``plan.json`` under ``tmp_path``.

    Helper that keeps the call sites short — most tests just want a
    on-disk JSON file the parser can read; we never need a custom name.
    """
    target = tmp_path / "plan.json"
    target.write_text(json.dumps(payload), encoding="utf-8")
    return target


def _minimal_task_dict(**overrides: Any) -> dict[str, Any]:
    """Build the smallest legal v4 task dict — required fields only.

    Tests that exercise validation start from this baseline and mutate the
    one field they care about, so the noise around each assertion stays
    flat. Optional fields default to their core-model defaults so the
    resulting :class:`Task` has empty tuples / strings / version=0.
    """
    base: dict[str, Any] = {
        "id": "TASK-001",
        "status": "PENDING",
        "priority": "high",
        "description": "Do the thing.",
    }
    base.update(overrides)
    return base


def _minimal_plan_dict(**overrides: Any) -> dict[str, Any]:
    """Build the smallest legal v4 plan dict — required fields only."""
    base: dict[str, Any] = {
        "project": "Workshop A",
        "tasks": [_minimal_task_dict()],
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Happy path: parse, then check the returned core models match what we wrote.
# ---------------------------------------------------------------------------


def test_parse_plan_minimal_returns_core_models(tmp_path: Path) -> None:
    target = _write_json(tmp_path, _minimal_plan_dict())

    plan, tasks = parse_plan(target)

    assert isinstance(plan, Plan)
    assert plan.id == "Workshop A"  # plan_id falls back to project
    assert plan.name == "Workshop A"
    assert len(tasks) == 1
    assert tasks[0] == Task(
        id="TASK-001",
        status=TaskStatus.PENDING,
        priority=Priority.HIGH,
        description="Do the thing.",
    )
    # plan.tasks is a tuple while the second return value is a list — both must
    # carry the same task objects.
    assert plan.tasks == tuple(tasks)


def test_parse_plan_explicit_plan_id_takes_precedence(tmp_path: Path) -> None:
    payload = _minimal_plan_dict(plan_id="plan-abc-001")
    target = _write_json(tmp_path, payload)

    plan, _ = parse_plan(target)

    assert plan.id == "plan-abc-001"
    assert plan.name == "Workshop A"


def test_parse_plan_status_is_case_insensitive(tmp_path: Path) -> None:
    """v3 fixtures store status as lowercase 'pending' — parser must accept it."""
    payload = _minimal_plan_dict(tasks=[_minimal_task_dict(status="pending")])
    target = _write_json(tmp_path, payload)

    _, tasks = parse_plan(target)

    assert tasks[0].status is TaskStatus.PENDING


def test_parse_plan_priority_is_case_insensitive(tmp_path: Path) -> None:
    payload = _minimal_plan_dict(tasks=[_minimal_task_dict(priority="CRITICAL")])
    target = _write_json(tmp_path, payload)

    _, tasks = parse_plan(target)

    assert tasks[0].priority is Priority.CRITICAL


def test_parse_plan_optional_fields_default_to_empty(tmp_path: Path) -> None:
    """Missing optional fields → core-model defaults (empty tuples, version=0)."""
    target = _write_json(tmp_path, _minimal_plan_dict())

    _, tasks = parse_plan(target)
    task = tasks[0]

    assert task.dependencies == ()
    assert task.key_files == ()
    assert task.acceptance_criteria == ()
    assert task.test_steps == ()
    assert task.prd_requirement == ""
    assert task.version == 0


def test_parse_plan_preserves_collection_fields(tmp_path: Path) -> None:
    payload = _minimal_plan_dict(
        tasks=[
            _minimal_task_dict(
                id="TASK-A",
                dependencies=["TASK-B", "TASK-C"],
                key_files=["src/foo.py", "src/bar.py"],
                acceptance_criteria=["AC1", "AC2"],
                test_steps=["step 1", "step 2"],
                prd_requirement="FR-2.5",
                version=3,
            ),
        ],
    )
    target = _write_json(tmp_path, payload)

    _, tasks = parse_plan(target)
    task = tasks[0]

    assert task.dependencies == ("TASK-B", "TASK-C")
    assert task.key_files == ("src/foo.py", "src/bar.py")
    assert task.acceptance_criteria == ("AC1", "AC2")
    assert task.test_steps == ("step 1", "step 2")
    assert task.prd_requirement == "FR-2.5"
    assert task.version == 3


def test_parse_plan_ignores_unknown_top_level_and_task_keys(tmp_path: Path) -> None:
    """v3-era fixtures carry ``prd_file``, ``agent_instructions`` etc. — silently dropped."""
    payload = {
        "project": "Workshop A",
        "plan_id": "plan-ws-a",
        "prd_file": "docs/PRD.md",
        "agent_instructions": {"before": ["read tasks.json"]},
        "tasks": [
            {
                **_minimal_task_dict(),
                "phase": "Phase 1",
                "category": "doc",
            },
        ],
    }
    target = _write_json(tmp_path, payload)

    plan, tasks = parse_plan(target)

    assert plan.id == "plan-ws-a"
    assert tasks[0].id == "TASK-001"


# ---------------------------------------------------------------------------
# Serialisation: returns json.dumps-friendly dict, enums rendered as strings.
# ---------------------------------------------------------------------------


def test_serialize_plan_returns_jsonable_dict() -> None:
    plan = Plan(id="plan-1", name="Workshop A")
    tasks = [
        Task(
            id="TASK-001",
            status=TaskStatus.DONE,
            dependencies=("TASK-000",),
            key_files=("src/x.py",),
            priority=Priority.CRITICAL,
            description="ship it",
            acceptance_criteria=("AC1",),
            test_steps=("pytest",),
            prd_requirement="FR-1",
            version=2,
        ),
    ]

    out = serialize_plan(plan, tasks)

    # json.dumps is the contract — if any value is non-serialisable this raises.
    encoded = json.dumps(out)
    decoded = json.loads(encoded)

    assert decoded["plan_id"] == "plan-1"
    assert decoded["project"] == "Workshop A"
    assert decoded["tasks"][0]["id"] == "TASK-001"
    assert decoded["tasks"][0]["status"] == "DONE"  # uppercase enum value
    assert decoded["tasks"][0]["priority"] == "critical"  # lowercase enum value
    assert decoded["tasks"][0]["dependencies"] == ["TASK-000"]
    assert decoded["tasks"][0]["version"] == 2


def test_serialize_plan_emits_plan_id_even_when_equal_to_project() -> None:
    """Round-trip stability: every export includes ``plan_id`` explicitly."""
    plan = Plan(id="Workshop A", name="Workshop A")

    out = serialize_plan(plan, ())

    assert out["plan_id"] == "Workshop A"
    assert out["project"] == "Workshop A"


def test_serialize_then_parse_round_trips(tmp_path: Path) -> None:
    """parse → serialize → write → parse produces ==-equal core models."""
    original_plan = Plan(
        id="plan-rt",
        name="Workshop RoundTrip",
        tasks=(
            Task(
                id="TASK-A",
                status=TaskStatus.PENDING,
                dependencies=(),
                key_files=("src/foo.py",),
                priority=Priority.HIGH,
                description="A",
                acceptance_criteria=("AC1",),
                test_steps=("step",),
                prd_requirement="FR-2.5",
                version=1,
            ),
            Task(
                id="TASK-B",
                status=TaskStatus.IN_PROGRESS,
                dependencies=("TASK-A",),
                priority=Priority.MEDIUM,
                description="B",
            ),
        ),
    )

    target = tmp_path / "plan.json"
    target.write_text(json.dumps(serialize_plan(original_plan, original_plan.tasks)), encoding="utf-8")

    parsed_plan, parsed_tasks = parse_plan(target)

    assert parsed_plan == original_plan
    assert parsed_tasks == list(original_plan.tasks)


# ---------------------------------------------------------------------------
# Validation: missing / wrong-type fields surface PlanParseError with task.id.
# ---------------------------------------------------------------------------


def test_parse_plan_missing_project_field(tmp_path: Path) -> None:
    target = _write_json(tmp_path, {"tasks": []})

    with pytest.raises(PlanParseError, match="missing required plan field 'project'"):
        parse_plan(target)


def test_parse_plan_missing_tasks_field(tmp_path: Path) -> None:
    target = _write_json(tmp_path, {"project": "X"})

    with pytest.raises(PlanParseError, match="missing required plan field 'tasks'"):
        parse_plan(target)


def test_parse_plan_top_level_not_object(tmp_path: Path) -> None:
    target = tmp_path / "plan.json"
    target.write_text(json.dumps([{"project": "X"}]), encoding="utf-8")

    with pytest.raises(PlanParseError, match="must contain a JSON object at the top level"):
        parse_plan(target)


def test_parse_plan_invalid_json(tmp_path: Path) -> None:
    target = tmp_path / "plan.json"
    target.write_text("{not json", encoding="utf-8")

    with pytest.raises(PlanParseError, match="not valid JSON"):
        parse_plan(target)


def test_parse_plan_file_not_found(tmp_path: Path) -> None:
    missing = tmp_path / "does_not_exist.json"

    with pytest.raises(PlanParseError, match="cannot read plan file"):
        parse_plan(missing)


def test_parse_plan_task_missing_id_uses_index_in_error(tmp_path: Path) -> None:
    payload = _minimal_plan_dict(
        tasks=[
            {"status": "PENDING", "priority": "high", "description": "no id"},
        ],
    )
    target = _write_json(tmp_path, payload)

    with pytest.raises(PlanParseError, match="task at index 0 has missing or empty 'id'"):
        parse_plan(target)


@pytest.mark.parametrize("missing_field", ["status", "priority", "description"])
def test_parse_plan_task_missing_required_field_references_task_id(tmp_path: Path, missing_field: str) -> None:
    """AC: missing required field → error names the offending task.id."""
    task = _minimal_task_dict(id="TASK-XYZ")
    del task[missing_field]
    target = _write_json(tmp_path, _minimal_plan_dict(tasks=[task]))

    with pytest.raises(PlanParseError) as excinfo:
        parse_plan(target)

    assert "TASK-XYZ" in str(excinfo.value)
    assert missing_field in str(excinfo.value)


def test_parse_plan_invalid_status_value(tmp_path: Path) -> None:
    target = _write_json(
        tmp_path,
        _minimal_plan_dict(tasks=[_minimal_task_dict(id="TASK-7", status="WAT")]),
    )

    with pytest.raises(PlanParseError) as excinfo:
        parse_plan(target)

    msg = str(excinfo.value)
    assert "TASK-7" in msg
    assert "WAT" in msg
    assert "PENDING" in msg  # listing valid choices


def test_parse_plan_invalid_priority_value(tmp_path: Path) -> None:
    target = _write_json(
        tmp_path,
        _minimal_plan_dict(tasks=[_minimal_task_dict(id="TASK-7", priority="urgent")]),
    )

    with pytest.raises(PlanParseError) as excinfo:
        parse_plan(target)

    msg = str(excinfo.value)
    assert "TASK-7" in msg
    assert "urgent" in msg


def test_parse_plan_dependencies_must_be_list_of_strings(tmp_path: Path) -> None:
    target = _write_json(
        tmp_path,
        _minimal_plan_dict(tasks=[_minimal_task_dict(id="TASK-D", dependencies=["ok", 7])]),
    )

    with pytest.raises(PlanParseError) as excinfo:
        parse_plan(target)

    msg = str(excinfo.value)
    assert "TASK-D" in msg
    assert "dependencies" in msg


def test_parse_plan_dependencies_wrong_type(tmp_path: Path) -> None:
    target = _write_json(
        tmp_path,
        _minimal_plan_dict(tasks=[_minimal_task_dict(id="TASK-D", dependencies="TASK-A")]),
    )

    with pytest.raises(PlanParseError, match="must be a list of strings"):
        parse_plan(target)


def test_parse_plan_version_must_be_nonneg_int(tmp_path: Path) -> None:
    target = _write_json(
        tmp_path,
        _minimal_plan_dict(tasks=[_minimal_task_dict(id="TASK-V", version=-1)]),
    )

    with pytest.raises(PlanParseError, match="non-negative integer"):
        parse_plan(target)


def test_parse_plan_version_rejects_bool(tmp_path: Path) -> None:
    """``True`` is an int subclass — must still be rejected to catch typos."""
    target = _write_json(
        tmp_path,
        _minimal_plan_dict(tasks=[_minimal_task_dict(id="TASK-V", version=True)]),
    )

    with pytest.raises(PlanParseError, match="non-negative integer"):
        parse_plan(target)


def test_parse_plan_duplicate_task_ids_rejected(tmp_path: Path) -> None:
    payload = _minimal_plan_dict(
        tasks=[_minimal_task_dict(id="TASK-DUP"), _minimal_task_dict(id="TASK-DUP")],
    )
    target = _write_json(tmp_path, payload)

    with pytest.raises(PlanParseError, match="duplicate task id 'TASK-DUP'"):
        parse_plan(target)


def test_parse_plan_task_not_object(tmp_path: Path) -> None:
    target = _write_json(tmp_path, {"project": "X", "tasks": ["not an object"]})

    with pytest.raises(PlanParseError, match="task at index 0 is not a JSON object"):
        parse_plan(target)


# ---------------------------------------------------------------------------
# parse_plan_dict — TASK-104a-2: in-memory counterpart for whilly init flow.
# ---------------------------------------------------------------------------


def test_parse_plan_dict_returns_same_shape_as_parse_plan() -> None:
    """Same input, same (Plan, [Task]) regardless of whether it came from disk."""
    payload = _minimal_plan_dict()

    plan, tasks = parse_plan_dict(payload)

    assert isinstance(plan, Plan)
    assert plan.id == "Workshop A"
    assert plan.name == "Workshop A"
    assert len(tasks) == 1
    assert tasks[0] == Task(
        id="TASK-001",
        status=TaskStatus.PENDING,
        priority=Priority.HIGH,
        description="Do the thing.",
    )
    assert plan.tasks == tuple(tasks)


def test_parse_plan_dict_keyword_plan_id_wins_over_payload() -> None:
    """plan_id kwarg overrides whatever the payload carries.

    Slug ownership lives in the CLI per PRD FR-3 — even if the wizard's
    JSON output happens to set plan_id, the caller-provided value must
    take precedence.
    """
    payload = _minimal_plan_dict(plan_id="from-payload")

    plan, _ = parse_plan_dict(payload, plan_id="from-cli")

    assert plan.id == "from-cli"


def test_parse_plan_dict_no_plan_id_falls_back_to_project() -> None:
    """Without an explicit plan_id (kwarg or in payload) — uses project as id.

    Same fallback semantics as parse_plan; tested separately so a future
    divergence between the two surfaces fails this test specifically.
    """
    payload = _minimal_plan_dict()  # no plan_id key

    plan, _ = parse_plan_dict(payload)

    assert plan.id == "Workshop A"


def test_parse_plan_dict_does_not_mutate_caller_payload() -> None:
    """Defensive: caller's dict must come back untouched after override.

    The override path uses {**payload, "plan_id": ...} to apply the kwarg,
    which builds a shallow copy. A naive payload[...]= would mutate the
    caller's dict — this test pins the no-mutation contract.
    """
    payload = _minimal_plan_dict(plan_id="original")
    snapshot = json.loads(json.dumps(payload))  # deep copy via JSON round-trip

    parse_plan_dict(payload, plan_id="overridden")

    assert payload == snapshot


def test_parse_plan_dict_rejects_non_dict_input() -> None:
    """Top-level not a dict → PlanParseError before we look at any field."""
    with pytest.raises(PlanParseError, match="must be a dict"):
        parse_plan_dict("not a dict")  # type: ignore[arg-type]


def test_parse_plan_dict_rejects_empty_plan_id_kwarg() -> None:
    """Empty plan_id override is a programming error — surface it loudly."""
    with pytest.raises(PlanParseError, match="non-empty string"):
        parse_plan_dict(_minimal_plan_dict(), plan_id="")


def test_parse_plan_dict_propagates_validation_errors() -> None:
    """Same validation as parse_plan — missing required field surfaces error."""
    bad = _minimal_plan_dict(tasks=[{"id": "TASK-X", "status": "PENDING"}])

    with pytest.raises(PlanParseError, match="missing required field"):
        parse_plan_dict(bad)


def test_parse_plan_dict_source_label_is_dict_marker() -> None:
    """Error messages use ``<dict>`` instead of a path so operators see surface."""
    bad = {"project": "X"}  # tasks missing entirely

    with pytest.raises(PlanParseError, match=r"<dict>:"):
        parse_plan_dict(bad)
