"""Unit tests for :mod:`whilly.core.prompts` (TASK-018).

These tests cover the AC for TASK-018:
- ``build_task_prompt`` is pure (deterministic for the same inputs).
- The output mentions ``task.id``, ``description``, every acceptance criterion,
  every test step, and the ``<promise>COMPLETE</promise>`` marker.
- Optional fields (dependencies, key_files, prd_requirement) are inlined when
  present and gracefully omitted when empty.

The tests live in ``tests/unit/`` per the v4 test layout (TASK-006). Pytest's
default rootdir discovery finds them without needing ``__init__.py`` (none of
the existing v3 tests have one either).
"""

from __future__ import annotations

from whilly.core.models import Plan, Priority, Task, TaskStatus
from whilly.core.prompts import PROMISE_MARKER, build_task_prompt


def _make_task(**overrides: object) -> Task:
    base: dict[str, object] = {
        "id": "TASK-018",
        "status": TaskStatus.IN_PROGRESS,
        "priority": Priority.CRITICAL,
        "description": "Создать whilly/core/prompts.py.",
        "acceptance_criteria": ("AC1: pure", "AC2: deterministic"),
        "test_steps": ("pytest tests/unit/test_prompts.py",),
    }
    base.update(overrides)
    return Task(**base)  # type: ignore[arg-type]


def _make_plan(tasks: tuple[Task, ...] = ()) -> Plan:
    return Plan(id="plan-v4", name="Whilly v4 refactor", tasks=tasks)


def test_prompt_includes_task_id_and_plan_metadata() -> None:
    task = _make_task()
    plan = _make_plan(tasks=(task,))

    prompt = build_task_prompt(task, plan)

    assert "TASK-018" in prompt
    assert "Whilly v4 refactor" in prompt
    assert "plan-v4" in prompt
    assert "critical" in prompt  # Priority.CRITICAL.value


def test_prompt_inlines_description_and_acceptance_and_test_steps() -> None:
    task = _make_task()
    plan = _make_plan(tasks=(task,))

    prompt = build_task_prompt(task, plan)

    assert "Создать whilly/core/prompts.py." in prompt
    assert "AC1: pure" in prompt
    assert "AC2: deterministic" in prompt
    assert "pytest tests/unit/test_prompts.py" in prompt


def test_prompt_demands_promise_marker() -> None:
    task = _make_task()
    plan = _make_plan(tasks=(task,))

    prompt = build_task_prompt(task, plan)

    assert PROMISE_MARKER == "<promise>COMPLETE</promise>"
    assert PROMISE_MARKER in prompt


def test_prompt_is_deterministic() -> None:
    task = _make_task()
    plan = _make_plan(tasks=(task,))

    assert build_task_prompt(task, plan) == build_task_prompt(task, plan)


def test_prompt_lists_dependencies_when_present() -> None:
    task = _make_task(dependencies=("TASK-004", "TASK-005"))
    plan = _make_plan(tasks=(task,))

    prompt = build_task_prompt(task, plan)

    assert "Зависимости" in prompt
    assert "TASK-004" in prompt
    assert "TASK-005" in prompt


def test_prompt_omits_dependency_section_when_empty() -> None:
    task = _make_task(dependencies=())
    plan = _make_plan(tasks=(task,))

    prompt = build_task_prompt(task, plan)

    assert "Зависимости" not in prompt


def test_prompt_lists_key_files_when_present() -> None:
    task = _make_task(key_files=("whilly/core/prompts.py",))
    plan = _make_plan(tasks=(task,))

    prompt = build_task_prompt(task, plan)

    assert "whilly/core/prompts.py" in prompt


def test_prompt_handles_empty_optional_fields_gracefully() -> None:
    task = Task(
        id="TASK-X",
        status=TaskStatus.PENDING,
        description="",
        acceptance_criteria=(),
        test_steps=(),
    )
    plan = _make_plan(tasks=(task,))

    prompt = build_task_prompt(task, plan)

    assert "TASK-X" in prompt
    assert "(описание не указано)" in prompt
    assert "Acceptance criteria" not in prompt
    assert "Test steps" not in prompt
    assert PROMISE_MARKER in prompt


def test_prompt_mentions_prd_requirement_when_set() -> None:
    task = _make_task(prd_requirement="FR-1.6")
    plan = _make_plan(tasks=(task,))

    prompt = build_task_prompt(task, plan)

    assert "FR-1.6" in prompt


def test_prompt_pins_agent_to_single_task() -> None:
    """The prompt must instruct the agent to work only on the assigned task."""
    task = _make_task()
    plan = _make_plan(tasks=(task,))

    prompt = build_task_prompt(task, plan)

    # The Russian-language rule from the v3 builder is preserved in v4.
    assert "ТОЛЬКО над задачей TASK-018" in prompt
