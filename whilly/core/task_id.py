"""Task id validator for the M1 hardening pass (VAL-SEC-023..026).

The orchestrator interpolates ``task.id`` into shell wrappers, branch
names, worktree paths, tmux session names, and log file names. Every
loader surface that can mint a :class:`~whilly.core.models.Task` from
external bytes (legacy ``Task.from_dict`` in :mod:`whilly.task_manager`,
v4 plan import in :mod:`whilly.adapters.filesystem.plan_io`, the legacy
``whilly.cli.validate_schema`` shim) routes through
:func:`validate_task_id` here so a malformed id is rejected upfront
instead of being smuggled all the way into ``zsh -ic <wrapper>``.

The accepted shape ``^[A-Za-z0-9._:/-]+$`` is a strict superset of what
:func:`whilly.sinks.github_pr._branch_name` already produces, so any id
that survives the existing branch-name sanitiser also survives this
validator unchanged. ``..`` substrings are rejected separately because
the regex would otherwise admit them via the ``.`` allowance, and
worktree code interpolates the id into filesystem paths.
"""

from __future__ import annotations

import re

__all__ = ["VALID_TASK_ID_RE", "validate_task_id"]


VALID_TASK_ID_RE = re.compile(r"^[A-Za-z0-9._:/-]+$")


def validate_task_id(task_id: object) -> str:
    """Return ``task_id`` if it is a safe, non-traversal task identifier.

    Raises :class:`ValueError` whose message names the offending id when
    the input is not a string, is empty, contains a ``..`` path-traversal
    substring, or contains any character outside ``[A-Za-z0-9._:/-]``.
    The exception type is intentionally :class:`ValueError` so the v4
    plan-import surface (which raises :class:`PlanParseError`, a
    ``ValueError`` subclass) can re-raise without changing semantics.
    """
    if not isinstance(task_id, str):
        raise ValueError(
            f"task id must be a string, got {type(task_id).__name__}",
        )
    if not task_id:
        raise ValueError("task id must be a non-empty string")
    if ".." in task_id:
        raise ValueError(
            f"task id {task_id!r} contains forbidden path-traversal substring '..'",
        )
    if not VALID_TASK_ID_RE.fullmatch(task_id):
        raise ValueError(
            f"task id {task_id!r} contains forbidden characters; must match ^[A-Za-z0-9._:/-]+$",
        )
    return task_id
