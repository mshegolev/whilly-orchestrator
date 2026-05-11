"""Pure parser/serialiser for the v4 plan JSON format (PRD FR-2.5, TASK-010a).

This module is the only on-ramp from on-disk JSON into the immutable
:class:`~whilly.core.models.Plan` / :class:`~whilly.core.models.Task` value
objects (and back). It lives in :mod:`whilly.adapters.filesystem` because it
performs file I/O — :mod:`whilly.core` is forbidden by the ``.importlinter``
``core-purity`` contract from doing that itself.

Why split the surface into ``parse_plan`` *and* ``serialize_plan``?
------------------------------------------------------------------
Higher-level callers in TASK-010b (``whilly plan import``) and TASK-010c
(``whilly plan export``) need a *symmetric* contract so the round-trip
``import → export → import`` is idempotent (PRD FR-2.5):

* :func:`parse_plan` reads a v4 JSON file, validates required fields, and
  returns ``(Plan, list[Task])``. The pair is intentional: ``Plan`` already
  carries ``tasks`` as a tuple, but the import path in TASK-010b iterates
  the list separately when it inserts rows into Postgres, so handing both
  out keeps that call site allocation-free.
* :func:`serialize_plan` returns a plain ``dict`` that ``json.dumps`` can
  encode without surprises (no enum members, no tuples, no datetimes — all
  values are strings, ints, or lists thereof). The CLI in TASK-010c writes
  the result straight to stdout.

Validation strategy
-------------------
The parser is *strict on shape*, *forgiving on case*. Required fields
(``id``, ``status``, ``priority``, ``description``) must be present on every
task — a missing field surfaces a :class:`PlanParseError` that names the
offending ``task.id`` so plan authors can find the bad row immediately. The
v3 example fixtures store ``status`` in lowercase (``"pending"``); the v4
:class:`~whilly.core.models.TaskStatus` enum is uppercase (``"PENDING"``).
Rather than reject the v3 corpus we normalise on input — ``status`` is
case-insensitive, ``priority`` already matches.

Optional fields default to their core-model defaults: empty tuples for
collection-typed fields, empty string for ``prd_requirement``, ``0`` for
``version``. Extra JSON keys (``prd_file``, ``agent_instructions``, …) are
silently ignored — those belong to the human-authored layout and are not
part of the canonical v4 plan model. ``serialize_plan`` only emits the
canonical fields, so the round-trip ``parse → serialize → parse`` produces
``==``-equal core models even when the source JSON carried extras.

Plan identity
-------------
The on-disk format may carry an explicit ``plan_id`` string. When it does
not, :func:`parse_plan` falls back to ``project`` so every plan still has
a stable identifier (the import path needs one for idempotent upserts in
TASK-010b). :func:`serialize_plan` always emits ``plan_id`` explicitly so
exports never depend on this fallback — that keeps the round-trip pure.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from whilly.core.models import Plan, PlanOrigin, Priority, RepoTarget, Task, TaskStatus, VerificationCommand
from whilly.core.task_id import validate_task_id

__all__ = ["PlanParseError", "parse_plan", "parse_plan_dict", "serialize_plan"]


# Required keys at the top level of the JSON document. ``plan_id`` is
# intentionally not required — see the module docstring on plan identity.
_REQUIRED_PLAN_FIELDS: tuple[str, ...] = ("project", "tasks")


# Required keys on every task entry. ``id`` is the only field whose absence
# we cannot phrase as "task <id> is missing X" — handled separately below.
_REQUIRED_TASK_FIELDS: tuple[str, ...] = ("status", "priority", "description")


class PlanParseError(ValueError):
    """Raised when the on-disk JSON cannot be parsed into a Plan + Tasks.

    Inherits from :class:`ValueError` so callers that just want to surface
    "bad input" can catch the broader exception, but the dedicated subclass
    lets the CLI in TASK-010b distinguish parse errors (exit 1, validation
    failure) from genuine ``OSError`` (exit 2, file missing) without
    string-matching the message.

    Error messages always include either the task id (for per-task
    failures) or the source path (for top-level failures) so the user can
    locate the offending row in their plan file without re-running the
    parser.
    """


def parse_plan(path: str | os.PathLike[str]) -> tuple[Plan, list[Task]]:
    """Read ``path`` and return ``(Plan, list[Task])`` for that v4 plan.

    Performs the file I/O (UTF-8 read), JSON decode, and shape validation
    in one round-trip. The returned :class:`~whilly.core.models.Plan`
    already contains ``tuple(tasks)`` so the second element is redundant
    for read-only callers — exposing it separately keeps the import path
    in TASK-010b allocation-free when it iterates the tasks once for the
    bulk INSERT.

    Raises:
        PlanParseError: the file cannot be read, is not valid JSON, the
            top-level value is not an object, or any required plan / task
            field is missing or has the wrong shape. The message always
            names either the source path or the offending ``task.id`` so
            plan authors can locate the problem.
    """
    plan_path = Path(path)
    try:
        raw = plan_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise PlanParseError(f"cannot read plan file {plan_path}: {exc}") from exc

    try:
        data: Any = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise PlanParseError(f"plan file {plan_path} is not valid JSON: {exc.msg} (line {exc.lineno})") from exc

    if not isinstance(data, dict):
        raise PlanParseError(f"plan file {plan_path} must contain a JSON object at the top level")

    return _plan_from_dict(data, source=str(plan_path))


def parse_plan_dict(
    payload: dict[str, Any],
    *,
    plan_id: str | None = None,
) -> tuple[Plan, list[Task]]:
    """Validate an in-memory plan ``payload`` and return ``(Plan, list[Task])``.

    Counterpart to :func:`parse_plan` for callers who already hold the
    decoded JSON in memory and don't want to round-trip through the
    filesystem (TASK-104a-2). The canonical caller is
    :mod:`whilly.cli.init`: the PRD wizard hands a freshly-built dict
    from :func:`whilly.prd_generator.generate_tasks_dict` straight into
    this parser, then drives the same ``_insert_plan_and_tasks`` helper
    that ``whilly plan import`` uses, with no ``tasks.json`` ever
    touching disk.

    Args:
        payload: Decoded plan dict — same shape as what :func:`parse_plan`
            reads from a file. Must have ``project`` (str) and ``tasks``
            (list[dict]) at the top level. ``plan_id`` may be present
            either in the dict itself or via the keyword argument; the
            keyword argument wins on conflict (slug ownership lives in
            the CLI per PRD docs/PRD-v41-prd-wizard-port.md FR-3, not in
            the wizard's JSON output).
        plan_id: Optional override that takes precedence over any
            ``plan_id`` key already in ``payload``. ``None`` means "use
            whatever the dict carries", which falls back to ``project``
            via the same path as :func:`parse_plan`.

    Returns:
        Same pair as :func:`parse_plan`: ``(Plan, list[Task])`` where
        the list is the same task tuple unpacked, ready for batched
        INSERT in the CLI's :func:`_insert_plan_and_tasks`.

    Raises:
        PlanParseError: any validation failure. The ``source`` in the
            message is ``"<dict>"`` rather than a file path so the
            operator can tell which surface produced the bad shape.
    """
    if not isinstance(payload, dict):
        raise PlanParseError(f"plan payload must be a dict, got {type(payload).__name__}")
    if plan_id is not None:
        if not isinstance(plan_id, str) or not plan_id:
            raise PlanParseError(f"plan_id override must be a non-empty string, got {plan_id!r}")
        # Override-on-conflict: caller-provided id wins. Mutating a
        # shallow copy keeps the caller's dict untouched even though
        # _plan_from_dict only reads.
        payload = {**payload, "plan_id": plan_id}
    return _plan_from_dict(payload, source="<dict>")


def serialize_plan(plan: Plan, tasks: Iterable[Task]) -> dict[str, Any]:
    """Return a JSON-serialisable ``dict`` for ``plan`` + ``tasks``.

    The output is the canonical v4 plan shape:

    * ``plan_id``: ``plan.id`` — always emitted so round-trips never rely
      on the ``project``-fallback in :func:`parse_plan`.
    * ``project``: ``plan.name``.
    * ``tasks``: a list of task dicts. Enum members are written as their
      string ``.value`` (lowercase for ``Priority``, uppercase for
      ``TaskStatus``) so ``json.dumps`` produces output that round-trips
      back through :func:`parse_plan` without further transformation.

    Tuples in the core model are converted to lists because JSON has no
    distinct tuple type and ``json.dumps`` would otherwise need a custom
    encoder. ``tasks`` is consumed lazily — pass any iterable, including
    ``plan.tasks`` directly.
    """
    out: dict[str, Any] = {
        "plan_id": plan.id,
        "project": plan.name,
        "tasks": [_task_to_dict(task) for task in tasks],
    }
    if plan.origin is not None:
        out["origin"] = _origin_to_dict(plan.origin)
    if plan.repo_targets:
        out["repo_targets"] = [_repo_target_to_dict(target) for target in plan.repo_targets]
    if plan.verification_commands:
        out["verification_commands"] = [
            _verification_command_to_dict(command) for command in plan.verification_commands
        ]
    return out


def _plan_from_dict(data: dict[str, Any], *, source: str) -> tuple[Plan, list[Task]]:
    """Validate the decoded top-level object and return ``(Plan, list[Task])``.

    Pulled out of :func:`parse_plan` so the file-I/O surface is small and the
    validation logic can be unit-tested by feeding it dicts directly should a
    future caller need that (currently we exercise it via ``parse_plan``
    plus :class:`tmp_path` fixtures in :mod:`tests.unit.test_plan_io`).
    """
    for required in _REQUIRED_PLAN_FIELDS:
        if required not in data:
            raise PlanParseError(f"{source}: missing required plan field {required!r}")

    project = data["project"]
    if not isinstance(project, str) or not project:
        raise PlanParseError(f"{source}: 'project' must be a non-empty string")

    raw_tasks = data["tasks"]
    if not isinstance(raw_tasks, list):
        raise PlanParseError(f"{source}: 'tasks' must be a JSON array")

    plan_id_raw = data.get("plan_id", project)
    if not isinstance(plan_id_raw, str) or not plan_id_raw:
        raise PlanParseError(f"{source}: 'plan_id' must be a non-empty string when provided")

    tasks: list[Task] = []
    seen_ids: set[str] = set()
    for index, task_raw in enumerate(raw_tasks):
        task = _task_from_dict(task_raw, index=index, source=source)
        if task.id in seen_ids:
            raise PlanParseError(f"{source}: duplicate task id {task.id!r}")
        seen_ids.add(task.id)
        tasks.append(task)

    origin = _origin_from_dict(data.get("origin"), source=source)
    repo_targets = _repo_targets_from_dict(data.get("repo_targets", ()), source=source)
    verification_commands = _verification_commands_from_dict(data.get("verification_commands"), source=source)
    repo_target_ids = {target.id for target in repo_targets}
    for task in tasks:
        if task.repo_target_id and task.repo_target_id not in repo_target_ids:
            raise PlanParseError(
                f"{source}: task {task.id!r}: repo_target_id {task.repo_target_id!r} "
                "is not declared in top-level 'repo_targets'",
            )

    plan = Plan(
        id=plan_id_raw,
        name=project,
        tasks=tuple(tasks),
        origin=origin,
        repo_targets=repo_targets,
        verification_commands=verification_commands,
    )
    return plan, tasks


def _origin_from_dict(raw: Any, *, source: str) -> PlanOrigin | None:
    """Parse optional top-level ``origin`` provenance metadata."""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise PlanParseError(f"{source}: 'origin' must be a JSON object when provided")

    system = raw.get("system", raw.get("origin_system", raw.get("type", "")))
    ref = raw.get("ref", raw.get("origin_ref", raw.get("external_ref", "")))
    if not isinstance(system, str) or not system:
        raise PlanParseError(f"{source}: 'origin.system' must be a non-empty string")
    if not isinstance(ref, str) or not ref:
        raise PlanParseError(f"{source}: 'origin.ref' must be a non-empty string")

    return PlanOrigin(
        system=system,
        ref=ref,
        url=_optional_string(raw.get("url", raw.get("external_url", "")), source=source, field="origin.url"),
        title=_optional_string(raw.get("title", ""), source=source, field="origin.title"),
        content_hash=_optional_string(raw.get("content_hash", ""), source=source, field="origin.content_hash"),
        prd_file=_optional_string(raw.get("prd_file", ""), source=source, field="origin.prd_file"),
        decomposition_mode=_optional_string(
            raw.get("decomposition_mode", ""),
            source=source,
            field="origin.decomposition_mode",
        ),
    )


def _repo_targets_from_dict(raw: Any, *, source: str) -> tuple[RepoTarget, ...]:
    """Parse optional top-level ``repo_targets`` routing metadata."""
    if raw in (None, ()):
        return ()
    if not isinstance(raw, list):
        raise PlanParseError(f"{source}: 'repo_targets' must be a JSON array when provided")

    targets: list[RepoTarget] = []
    seen: set[str] = set()
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise PlanParseError(f"{source}: repo_targets[{index}] must be a JSON object")
        target_id = item.get("id")
        provider = item.get("provider")
        repo_full_name = item.get("repo_full_name", item.get("repo", ""))
        if not isinstance(target_id, str) or not target_id:
            raise PlanParseError(f"{source}: repo_targets[{index}].id must be a non-empty string")
        if target_id in seen:
            raise PlanParseError(f"{source}: duplicate repo target id {target_id!r}")
        if not isinstance(provider, str) or not provider:
            raise PlanParseError(f"{source}: repo_targets[{index}].provider must be a non-empty string")
        if not isinstance(repo_full_name, str) or not repo_full_name:
            raise PlanParseError(f"{source}: repo_targets[{index}].repo_full_name must be a non-empty string")
        targets.append(
            RepoTarget(
                id=target_id,
                provider=provider,
                repo_full_name=repo_full_name,
                clone_url=_optional_string(
                    item.get("clone_url", ""),
                    source=source,
                    field=f"repo_targets[{index}].clone_url",
                ),
                default_branch=_optional_string(
                    item.get("default_branch", ""),
                    source=source,
                    field=f"repo_targets[{index}].default_branch",
                ),
                credential_policy=_optional_string(
                    item.get("credential_policy", ""),
                    source=source,
                    field=f"repo_targets[{index}].credential_policy",
                ),
            )
        )
        seen.add(target_id)
    return tuple(targets)


def _verification_commands_from_dict(raw: Any, *, source: str) -> tuple[VerificationCommand, ...]:
    """Parse optional top-level ``verification_commands`` metadata."""
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise PlanParseError(f"{source}: 'verification_commands' must be a JSON array when provided")

    commands: list[VerificationCommand] = []
    for index, item in enumerate(raw):
        field = f"verification_commands[{index}]"
        if not isinstance(item, dict):
            raise PlanParseError(f"{source}: {field} must be a JSON object")

        name = item.get("name")
        if not isinstance(name, str) or not name.strip():
            raise PlanParseError(f"{source}: {field}.name must be a non-empty string")

        command = item.get("command")
        if not isinstance(command, str) or not command.strip():
            raise PlanParseError(f"{source}: {field}.command must be a non-empty string")

        required = item.get("required", True)
        if not isinstance(required, bool):
            raise PlanParseError(f"{source}: {field}.required must be a bool when provided")

        command_source = item.get("source", "profile")
        if not isinstance(command_source, str) or not command_source.strip():
            raise PlanParseError(f"{source}: {field}.source must be a non-empty string when provided")

        commands.append(
            VerificationCommand(
                name=name,
                command=command,
                required=required,
                source=command_source,
                repair_max_attempts=_optional_non_negative_int(
                    item.get("repair_max_attempts", 0),
                    source=source,
                    field=f"{field}.repair_max_attempts",
                ),
            )
        )
    return tuple(commands)


def _optional_string(value: Any, *, source: str, field: str) -> str:
    """Validate optional string metadata fields."""
    if value is None:
        return ""
    if not isinstance(value, str):
        raise PlanParseError(f"{source}: {field!r} must be a string when provided")
    return value


def _optional_non_negative_int(value: Any, *, source: str, field: str) -> int:
    """Validate optional non-negative integer metadata fields."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise PlanParseError(f"{source}: {field} must be a non-negative integer when provided")
    return value


def _task_from_dict(raw: Any, *, index: int, source: str) -> Task:
    """Validate one entry from ``data["tasks"]`` and return a :class:`Task`.

    The error path is deliberately verbose: every rejection includes either
    the failing ``task.id`` (when known) or its positional ``index`` (when
    even the id is missing or malformed). This is the contract behind the
    AC for TASK-010a: "missing required field → understandable error
    pointing at task.id".
    """
    if not isinstance(raw, dict):
        raise PlanParseError(f"{source}: task at index {index} is not a JSON object")

    raw_id = raw.get("id")
    if not isinstance(raw_id, str) or not raw_id:
        raise PlanParseError(f"{source}: task at index {index} has missing or empty 'id'")

    try:
        validate_task_id(raw_id)
    except ValueError as exc:
        raise PlanParseError(f"{source}: task at index {index}: {exc}") from exc

    for required in _REQUIRED_TASK_FIELDS:
        if required not in raw:
            raise PlanParseError(f"{source}: task {raw_id!r} missing required field {required!r}")

    description = raw["description"]
    if not isinstance(description, str):
        raise PlanParseError(f"{source}: task {raw_id!r}: 'description' must be a string")

    raw_status = raw["status"]
    if not isinstance(raw_status, str):
        raise PlanParseError(f"{source}: task {raw_id!r}: 'status' must be a string")
    try:
        status = TaskStatus(raw_status.upper())
    except ValueError as exc:
        valid = ", ".join(s.value for s in TaskStatus)
        raise PlanParseError(
            f"{source}: task {raw_id!r}: invalid status {raw_status!r}; expected one of {valid}",
        ) from exc

    raw_priority = raw["priority"]
    if not isinstance(raw_priority, str):
        raise PlanParseError(f"{source}: task {raw_id!r}: 'priority' must be a string")
    try:
        priority = Priority(raw_priority.lower())
    except ValueError as exc:
        valid = ", ".join(p.value for p in Priority)
        raise PlanParseError(
            f"{source}: task {raw_id!r}: invalid priority {raw_priority!r}; expected one of {valid}",
        ) from exc

    dependencies = _coerce_string_tuple(
        raw.get("dependencies", ()), task_id=raw_id, field="dependencies", source=source
    )
    key_files = _coerce_string_tuple(raw.get("key_files", ()), task_id=raw_id, field="key_files", source=source)
    acceptance_criteria = _coerce_string_tuple(
        raw.get("acceptance_criteria", ()), task_id=raw_id, field="acceptance_criteria", source=source
    )
    test_steps = _coerce_string_tuple(raw.get("test_steps", ()), task_id=raw_id, field="test_steps", source=source)

    raw_prd = raw.get("prd_requirement", "")
    if not isinstance(raw_prd, str):
        raise PlanParseError(f"{source}: task {raw_id!r}: 'prd_requirement' must be a string")

    raw_version: Any = raw.get("version", 0)
    # ``isinstance(True, int)`` is True in Python, but a bool here almost
    # certainly means a typo upstream — reject it explicitly so we don't
    # silently coerce ``True`` to ``1``.
    if isinstance(raw_version, bool) or not isinstance(raw_version, int) or raw_version < 0:
        raise PlanParseError(f"{source}: task {raw_id!r}: 'version' must be a non-negative integer")

    repo_target_id = raw.get("repo_target_id", "")
    if not isinstance(repo_target_id, str):
        raise PlanParseError(f"{source}: task {raw_id!r}: 'repo_target_id' must be a string when provided")

    return Task(
        id=raw_id,
        status=status,
        dependencies=dependencies,
        key_files=key_files,
        priority=priority,
        description=description,
        acceptance_criteria=acceptance_criteria,
        test_steps=test_steps,
        prd_requirement=raw_prd,
        version=raw_version,
        repo_target_id=repo_target_id,
    )


def _coerce_string_tuple(value: Any, *, task_id: str, field: str, source: str) -> tuple[str, ...]:
    """Return ``value`` as ``tuple[str, ...]`` or raise :class:`PlanParseError`.

    Accepts JSON arrays (``list``) or pre-built tuples (defensive: callers
    inside this module only ever pass ``raw.get(...)`` whose JSON-decoded
    type is ``list``). Any non-list/tuple value, or a list element that
    isn't a string, surfaces a :class:`PlanParseError` referencing the
    offending ``task_id`` and ``field``.
    """
    if not isinstance(value, (list, tuple)):
        raise PlanParseError(
            f"{source}: task {task_id!r}: {field!r} must be a list of strings, got {type(value).__name__}",
        )
    items: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str):
            raise PlanParseError(
                f"{source}: task {task_id!r}: {field!r}[{index}] must be a string, got {type(item).__name__}",
            )
        items.append(item)
    return tuple(items)


def _origin_to_dict(origin: PlanOrigin) -> dict[str, Any]:
    out = {
        "system": origin.system,
        "ref": origin.ref,
    }
    if origin.url:
        out["url"] = origin.url
    if origin.title:
        out["title"] = origin.title
    if origin.content_hash:
        out["content_hash"] = origin.content_hash
    if origin.prd_file:
        out["prd_file"] = origin.prd_file
    if origin.decomposition_mode:
        out["decomposition_mode"] = origin.decomposition_mode
    return out


def _repo_target_to_dict(target: RepoTarget) -> dict[str, Any]:
    out = {
        "id": target.id,
        "provider": target.provider,
        "repo_full_name": target.repo_full_name,
    }
    if target.clone_url:
        out["clone_url"] = target.clone_url
    if target.default_branch:
        out["default_branch"] = target.default_branch
    if target.credential_policy:
        out["credential_policy"] = target.credential_policy
    return out


def _verification_command_to_dict(command: VerificationCommand) -> dict[str, Any]:
    return {
        "name": command.name,
        "command": command.command,
        "required": command.required,
        "source": command.source,
        "repair_max_attempts": command.repair_max_attempts,
    }


def _task_to_dict(task: Task) -> dict[str, Any]:
    """Render one :class:`Task` as a JSON-serialisable dict.

    Counterpart to :func:`_task_from_dict`. Emits the canonical v4 shape:
    enum values rendered as strings (``status`` uppercase, ``priority``
    lowercase, matching the enum definitions), tuples flattened to lists,
    every field present even when empty so two consecutive serialisations
    of the same plan produce byte-identical JSON.
    """
    out = {
        "id": task.id,
        "status": task.status.value,
        "dependencies": list(task.dependencies),
        "key_files": list(task.key_files),
        "priority": task.priority.value,
        "description": task.description,
        "acceptance_criteria": list(task.acceptance_criteria),
        "test_steps": list(task.test_steps),
        "prd_requirement": task.prd_requirement,
        "version": task.version,
    }
    if task.repo_target_id:
        out["repo_target_id"] = task.repo_target_id
    return out
