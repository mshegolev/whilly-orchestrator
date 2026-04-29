"""Wire schemas for the worker ↔ control-plane HTTP protocol (PRD FR-1.2, TC-6).

This module is the **pure pydantic** layer of :mod:`whilly.adapters.transport`:
request/response models for every RPC the remote worker speaks to the
control plane. By design it imports neither FastAPI nor asyncpg nor httpx
so the same models can be reused by:

* the FastAPI server (TASK-021a3) — as ``response_model=`` and request body
  annotations,
* the httpx client inside the remote worker (TASK-022a) — for request
  construction and response parsing,
* unit tests — without spinning up either side.

This split is what makes a worker a *thin* httpx client (TASK-022b1): the
worker package only depends on this schemas module + httpx + the pure
:mod:`whilly.core` layer, never on FastAPI.

Schema map
----------
======================  ========================  ============================
RPC                     Request                   Response
======================  ========================  ============================
``POST /workers/register``  :class:`RegisterRequest`  :class:`RegisterResponse`
``POST /tasks/claim``       :class:`ClaimRequest`     :class:`ClaimResponse`
``POST /tasks/{id}/complete`` :class:`CompleteRequest`  :class:`CompleteResponse`
``POST /tasks/{id}/fail``   :class:`FailRequest`      :class:`FailResponse`
``POST /workers/{id}/heartbeat`` :class:`HeartbeatRequest` :class:`HeartbeatResponse`
``*`` (4xx / 5xx)           —                         :class:`ErrorResponse`
======================  ========================  ============================

:class:`TaskPayload` is the wire-format projection of
:class:`whilly.core.models.Task`. The domain dataclass uses tuples for its
sequence fields and is intentionally hash-stable; the wire model uses lists
because JSON has no tuple type and the network boundary is the right place
to translate. ``.from_task`` / ``.to_task`` keep the conversion in one
place so server and client never duplicate the field-by-field copy.

Why frozen pydantic models?
    Matches the value-object semantics of the core domain dataclasses. A
    handler that receives a request body or a response body should not be
    free to mutate it after validation — any "modified" payload should be a
    new instance. ``model_config = ConfigDict(frozen=True)`` enforces that
    at runtime.

Why ``model_config = ConfigDict(extra="forbid")``?
    A worker that sends an unknown field is more likely to be mid-rolling-
    update against an older control plane than to be intentionally
    extensible. Failing closed surfaces version skew immediately as a 422
    instead of silently dropping the field — protocol drift then shows up
    in CI / smoke tests, not in a half-broken production claim.

Validation contract
-------------------
* All free-form ID strings (``worker_id``, ``plan_id``, ``task_id``,
  ``token``, ``hostname``) require ``min_length=1`` — the empty string is
  never a valid identifier. Length caps (``max_length``) are conservative
  upper bounds that match the database column types (TEXT, but FK joins
  benefit from sane caps and we don't want a pathological 1MB body).
* ``version`` is a non-negative integer (PRD FR-2.4 monotonic counter).
* ``reason`` on :class:`FailRequest` requires non-empty content because
  the value flows straight into the ``events.payload`` audit row — a blank
  reason would be useless for the dashboard / post-mortem queries.

mypy --strict
-------------
This module is the only adapter that mypy --strict targets directly today
(``python3 -m mypy --strict whilly/adapters/transport/schemas.py`` per
TASK-021a1's test_steps). pydantic 2.x ships type stubs out of the box, so
``BaseModel`` subclasses with class-level field annotations satisfy
strict mode without ``Any`` escapes or explicit method annotations on
generated ``__init__`` / ``__eq__``.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated, Final

from pydantic import BaseModel, ConfigDict, Field

from whilly.core.models import Plan, Priority, Task, TaskId, TaskStatus

# ---------------------------------------------------------------------------
# Validation primitives
# ---------------------------------------------------------------------------
# Hand-rolled ``Annotated`` aliases so each request/response can declare
# ``hostname: NonEmptyShortStr`` and inherit the constraint without having
# to copy the ``Field(min_length=...)`` boilerplate. Caps are chosen to
# match what the Postgres schema and downstream callers accept while still
# rejecting obviously-malformed payloads (e.g. a 1MB ``hostname``).
#
# ``MAX_REASON_LEN`` is larger because failure reasons can include a
# truncated stdout snippet from the agent — see ``_FAIL_REASON_OUTPUT_CAP``
# in :mod:`whilly.worker.local` (500 chars) plus the ``exit_code=...``
# prefix. We use 2KiB to give a generous headroom without inviting a
# 64KB rant from a misbehaving agent into the audit log.

MAX_ID_LEN: Final[int] = 256
MAX_HOSTNAME_LEN: Final[int] = 256
MAX_TOKEN_LEN: Final[int] = 1024
MAX_REASON_LEN: Final[int] = 2048
MAX_DESCRIPTION_LEN: Final[int] = 8192

NonEmptyShortStr = Annotated[str, Field(min_length=1, max_length=MAX_ID_LEN)]
NonEmptyHostname = Annotated[str, Field(min_length=1, max_length=MAX_HOSTNAME_LEN)]
NonEmptyToken = Annotated[str, Field(min_length=1, max_length=MAX_TOKEN_LEN)]
NonEmptyReason = Annotated[str, Field(min_length=1, max_length=MAX_REASON_LEN)]
NonNegativeVersion = Annotated[int, Field(ge=0)]


class _FrozenModel(BaseModel):
    """Shared ``model_config`` for every wire schema.

    Subclassing keeps the per-class declarations short and guarantees no
    request/response model in this module accidentally drifts away from the
    "frozen + extra=forbid" contract. Both knobs are deliberate:

    * ``frozen=True`` — handlers and tests cannot mutate a validated
      payload; any "modified" version must be a fresh instance.
    * ``extra="forbid"`` — unknown fields raise 422 instead of being
      silently dropped, surfacing version skew between worker / server
      builds at the network boundary instead of much later in production.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")


# ---------------------------------------------------------------------------
# TaskPayload — wire-format projection of whilly.core.models.Task
# ---------------------------------------------------------------------------


class TaskPayload(_FrozenModel):
    """JSON-serialisable projection of :class:`whilly.core.models.Task`.

    The core :class:`Task` is a frozen dataclass that uses ``tuple`` for
    every sequence field (so the value object stays effectively immutable
    end-to-end). JSON has no tuple type and pydantic v2 serialises tuples
    differently from lists, so the wire boundary is the right place to
    translate: callers go through :meth:`from_task` / :meth:`to_task`
    instead of constructing this model field-by-field, which keeps the
    conversion in one place and avoids drift if :class:`Task` grows new
    fields later.

    Field semantics mirror the source dataclass exactly — see
    :class:`whilly.core.models.Task` for the per-field documentation.
    """

    id: NonEmptyShortStr
    status: TaskStatus
    dependencies: list[NonEmptyShortStr] = Field(default_factory=list)
    key_files: list[str] = Field(default_factory=list)
    priority: Priority = Priority.MEDIUM
    description: Annotated[str, Field(max_length=MAX_DESCRIPTION_LEN)] = ""
    acceptance_criteria: list[str] = Field(default_factory=list)
    test_steps: list[str] = Field(default_factory=list)
    prd_requirement: str = ""
    version: NonNegativeVersion = 0

    @classmethod
    def from_task(cls, task: Task) -> TaskPayload:
        """Build a wire payload from a domain :class:`Task`.

        Tuple → list conversion is done explicitly here so a future change
        to :class:`Task`'s container types can't silently desync the
        contract — the test suite for this module asserts on the JSON shape.
        """
        return cls(
            id=task.id,
            status=task.status,
            dependencies=list(task.dependencies),
            key_files=list(task.key_files),
            priority=task.priority,
            description=task.description,
            acceptance_criteria=list(task.acceptance_criteria),
            test_steps=list(task.test_steps),
            prd_requirement=task.prd_requirement,
            version=task.version,
        )

    def to_task(self) -> Task:
        """Reconstruct a domain :class:`Task` from this wire payload.

        Inverse of :meth:`from_task`: list → tuple, every other field
        passes through unchanged. Used by the remote worker (TASK-022a)
        after deserialising a :class:`ClaimResponse` so the rest of the
        worker loop can speak in pure-domain types.
        """
        return Task(
            id=self.id,
            status=self.status,
            dependencies=tuple(self.dependencies),
            key_files=tuple(self.key_files),
            priority=self.priority,
            description=self.description,
            acceptance_criteria=tuple(self.acceptance_criteria),
            test_steps=tuple(self.test_steps),
            prd_requirement=self.prd_requirement,
            version=self.version,
        )


class PlanPayload(_FrozenModel):
    """JSON-serialisable projection of :class:`whilly.core.models.Plan`.

    Returned alongside :class:`TaskPayload` in :class:`ClaimResponse` so a
    remote worker can call :func:`whilly.core.prompts.build_task_prompt`
    without an extra round-trip to fetch the plan metadata. ``tasks`` is
    intentionally **not** serialised here: the worker only needs the
    plan's ``id`` and ``name`` for prompt context, and shipping the full
    sibling task list on every claim would amplify the payload size by an
    order of magnitude on plans with hundreds of tasks.
    """

    id: NonEmptyShortStr
    name: NonEmptyShortStr

    @classmethod
    def from_plan(cls, plan: Plan) -> PlanPayload:
        """Build a wire payload from a domain :class:`Plan`."""
        return cls(id=plan.id, name=plan.name)


# ---------------------------------------------------------------------------
# Register
# ---------------------------------------------------------------------------


class RegisterRequest(_FrozenModel):
    """``POST /workers/register`` request body (PRD FR-1.1).

    A worker calls this once on boot to obtain its ``worker_id`` + bearer
    ``token``. ``hostname`` is the only field the worker can self-report
    that's worth recording — it shows up in the dashboard (TASK-027) so
    operators can correlate workers with the boxes they run on.
    """

    hostname: NonEmptyHostname


class RegisterResponse(_FrozenModel):
    """``POST /workers/register`` response body (PRD FR-1.1).

    ``worker_id`` is the server-issued identifier (also the primary key of
    the ``workers`` table). ``token`` is the *plaintext* bearer token —
    returned exactly once at registration and never persisted on the
    server: only its hash lives in ``workers.token_hash`` (PRD NFR-3).
    The worker MUST keep this token in memory and resend it as the
    ``Authorization: Bearer <token>`` header on every subsequent RPC; the
    auth dependency in TASK-021a2 verifies the hash on each request.
    """

    worker_id: NonEmptyShortStr
    token: NonEmptyToken


# ---------------------------------------------------------------------------
# Claim
# ---------------------------------------------------------------------------


class ClaimRequest(_FrozenModel):
    """``POST /tasks/claim`` request body (PRD FR-1.2, FR-1.3).

    Long-polled at the server (up to 30s — TASK-021c1): when no PENDING
    rows are available the server holds the request open instead of
    returning an empty body immediately, so the worker can wake up the
    moment a task lands without slamming the database with poll queries.

    ``worker_id`` echoes the registered identity even though the bearer
    token also identifies the worker — the explicit echo lets the server
    validate that the token's owner matches the claimer (defence-in-depth
    against a leaked but mis-rotated token).
    """

    worker_id: NonEmptyShortStr
    plan_id: NonEmptyShortStr


class ClaimResponse(_FrozenModel):
    """``POST /tasks/claim`` response body.

    ``task is None`` is the documented "long-polling timeout, queue empty"
    outcome — the worker should sleep ``idle_wait`` and re-issue the
    claim. ``task is not None`` is the canonical happy path: the row has
    already transitioned ``PENDING`` → ``CLAIMED`` server-side and the
    payload echoes the post-update ``version`` so the worker can pass it
    straight into a follow-up ``start_task`` call.

    ``plan`` carries the parent plan's ``id`` and ``name`` so the worker
    can build the agent prompt without a second round-trip — see
    :class:`PlanPayload` for the rationale on omitting sibling tasks.
    """

    task: TaskPayload | None = None
    plan: PlanPayload | None = None


# ---------------------------------------------------------------------------
# Complete
# ---------------------------------------------------------------------------


class CompleteRequest(_FrozenModel):
    """``POST /tasks/{task_id}/complete`` request body (PRD FR-2.2, FR-2.4).

    The path parameter ``task_id`` is **not** repeated here — FastAPI
    binds it from the URL. Body carries the optimistic-locking
    ``version`` so the server's ``UPDATE ... WHERE id = $1 AND version =
    $2 AND status = 'IN_PROGRESS'`` filter sees exactly the version the
    worker last observed. ``worker_id`` is echoed for the same defence-
    in-depth reason as :class:`ClaimRequest`.

    Plan budget guard (TASK-102)
    ----------------------------
    ``cost_usd`` is the optional spend amount the worker accumulated
    while running this task — typically the value parsed from
    ``AgentResult.usage.cost_usd`` (Claude CLI's ``total_cost_usd``).
    The server forwards it into
    :meth:`whilly.adapters.db.repository.TaskRepository.complete_task`,
    which atomically increments ``plans.spent_usd`` by this amount in
    the same transaction as the task → DONE flip. ``None`` (or the
    field omitted entirely) is treated as ``0`` — the no-op spend
    path (VAL-BUDGET-032). Negative values are rejected at the schema
    layer (the strict-monotonic spend invariant lives on the
    repository contract, but rejecting at the wire is cheaper).

    The field is typed as ``Decimal | None`` so a worker that knows
    its cost in exact form (e.g. echoed from a billing system) can
    skip the float round-trip; pydantic 2.x serialises ``Decimal`` as
    a JSON string by default which round-trips losslessly through
    asyncpg's NUMERIC adapter.
    """

    worker_id: NonEmptyShortStr
    version: NonNegativeVersion
    # Spend echo (TASK-102). ``ge=0`` rejects negatives at the wire so
    # the strict-monotonic invariant on ``plans.spent_usd`` is enforced
    # before the SQL UPDATE even fires. ``None`` (default) → repository
    # treats as ``0`` (no-op spend).
    cost_usd: Decimal | None = Field(default=None, ge=0)


class CompleteResponse(_FrozenModel):
    """``POST /tasks/{task_id}/complete`` response body.

    Carries the post-update :class:`TaskPayload` (status ``DONE``,
    ``version`` incremented). The worker's stats counter increments on a
    2xx; a 409 surfaces via :class:`ErrorResponse` and the conflict-
    classification logic from
    :class:`whilly.adapters.db.repository.VersionConflictError`.
    """

    task: TaskPayload


# ---------------------------------------------------------------------------
# Fail
# ---------------------------------------------------------------------------


class FailRequest(_FrozenModel):
    """``POST /tasks/{task_id}/fail`` request body (PRD FR-2.2, FR-2.4).

    Same shape as :class:`CompleteRequest` plus a non-empty ``reason``:
    the value flows directly into the ``events.payload`` audit row, so
    the dashboard and post-mortem queries can show *why* without
    re-scanning logs (matches the ``reason`` parameter of
    :meth:`whilly.adapters.db.repository.TaskRepository.fail_task`).
    """

    worker_id: NonEmptyShortStr
    version: NonNegativeVersion
    reason: NonEmptyReason


class FailResponse(_FrozenModel):
    """``POST /tasks/{task_id}/fail`` response body.

    Mirrors :class:`CompleteResponse`: post-update :class:`TaskPayload`
    with status ``FAILED`` and the version counter incremented.
    """

    task: TaskPayload


# ---------------------------------------------------------------------------
# Release (TASK-022b3)
# ---------------------------------------------------------------------------


class ReleaseRequest(_FrozenModel):
    """``POST /tasks/{task_id}/release`` request body (PRD FR-1.6, NFR-1).

    Worker-driven release of an in-flight task back to ``PENDING`` —
    the HTTP analogue of :meth:`whilly.adapters.db.repository.TaskRepository.release_task`.
    Used by the remote worker (TASK-022b3) on SIGTERM / SIGINT so a peer
    (or this worker on restart) can re-claim the task within one poll
    cycle instead of waiting out the visibility-timeout sweep
    (default 15 minutes, PRD FR-1.4).

    Body shape is identical to :class:`FailRequest`: optimistic-locking
    ``version`` and a non-empty ``reason``. The reason flows directly
    into the ``RELEASE`` event payload — distinguishes shutdown
    releases (``"shutdown"``) from sweep-driven releases
    (``"visibility_timeout"``) so dashboards / post-mortems can attribute
    the bounce without re-reading worker logs.
    """

    worker_id: NonEmptyShortStr
    version: NonNegativeVersion
    reason: NonEmptyReason


class ReleaseResponse(_FrozenModel):
    """``POST /tasks/{task_id}/release`` response body.

    Carries the post-update :class:`TaskPayload` with status ``PENDING``
    and the version incremented (the row's ``claimed_by`` /
    ``claimed_at`` are also cleared on the server side, but they aren't
    on the wire payload — the worker that asked to release it has no
    use for them).
    """

    task: TaskPayload


# ---------------------------------------------------------------------------
# Heartbeat
# ---------------------------------------------------------------------------


class HeartbeatRequest(_FrozenModel):
    """``POST /workers/{worker_id}/heartbeat`` request body (PRD FR-1.6).

    The path parameter is the canonical worker identity; the body's
    ``worker_id`` is the same defence-in-depth echo as the other RPCs.
    No additional payload — the heartbeat's only job is to refresh
    ``workers.last_heartbeat = NOW()`` so the visibility-timeout sweep
    (PRD FR-1.4) doesn't reclaim live workers.
    """

    worker_id: NonEmptyShortStr


class HeartbeatResponse(_FrozenModel):
    """``POST /workers/{worker_id}/heartbeat`` response body.

    ``ok`` is the boolean returned by
    :meth:`whilly.adapters.db.repository.TaskRepository.update_heartbeat`:
    ``True`` when a row matched, ``False`` when ``worker_id`` is no
    longer registered (admin revoked the worker — recoverable, the
    worker should re-register and continue).
    """

    ok: bool


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ErrorResponse(_FrozenModel):
    """Shared error envelope for any non-2xx response (PRD FR-1.2).

    FastAPI's default error body is ``{"detail": "..."}``; we wrap it in
    a typed model so the httpx client (TASK-022a) can ``response.json()``
    and validate against this schema instead of probing for ``detail``
    by hand. ``error`` is a short machine-readable code (e.g.
    ``"version_conflict"``, ``"unauthorized"``, ``"plan_not_found"``)
    that handlers map from the corresponding repository exception or
    auth failure; ``detail`` is the human-readable description.

    ``task_id`` / ``actual_version`` / ``actual_status`` are populated
    only on optimistic-locking conflicts (mapped from
    :class:`whilly.adapters.db.repository.VersionConflictError`) so the
    worker can decide whether to retry, drop, or surface to the
    operator without re-running a SELECT itself. See
    ``VersionConflictError.__doc__`` for the field semantics.
    """

    error: NonEmptyShortStr
    detail: str = ""
    task_id: TaskId | None = None
    expected_version: NonNegativeVersion | None = None
    actual_version: NonNegativeVersion | None = None
    actual_status: TaskStatus | None = None


__all__ = [
    "MAX_DESCRIPTION_LEN",
    "MAX_HOSTNAME_LEN",
    "MAX_ID_LEN",
    "MAX_REASON_LEN",
    "MAX_TOKEN_LEN",
    "ClaimRequest",
    "ClaimResponse",
    "CompleteRequest",
    "CompleteResponse",
    "ErrorResponse",
    "FailRequest",
    "FailResponse",
    "HeartbeatRequest",
    "HeartbeatResponse",
    "PlanPayload",
    "RegisterRequest",
    "RegisterResponse",
    "ReleaseRequest",
    "ReleaseResponse",
    "TaskPayload",
]
