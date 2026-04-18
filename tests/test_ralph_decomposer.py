"""Tests for ralph.decomposer module (R2-017)."""

import json

import pytest
from ralph.decomposer import _tasks_hash, build_decompose_prompt, needs_decompose
from ralph.task_manager import TaskManager


@pytest.fixture
def plan_file(tmp_path):
    """Create temp plan file."""

    def _make(tasks):
        plan = {"project": "test", "tasks": tasks}
        f = tmp_path / "tasks.json"
        f.write_text(json.dumps(plan))
        return str(f)

    return _make


def test_needs_decompose_false_simple(plan_file):
    """Simple tasks should not need decomposition."""
    path = plan_file(
        [
            {
                "id": "T-1",
                "phase": "P1",
                "category": "func",
                "priority": "high",
                "description": "Simple task",
                "status": "pending",
                "dependencies": [],
                "key_files": [],
                "acceptance_criteria": ["AC1", "AC2"],
                "test_steps": [],
            },
        ]
    )
    tm = TaskManager(path)
    assert needs_decompose(tm) is False


def test_needs_decompose_true_many_ac(plan_file):
    """Task with 6+ AC should trigger decomposition."""
    path = plan_file(
        [
            {
                "id": "T-1",
                "phase": "P1",
                "category": "func",
                "priority": "high",
                "description": "Complex task",
                "status": "pending",
                "dependencies": [],
                "key_files": [],
                "acceptance_criteria": ["AC1", "AC2", "AC3", "AC4", "AC5", "AC6"],
                "test_steps": [],
            },
        ]
    )
    tm = TaskManager(path)
    assert needs_decompose(tm) is True


def test_needs_decompose_true_multiple_and(plan_file):
    """Task with 2+ ' и ' in description should trigger."""
    path = plan_file(
        [
            {
                "id": "T-1",
                "phase": "P1",
                "category": "func",
                "priority": "high",
                "description": "Создать миграцию и применить её и проверить результат",
                "status": "pending",
                "dependencies": [],
                "key_files": [],
                "acceptance_criteria": [],
                "test_steps": [],
            },
        ]
    )
    tm = TaskManager(path)
    assert needs_decompose(tm) is True


def test_needs_decompose_skips_done(plan_file):
    """Done tasks should be ignored."""
    path = plan_file(
        [
            {
                "id": "T-1",
                "phase": "P1",
                "category": "func",
                "priority": "high",
                "description": "Создать и применить и проверить",
                "status": "done",
                "dependencies": [],
                "key_files": [],
                "acceptance_criteria": ["AC"] * 10,
                "test_steps": [],
            },
        ]
    )
    tm = TaskManager(path)
    assert needs_decompose(tm) is False


def test_tasks_hash_deterministic(plan_file):
    """Same tasks should produce same hash."""
    path = plan_file(
        [
            {
                "id": "T-1",
                "phase": "P1",
                "category": "func",
                "priority": "high",
                "description": "Task",
                "status": "pending",
                "dependencies": [],
                "key_files": [],
            },
        ]
    )
    tm = TaskManager(path)
    h1 = _tasks_hash(tm)
    h2 = _tasks_hash(tm)
    assert h1 == h2
    assert len(h1) == 16


def test_build_decompose_prompt():
    """Prompt should reference the tasks file."""
    p = build_decompose_prompt("my_tasks.json")
    assert "@my_tasks.json" in p
    assert "NO_DECOMPOSE" in p
    assert "DECOMPOSED" in p
