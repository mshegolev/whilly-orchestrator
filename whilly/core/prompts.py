"""Pure prompt construction for Whilly v4.0 agents (PRD FR-1.6, Module structure).

This module belongs to the ``whilly.core`` layer (Hexagonal architecture, PRD
TC-8 / SC-6): no I/O, no networking, no file reading, no cwd manipulation. The
:func:`build_task_prompt` function is deterministic — given the same
:class:`~whilly.core.models.Task` and :class:`~whilly.core.models.Plan` inputs
it always produces the same string. Side effects (writing prompts to disk,
sending them over a transport) belong in adapters/.

Compared with the v3 prompt builder in ``whilly/cli.py`` this version
deliberately drops all references to ``@tasks.json`` / ``@progress.txt``: the
worker transport (TASK-022) carries the task payload over HTTP, so the agent
need not know any host paths. That keeps the prompt portable across local and
remote workers and removes the cwd-magic the v3 loop relied on.
"""

from __future__ import annotations

from whilly.core.models import Plan, Task

PROMISE_MARKER = "<promise>COMPLETE</promise>"


def build_task_prompt(task: Task, plan: Plan) -> str:
    """Construct the agent prompt for ``task`` within ``plan``.

    The returned string:

    * Names the assigned task by ID and pins the agent to it.
    * Includes the task's ``description``, ``acceptance_criteria``, and
      ``test_steps`` verbatim so the agent does not have to fetch them.
    * Surfaces ``priority``, ``dependencies``, and ``prd_requirement`` for
      context — these are part of the domain model and cheap to inline.
    * Demands ``<promise>COMPLETE</promise>`` on success (PRD FR-1.6).

    Pure: no I/O, no globals, no time-dependent values; deterministic for
    deterministic inputs. ``mypy --strict`` clean per PRD NFR-4.
    """
    lines: list[str] = []
    lines.append(f"План: **{plan.name}** (id={plan.id})")
    lines.append(f"Задача: **{task.id}**")
    lines.append(f"Приоритет: {task.priority.value}")
    if task.prd_requirement:
        lines.append(f"PRD requirement: {task.prd_requirement}")
    lines.append("")

    lines.append("## Описание")
    lines.append(task.description if task.description else "(описание не указано)")
    lines.append("")

    if task.dependencies:
        lines.append("## Зависимости (должны быть DONE до старта)")
        for dep in task.dependencies:
            lines.append(f"- {dep}")
        lines.append("")

    if task.acceptance_criteria:
        lines.append("## Acceptance criteria")
        for idx, criterion in enumerate(task.acceptance_criteria, start=1):
            lines.append(f"{idx}. {criterion}")
        lines.append("")

    if task.test_steps:
        lines.append("## Test steps")
        for idx, step in enumerate(task.test_steps, start=1):
            lines.append(f"{idx}. {step}")
        lines.append("")

    if task.key_files:
        lines.append("## Ключевые файлы")
        for path in task.key_files:
            lines.append(f"- {path}")
        lines.append("")

    lines.append("## Правила")
    lines.append(f"- Работай ТОЛЬКО над задачей {task.id}; не трогай другие задачи плана.")
    lines.append("- Закрой все acceptance criteria и пройди все test steps.")
    lines.append(f"- На финише, ТОЛЬКО при полном успехе, выведи `{PROMISE_MARKER}`.")
    lines.append("- Если не можешь завершить — опиши проблему и НЕ выводи promise-маркер.")

    return "\n".join(lines)


__all__ = ["PROMISE_MARKER", "build_task_prompt"]
