"""Re-iterate follow-up task spawner for the M2 PR-review feedback loop.

When a ``pr.review.changes_requested`` event is observed for an
existing task ``T`` (mission ``m2-pr-review-feedback``, validation
contract assertions ``VAL-PR-014..017``, ``VAL-PR-025``,
``VAL-PR-027``, ``VAL-CROSS-002``, ``VAL-CROSS-004``,
``VAL-CROSS-006``), the orchestrator spawns a follow-up task whose
id is ``T-rev-{N+1}`` where ``N`` is the number of pre-existing
``T-rev-*`` rows for ``T``. The follow-up depends on ``T`` (so the
DAG keeps the original task as a prerequisite for the rev), inherits
``T``'s ``priority`` and ``key_files``, carries the PR URL in
``prd_requirement``, and stores the SANITIZED concatenation of the
review-comment bodies in its ``description``.

A configurable cap (``WHILLY_MAX_REVIEW_ITERATIONS``, default ``3``)
prevents an unbounded re-iteration loop: when the existing rev count
is already ``>= cap``, no new ``tasks`` row is inserted; instead a
single ``pr.iteration.requested`` event with ``detail.refused=true``
is emitted carrying the SANITIZED comment payload so the audit trail
records the cap firing in a way distinguishable from successful
spawns.

Trust boundaries
----------------
* Every review-comment body received from the
  :mod:`whilly.sources.github_pr_feedback` poller is *untrusted* —
  the poller forwards comment text VERBATIM and the M1 sanitizer
  contract pins this module as the M2 sanitization site
  (``scope='pr_review_comment'``). All comment text reaching this
  module passes through
  :func:`whilly.security.prompt_sanitizer.sanitize_external_text`
  before it is interpolated into a task description, an event
  payload, or any other downstream surface.
* ``orig_task_id`` is validated at module entry against the M1
  task-id regex (``^[A-Za-z0-9._:/-]+$``) BEFORE any database write;
  malformed values raise :class:`ValueError` and produce zero side
  effects (no row insertion, no event emission).

Why a connection-based API rather than ``TaskRepository``?
    The post-COMPLETE PR opener hook caller already owns a
    ``TaskRepository`` and dispatches in its own asynccontextmanager.
    The poller-driven re-iterate path runs after a
    ``pr.review.changes_requested`` event is committed and naturally
    composes with the same ``async with conn.transaction():`` block
    the producer used. Taking ``conn`` directly here keeps the
    follow-up insert + ``pr.iteration.requested`` event emission in
    the same transaction as the change-requested event, so an audit
    reader never sees a follow-up task without the matching audit
    row. ``jsonl_sink`` is opt-in: callers that mirror PG events to
    JSONL pass it; callers that don't (e.g. unit tests asserting on
    Postgres state only) leave it at ``None``.
"""

from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import Iterable, Mapping
from typing import Any

import asyncpg

from whilly.adapters.db.repository import (
    PR_ITERATION_COMPLETED_EVENT_TYPE,
    PR_ITERATION_REQUESTED_EVENT_TYPE,
    TaskRepository,
)
from whilly.audit import JsonlEventSink
from whilly.core.models import PlanId, Priority, Task, TaskId, TaskStatus
from whilly.core.task_id import validate_task_id
from whilly.security.prompt_sanitizer import sanitize_external_text

logger = logging.getLogger(__name__)


#: Environment variable that caps the number of review iterations per
#: originating task. Default ``3``. Setting ``0`` disables iteration
#: entirely — the very first ``pr.review.changes_requested`` for any
#: task spawns zero rev tasks and emits the cap-fired event with
#: ``iteration=0`` (VAL-PR-027).
MAX_REVIEW_ITERATIONS_ENV: str = "WHILLY_MAX_REVIEW_ITERATIONS"

#: Default cap on the number of follow-up rev tasks per originating
#: task. Mirrors the value documented in the M2 mission spec.
DEFAULT_MAX_REVIEW_ITERATIONS: int = 3

#: Sanitizer scope label for the PR-review-comment ingestion site.
#: VAL-CROSS-001 pins this scope; downstream prompt builders re-use
#: the same literal so the open-fence header is queryable.
SANITIZER_SCOPE: str = "pr_review_comment"


_REV_TASK_ID_RE: re.Pattern[str] = re.compile(r"^(?P<orig>.+)-rev-(?P<n>\d+)$")


__all__ = [
    "DEFAULT_MAX_REVIEW_ITERATIONS",
    "MAX_REVIEW_ITERATIONS_ENV",
    "SANITIZER_SCOPE",
    "build_followup_description",
    "emit_iteration_completed",
    "get_max_review_iterations",
    "parse_rev_task_id",
    "spawn_followup",
]


def get_max_review_iterations(env: Mapping[str, str] | None = None) -> int:
    """Return the configured cap, defaulting to :data:`DEFAULT_MAX_REVIEW_ITERATIONS`.

    Parses ``WHILLY_MAX_REVIEW_ITERATIONS`` from ``env`` (or
    :data:`os.environ` when ``env`` is ``None``). Non-integer values
    fall back to the default with a WARNING — operators rarely
    misconfigure this and a hard exit on a typo would be hostile to
    long-running orchestrator processes. Negative values are clamped
    to ``0`` so the boundary semantics in VAL-PR-027 (``=0`` disables
    iteration entirely) extend down to malformed negatives without a
    code path that could spawn a rev task on a "negative cap".
    """
    src: Mapping[str, str] = env if env is not None else os.environ
    raw = src.get(MAX_REVIEW_ITERATIONS_ENV)
    if raw is None or not str(raw).strip():
        return DEFAULT_MAX_REVIEW_ITERATIONS
    try:
        value = int(str(raw).strip())
    except ValueError:
        logger.warning(
            "%s=%r is not a valid integer; falling back to default=%d",
            MAX_REVIEW_ITERATIONS_ENV,
            raw,
            DEFAULT_MAX_REVIEW_ITERATIONS,
        )
        return DEFAULT_MAX_REVIEW_ITERATIONS
    return max(0, value)


def _comment_body_iter(comments: Iterable[Any] | None) -> list[str]:
    if comments is None:
        return []
    bodies: list[str] = []
    for entry in comments:
        if isinstance(entry, Mapping):
            body = entry.get("body")
            if isinstance(body, str):
                bodies.append(body)
            elif body is not None:
                bodies.append(str(body))
        elif isinstance(entry, str):
            bodies.append(entry)
    return bodies


def build_followup_description(comments: Iterable[Any] | None) -> str:
    """Concatenate review-comment bodies and pass through the M1 sanitizer.

    The output is the byte sequence the follow-up task's
    ``description`` column carries: a single
    ``<UNTRUSTED kind=pr_review_comment>...</UNTRUSTED>`` envelope
    around the joined bodies, with secret-pattern redaction applied
    and embedded ``</UNTRUSTED>`` substrings neutralised so the count
    of closing fences in the output is exactly one (VAL-PR-015,
    VAL-CROSS-006).

    Empty ``comments`` (``None`` / empty list / list of non-dict
    entries) returns the canonical empty-fence payload from the
    sanitizer so downstream callers always see the well-formed
    envelope shape — no special casing required.
    """
    bodies = _comment_body_iter(comments)
    joined = "\n\n---\n\n".join(bodies)
    return sanitize_external_text(joined, scope=SANITIZER_SCOPE)


def parse_rev_task_id(task_id: str) -> tuple[str, int] | None:
    """Decompose ``T-rev-N`` into ``(T, N)`` or return ``None``.

    Accepts arbitrarily-nested rev ids (``T-rev-1-rev-1`` parses as
    ``(T-rev-1, 1)``), so the iteration-completed hook can fire on
    the inner rev's COMPLETE without losing the outer rev's id from
    the audit trail. Non-rev task ids return ``None``.
    """
    match = _REV_TASK_ID_RE.match(task_id)
    if match is None:
        return None
    return match.group("orig"), int(match.group("n"))


async def _count_existing_rev_tasks(
    conn: asyncpg.Connection,
    orig_task_id: str,
) -> int:
    pattern = orig_task_id.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "-rev-%"
    count = await conn.fetchval(
        "SELECT COUNT(*) FROM tasks WHERE id LIKE $1 ESCAPE '\\'",
        pattern,
    )
    return int(count or 0)


async def _emit_event_with_conn(
    conn: asyncpg.Connection,
    *,
    event_type: str,
    plan_id: PlanId | None,
    task_id: TaskId | None,
    payload: dict[str, Any],
    jsonl_sink: JsonlEventSink | None,
) -> int:
    """Insert one ``events`` row and best-effort mirror to JSONL.

    Mirrors the
    :meth:`whilly.adapters.db.repository.TaskRepository.emit_pr_event`
    contract (Postgres + JSONL) but uses the caller's ``conn`` so the
    INSERT lands in the same transaction the caller is composing.
    The JSONL mirror runs unconditionally after the INSERT — failures
    inside the sink are swallowed so a sink failure can never roll
    back the database commit (VAL-CROSS-BACKCOMPAT-907).
    """
    payload_json = json.dumps(payload)
    event_id = await conn.fetchval(
        """
        INSERT INTO events (task_id, plan_id, event_type, payload)
        VALUES ($1, $2, $3, $4::jsonb)
        RETURNING id
        """,
        task_id,
        plan_id,
        event_type,
        payload_json,
    )
    if jsonl_sink is not None:
        try:
            jsonl_sink.record(
                event_type,
                task_id=task_id,
                plan_id=plan_id,
                payload=payload,
            )
        except Exception:  # noqa: BLE001 — fail-open contract.
            logger.warning(
                "pr_iterate jsonl mirror: record(%s) raised — swallowing",
                event_type,
                exc_info=True,
            )
    return int(event_id)


async def spawn_followup(
    *,
    orig_task_id: str,
    pr_url: str,
    comments: Iterable[Any] | None,
    plan_id: str,
    conn: asyncpg.Connection,
    jsonl_sink: JsonlEventSink | None = None,
    env: Mapping[str, str] | None = None,
) -> Task | None:
    """Spawn a follow-up rev task in response to a CHANGES_REQUESTED review.

    Behaviour
    ---------
    1. Validate ``orig_task_id`` with the M1 task-id regex BEFORE any
       database write. Malformed input raises :class:`ValueError` and
       produces zero side effects.
    2. Sanitize the concatenation of the supplied comment bodies via
       :func:`build_followup_description` (M1 sanitizer with
       ``scope='pr_review_comment'``).
    3. Count existing ``orig_task_id-rev-*`` rows in the ``tasks``
       table.
    4. If the existing count is ``>= cap``
       (``WHILLY_MAX_REVIEW_ITERATIONS``, default ``3``), do NOT
       insert a new task. Emit a single
       ``pr.iteration.requested`` event with ``detail.refused=True``
       and ``detail.iteration`` set to the current count, embedding
       the SANITIZED comment payload in ``detail.comments`` so the
       audit trail still records the offending review (VAL-PR-017,
       VAL-CROSS-006). Return ``None``.
    5. Otherwise INSERT the new task row with id
       ``f"{orig_task_id}-rev-{count+1}"``,
       ``dependencies=[orig_task_id]``,
       ``prd_requirement=pr_url``,
       ``description`` = sanitized concatenation, ``status='PENDING'``,
       and copies of the originating task's ``priority`` and
       ``key_files``. Then emit ``pr.iteration.requested`` with
       ``detail = {orig_task_id, new_task_id, pr_url, iteration}``
       (1-indexed). Return the constructed
       :class:`whilly.core.models.Task`.

    Args
    ----
    orig_task_id:
        The originating task id (the task whose PR received the
        CHANGES_REQUESTED review).
    pr_url:
        The full ``https://github.com/<owner>/<repo>/pull/<n>`` URL
        of the PR that requested changes. Stored on the new task's
        ``prd_requirement`` column for downstream PR-fix prompt
        building.
    comments:
        Iterable of review comment bodies — typically a list of
        ``{"body": ..., "path": ..., "line": ..., "author": ...}``
        dicts as produced by
        :mod:`whilly.sources.github_pr_feedback`. Non-mapping entries
        and entries lacking a ``body`` key are silently skipped.
    plan_id:
        The plan that owns the originating task. The new rev task is
        inserted under the same ``plan_id``.
    conn:
        A live :class:`asyncpg.Connection`. The caller owns the
        transaction; this function does not start or commit one. The
        INSERT and event emission run on the supplied connection so
        they land in whatever transaction the caller is composing.
    jsonl_sink:
        Optional JSONL audit sink. When provided, every emitted
        event is also mirrored to ``whilly_logs/whilly_events.jsonl``
        via :meth:`JsonlEventSink.record`. Failures inside the sink
        are swallowed.
    env:
        Optional mapping for the cap lookup; used by tests to avoid
        process-global env mutation. Defaults to :data:`os.environ`.

    Returns
    -------
    Task | None
        The newly-inserted :class:`Task` on a successful spawn;
        ``None`` when the cap fired (no row inserted).

    Raises
    ------
    ValueError
        ``orig_task_id`` violates the M1 task-id regex, OR the
        originating task does not exist in ``tasks``.
    """
    validate_task_id(orig_task_id)

    sanitized_description = build_followup_description(comments)
    cap = get_max_review_iterations(env)

    existing_count = await _count_existing_rev_tasks(conn, orig_task_id)

    if existing_count >= cap:
        cap_payload: dict[str, Any] = {
            "orig_task_id": orig_task_id,
            "pr_url": pr_url,
            "iteration": existing_count,
            "refused": True,
            "reason": "max_review_iterations_exceeded",
            "max_review_iterations": cap,
            "comments": sanitized_description,
        }
        await _emit_event_with_conn(
            conn,
            event_type=PR_ITERATION_REQUESTED_EVENT_TYPE,
            plan_id=plan_id,
            task_id=orig_task_id,
            payload=cap_payload,
            jsonl_sink=jsonl_sink,
        )
        logger.warning(
            "spawn_followup: cap fired for orig_task_id=%s (existing=%d, cap=%d)",
            orig_task_id,
            existing_count,
            cap,
        )
        return None

    orig_row = await conn.fetchrow(
        "SELECT priority, key_files FROM tasks WHERE id = $1",
        orig_task_id,
    )
    if orig_row is None:
        raise ValueError(
            f"spawn_followup: orig_task_id {orig_task_id!r} does not exist in tasks table",
        )

    priority_value = str(orig_row["priority"])
    raw_key_files = orig_row["key_files"]
    if isinstance(raw_key_files, str):
        decoded_key_files = json.loads(raw_key_files)
    elif raw_key_files is None:
        decoded_key_files = []
    else:
        decoded_key_files = list(raw_key_files)
    key_files_list: list[str] = [str(item) for item in decoded_key_files]

    new_task_id = f"{orig_task_id}-rev-{existing_count + 1}"
    iteration = existing_count + 1

    await conn.execute(
        """
        INSERT INTO tasks (
            id,
            plan_id,
            status,
            dependencies,
            key_files,
            priority,
            description,
            acceptance_criteria,
            test_steps,
            prd_requirement,
            version
        )
        VALUES (
            $1, $2, 'PENDING', $3::jsonb, $4::jsonb,
            $5, $6, '[]'::jsonb, '[]'::jsonb, $7, 0
        )
        """,
        new_task_id,
        plan_id,
        json.dumps([orig_task_id]),
        json.dumps(key_files_list),
        priority_value,
        sanitized_description,
        pr_url,
    )

    requested_payload: dict[str, Any] = {
        "orig_task_id": orig_task_id,
        "new_task_id": new_task_id,
        "pr_url": pr_url,
        "iteration": iteration,
    }
    await _emit_event_with_conn(
        conn,
        event_type=PR_ITERATION_REQUESTED_EVENT_TYPE,
        plan_id=plan_id,
        task_id=new_task_id,
        payload=requested_payload,
        jsonl_sink=jsonl_sink,
    )

    logger.info(
        "spawn_followup: orig=%s new=%s iteration=%d cap=%d",
        orig_task_id,
        new_task_id,
        iteration,
        cap,
    )

    return Task(
        id=new_task_id,
        status=TaskStatus.PENDING,
        dependencies=(orig_task_id,),
        key_files=tuple(key_files_list),
        priority=Priority(priority_value),
        description=sanitized_description,
        acceptance_criteria=(),
        test_steps=(),
        prd_requirement=pr_url,
        version=0,
    )


async def emit_iteration_completed(
    *,
    repo: TaskRepository,
    plan_id: str,
    task_id: str,
) -> int | None:
    """Emit ``pr.iteration.completed`` if ``task_id`` matches ``*-rev-N``.

    Returns the inserted ``events.id`` on emission, or ``None`` when
    ``task_id`` is not a rev task (a normal originating task COMPLETE
    must produce no ``pr.iteration.completed`` row). The detail
    payload carries ``orig_task_id``, ``new_task_id``, and
    ``iteration`` (1-indexed) — matching the symmetric shape of the
    ``pr.iteration.requested`` event so an audit consumer can pair
    the two by ``new_task_id`` (VAL-PR-025).
    """
    parsed = parse_rev_task_id(task_id)
    if parsed is None:
        return None
    orig_task_id, iteration = parsed
    payload: dict[str, Any] = {
        "orig_task_id": orig_task_id,
        "new_task_id": task_id,
        "iteration": iteration,
    }
    return await repo.emit_pr_event(
        PR_ITERATION_COMPLETED_EVENT_TYPE,
        plan_id=plan_id,
        task_id=task_id,
        payload=payload,
    )
