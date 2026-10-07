"""Structured swarm plan contract and validation.

A plan is the JSON object the planner agent must emit (inside a fenced
```` ```json ```` block) before any work can be queued::

    {
      "summary": "What this revision achieves",
      "tasks": [
        {
          "id": "lib-change",
          "project": "demo-lib",
          "role": "implementer",
          "description": "What to do and why",
          "depends_on": [],
          "verification": [["python", "-m", "pytest", "-q"]],
          "acceptance": ["observable acceptance criterion"]
        }
      ]
    }

Validation is all-or-nothing: :func:`validate_plan` either returns a
normalised :class:`SwarmPlan` or raises :class:`PlanError` listing every
problem. Callers persist invalid plans as ``invalid`` revisions with the
error text and never insert any task rows for them.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from whilly.swarm.registry import ID_RE, Registry, find_cycle, parse_argv_list

__all__ = ["PlanError", "PlanTask", "SwarmPlan", "extract_plan_json", "validate_plan"]

_TASK_KEYS = {"id", "project", "role", "engine", "description", "depends_on", "verification", "acceptance"}
_MAX_DESCRIPTION = 8000
_MAX_ACCEPTANCE = 20
_FENCE_RE = re.compile(r"```([^`\r\n]*)\r?\n(.*?)\r?\n```", re.DOTALL)


class PlanError(ValueError):
    """Raised when a proposed plan violates the contract."""

    def __init__(self, problems: list[str]) -> None:
        self.problems = list(problems)
        super().__init__("invalid swarm plan: " + "; ".join(problems))


@dataclass(frozen=True)
class PlanTask:
    id: str
    project: str
    role: str
    engine: str | None
    description: str
    depends_on: tuple[str, ...]
    verification: tuple[tuple[str, ...], ...]
    acceptance: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "project": self.project,
            "role": self.role,
            **({"engine": self.engine} if self.engine else {}),
            "description": self.description,
            "depends_on": list(self.depends_on),
            "verification": [list(cmd) for cmd in self.verification],
            "acceptance": list(self.acceptance),
        }


@dataclass(frozen=True)
class SwarmPlan:
    summary: str
    tasks: tuple[PlanTask, ...]

    def to_dict(self) -> dict[str, Any]:
        return {"summary": self.summary, "tasks": [task.to_dict() for task in self.tasks]}

    def topological_ids(self) -> list[str]:
        order: list[str] = []
        done: set[str] = set()
        by_id = {task.id: task for task in self.tasks}

        def visit(task_id: str) -> None:
            if task_id in done:
                return
            for dep in by_id[task_id].depends_on:
                visit(dep)
            done.add(task_id)
            order.append(task_id)

        for task in self.tasks:
            visit(task.id)
        return order


def extract_plan_json(text: str) -> Any | None:
    """Return the decoded plan object from agent text, or ``None`` if absent.

    Looks at fenced code blocks (last one containing a ``tasks`` key wins),
    then at the whole text. Raises :class:`PlanError` if a candidate block
    mentions ``tasks`` but is not valid JSON, so malformed output is named
    rather than silently treated as discussion.
    """
    # Consume every labelled fence first; otherwise an ignored artifact's
    # closing delimiter can be mistaken for an unlabelled opening fence.
    candidates = [body for language, body in _FENCE_RE.findall(text) if language.strip().lower() in {"", "json"}]
    stripped = text.strip()
    if stripped.startswith("{"):
        candidates.append(stripped)
    decode_errors: list[str] = []
    found: Any | None = None
    for block in candidates:
        if '"tasks"' not in block:
            continue
        try:
            found = json.loads(block)
        except json.JSONDecodeError as exc:
            decode_errors.append(f"plan block is not valid JSON: {exc.msg} at line {exc.lineno}")
    if found is None and decode_errors:
        raise PlanError(decode_errors)
    return found


def validate_plan(data: Any, registry: Registry) -> SwarmPlan:
    """Validate ``data`` against the registry; see module docstring."""
    problems: list[str] = []
    if not isinstance(data, dict):
        raise PlanError(["plan must be a JSON object"])
    unknown = set(data) - {"summary", "tasks"}
    if unknown:
        problems.append(f"unknown plan keys: {sorted(unknown)}")
    summary = data.get("summary", "")
    if not isinstance(summary, str):
        problems.append("summary must be a string")
        summary = ""
    raw_tasks = data.get("tasks")
    if not isinstance(raw_tasks, list) or not raw_tasks:
        raise PlanError(problems + ["'tasks' must be a non-empty list"])
    if len(raw_tasks) > registry.limits.max_tasks:
        problems.append(f"too many tasks ({len(raw_tasks)} > limits.max_tasks={registry.limits.max_tasks})")

    tasks: list[PlanTask] = []
    seen: set[str] = set()
    for index, raw in enumerate(raw_tasks):
        where = f"tasks[{index}]"
        if not isinstance(raw, dict):
            problems.append(f"{where} must be an object")
            continue
        unknown = set(raw) - _TASK_KEYS
        if unknown:
            problems.append(f"{where}: unknown keys {sorted(unknown)}")
        task_id = raw.get("id")
        if not isinstance(task_id, str) or not ID_RE.match(task_id):
            problems.append(f"{where}.id {task_id!r} is invalid (expected {ID_RE.pattern})")
            continue
        where = f"task {task_id!r}"
        if task_id in seen:
            problems.append(f"duplicate task id {task_id!r}")
            continue
        seen.add(task_id)
        project_id = raw.get("project")
        project = registry.projects.get(project_id) if isinstance(project_id, str) else None
        if project is None:
            problems.append(f"{where} references unknown project {project_id!r}")
        role_id = raw.get("role")
        role = registry.roles.get(role_id) if isinstance(role_id, str) else None
        if role is None:
            problems.append(f"{where} references unknown role {role_id!r}")
        elif project is not None and project.id not in role.projects:
            problems.append(f"{where}: role {role.id!r} is not registered for project {project.id!r}")
        engine = raw.get("engine")
        if engine is not None and (not isinstance(engine, str) or engine not in registry.engines):
            problems.append(f"{where}.engine {engine!r} is not configured")
            engine = None
        description = raw.get("description")
        if not isinstance(description, str) or not description.strip():
            problems.append(f"{where}.description must be a non-empty string")
            description = ""
        elif len(description) > _MAX_DESCRIPTION:
            problems.append(f"{where}.description exceeds {_MAX_DESCRIPTION} characters")
        depends_on = raw.get("depends_on", [])
        if not isinstance(depends_on, list) or not all(isinstance(dep, str) for dep in depends_on):
            problems.append(f"{where}.depends_on must be a list of task ids")
            depends_on = []
        if len(set(depends_on)) != len(depends_on):
            problems.append(f"{where}.depends_on contains duplicates")
        verification = parse_argv_list(raw.get("verification", []), f"{where}.verification", problems)
        if (
            project is not None
            and not verification
            and not project.verification
            and project.verification_policy is None
        ):
            problems.append(
                f"{where} has no verification commands and project {project.id!r} defines none; "
                "unverifiable work cannot be accepted"
            )
        acceptance = raw.get("acceptance", [])
        if not isinstance(acceptance, list) or not all(isinstance(item, str) for item in acceptance):
            problems.append(f"{where}.acceptance must be a list of strings")
            acceptance = []
        if len(acceptance) > _MAX_ACCEPTANCE:
            problems.append(f"{where}.acceptance: at most {_MAX_ACCEPTANCE} entries")
        tasks.append(
            PlanTask(
                id=task_id,
                project=project_id if isinstance(project_id, str) else "",
                role=role_id if isinstance(role_id, str) else "",
                engine=engine,
                description=description,
                depends_on=tuple(depends_on),
                verification=verification,
                acceptance=tuple(acceptance),
            )
        )

    ids = {task.id for task in tasks}
    for task in tasks:
        for dep in task.depends_on:
            if dep == task.id:
                problems.append(f"task {task.id!r} depends on itself")
            elif dep not in ids:
                problems.append(f"task {task.id!r} depends on unknown task {dep!r}")
    cycle = find_cycle({task.id: list(task.depends_on) for task in tasks})
    if cycle:
        problems.append("task dependency cycle: " + " -> ".join(cycle))
    if problems:
        raise PlanError(problems)
    return SwarmPlan(summary=summary, tasks=tuple(tasks))
