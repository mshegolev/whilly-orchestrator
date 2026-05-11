"""Task id validation across every public load surface (M1 VAL-SEC-023..026).

Covers the v3 ``Task.from_dict`` loader in :mod:`whilly.task_manager`,
the v4 plan-import path in :mod:`whilly.adapters.filesystem.plan_io`,
the legacy ``whilly.cli.validate_schema`` shim, and the shared validator
module :mod:`whilly.core.task_id`. Every malicious shape from
VAL-SEC-023 (shell metacharacters) and VAL-SEC-024 (path traversal)
must be rejected at every surface, while every legitimate id from
VAL-SEC-026 must round-trip unchanged.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from whilly.adapters.filesystem.plan_io import PlanParseError, parse_plan, parse_plan_dict
from whilly.cli import validate_schema
from whilly.core.task_id import validate_task_id
from whilly.task_manager import Task as LegacyTask


_VALID_IDS = [
    "TASK-001",
    "GH-42",
    "JIRA-PROJ-13",
    "epic.subepic/leaf",
    "task-42-rev-1",
    "GH-123-rev-2",
    "a",
    "T_1",
    "0123:abc",
]

_SHELL_META_IDS = [
    'x"; rm -rf $HOME; #',
    "x;y",
    "x$(whoami)",
    "x`id`",
    "x\\y",
    "x y",
    "x\ny",
    "x&y",
    "x|y",
    "x>y",
    "x<y",
    'x"y',
    "x'y",
]

_TRAVERSAL_IDS = [
    "../escape",
    "foo/../bar",
    "..",
    "../../etc/passwd",
    "a/..",
]


# ── shared validator ─────────────────────────────────────────────────────


@pytest.mark.parametrize("task_id", _VALID_IDS)
def test_validate_task_id_accepts_legitimate_shapes(task_id: str) -> None:
    assert validate_task_id(task_id) == task_id


@pytest.mark.parametrize("task_id", _SHELL_META_IDS)
def test_validate_task_id_rejects_shell_metacharacters(task_id: str) -> None:
    with pytest.raises(ValueError) as excinfo:
        validate_task_id(task_id)
    # Error message names the offending id so operators can grep for it.
    assert repr(task_id) in str(excinfo.value)


@pytest.mark.parametrize("task_id", _TRAVERSAL_IDS)
def test_validate_task_id_rejects_path_traversal(task_id: str) -> None:
    with pytest.raises(ValueError) as excinfo:
        validate_task_id(task_id)
    assert repr(task_id) in str(excinfo.value)


def test_validate_task_id_rejects_empty_string() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        validate_task_id("")


def test_validate_task_id_rejects_non_string() -> None:
    with pytest.raises(ValueError, match="must be a string"):
        validate_task_id(42)


# ── legacy Task.from_dict (whilly/task_manager.py) ────────────────────────


def _legacy_task_dict(task_id: str) -> dict:
    return {
        "id": task_id,
        "phase": "P1",
        "category": "func",
        "priority": "high",
        "description": "x",
        "status": "pending",
    }


@pytest.mark.parametrize("task_id", _VALID_IDS)
def test_legacy_from_dict_accepts_legitimate_shapes(task_id: str) -> None:
    task = LegacyTask.from_dict(_legacy_task_dict(task_id))
    assert task.id == task_id


@pytest.mark.parametrize("task_id", _SHELL_META_IDS + _TRAVERSAL_IDS)
def test_legacy_from_dict_rejects_malicious_ids(task_id: str) -> None:
    with pytest.raises(ValueError) as excinfo:
        LegacyTask.from_dict(_legacy_task_dict(task_id))
    assert repr(task_id) in str(excinfo.value)


# ── v4 plan-import (whilly/adapters/filesystem/plan_io.py) ────────────────


def _plan_dict(task_id: str) -> dict:
    return {
        "project": "p",
        "tasks": [
            {
                "id": task_id,
                "status": "PENDING",
                "priority": "high",
                "description": "x",
            }
        ],
    }


@pytest.mark.parametrize("task_id", _VALID_IDS)
def test_parse_plan_dict_accepts_legitimate_shapes(task_id: str) -> None:
    plan, tasks = parse_plan_dict(_plan_dict(task_id))
    assert tasks[0].id == task_id


@pytest.mark.parametrize("task_id", _SHELL_META_IDS + _TRAVERSAL_IDS)
def test_parse_plan_dict_rejects_malicious_ids(task_id: str) -> None:
    with pytest.raises(PlanParseError) as excinfo:
        parse_plan_dict(_plan_dict(task_id))
    assert repr(task_id) in str(excinfo.value)


@pytest.mark.parametrize("task_id", _SHELL_META_IDS + _TRAVERSAL_IDS)
def test_parse_plan_file_rejects_malicious_ids_without_writing(
    tmp_path: Path,
    task_id: str,
) -> None:
    # The plan file lives on disk but the import should reject it before
    # any downstream side effect; we assert no extra files appeared
    # alongside the source — the import never wrote anywhere.
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(_plan_dict(task_id)), encoding="utf-8")

    before = sorted(p.name for p in tmp_path.iterdir())
    with pytest.raises(PlanParseError) as excinfo:
        parse_plan(plan_path)
    after = sorted(p.name for p in tmp_path.iterdir())

    assert repr(task_id) in str(excinfo.value)
    assert before == after, "plan-import wrote new files despite rejection"


# ── legacy whilly.cli.validate_schema shim ────────────────────────────────


@pytest.mark.parametrize("task_id", _VALID_IDS)
def test_cli_validate_schema_accepts_legitimate_shapes(task_id: str) -> None:
    validate_schema(_plan_dict(task_id))


@pytest.mark.parametrize("task_id", _SHELL_META_IDS + _TRAVERSAL_IDS)
def test_cli_validate_schema_rejects_malicious_ids(task_id: str) -> None:
    with pytest.raises(ValueError) as excinfo:
        validate_schema(_plan_dict(task_id))
    assert repr(task_id) in str(excinfo.value)


def test_cli_validate_schema_accepts_path(tmp_path: Path) -> None:
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(_plan_dict("TASK-001")), encoding="utf-8")
    validate_schema(plan_path)


def test_cli_validate_schema_rejects_path_with_malicious_id(tmp_path: Path) -> None:
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(_plan_dict("../escape")), encoding="utf-8")
    with pytest.raises(ValueError, match="../escape"):
        validate_schema(plan_path)
