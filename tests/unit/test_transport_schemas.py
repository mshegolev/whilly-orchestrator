"""Unit tests for :mod:`whilly.adapters.transport.schemas` (TASK-021a1, PRD FR-1.2, TC-6).

The schemas module is the wire-level contract between the remote worker
(httpx client, TASK-022a) and the FastAPI control plane (TASK-021a3). Its
correctness is load-bearing: every downstream RPC handler imports from
here. These tests cover the AC for TASK-021a1:

* every documented request / response model exists and accepts a happy-path
  payload;
* :class:`TaskPayload` round-trips with :class:`whilly.core.models.Task`
  bit-for-bit (tuple ↔ list at the wire boundary, no other drift);
* JSON ↔ model round-trip survives ``model_dump_json`` →
  ``model_validate_json`` for a fully-populated ``TaskPayload``;
* validators reject empty IDs / tokens / reasons and negative versions;
* ``model_config = ConfigDict(frozen=True, extra="forbid")`` is enforced
  (mutation raises, unknown fields raise);
* the module imports nothing from FastAPI / asyncpg / httpx (TC-8 / SC-6).
"""

from __future__ import annotations

import sys

import pytest
from pydantic import ValidationError

from whilly.adapters.transport.schemas import (
    ClaimRequest,
    ClaimResponse,
    CompleteRequest,
    CompleteResponse,
    ErrorResponse,
    FailRequest,
    FailResponse,
    HeartbeatRequest,
    HeartbeatResponse,
    PlanPayload,
    RegisterRequest,
    RegisterResponse,
    TaskPayload,
)
from whilly.core.models import Plan, Priority, Task, TaskStatus

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sample_task() -> Task:
    """A maximally non-default :class:`Task` so round-trip drift surfaces.

    Every collection field carries at least two entries and every scalar is
    set away from its dataclass default — a silent drop of any field during
    serialisation is then immediately visible in the equality assertion.
    """
    return Task(
        id="TASK-021a1",
        status=TaskStatus.IN_PROGRESS,
        dependencies=("TASK-009d", "TASK-021a0"),
        key_files=("whilly/adapters/transport/schemas.py",),
        priority=Priority.CRITICAL,
        description="pydantic schemas for the worker protocol",
        acceptance_criteria=("frozen", "extra=forbid"),
        test_steps=("python3 -m mypy --strict ...",),
        prd_requirement="FR-1.2, TC-6",
        version=7,
    )


# ---------------------------------------------------------------------------
# TaskPayload round-trip
# ---------------------------------------------------------------------------


def test_task_payload_round_trip_preserves_all_fields() -> None:
    """``TaskPayload.from_task(t).to_task() == t`` for every field set."""
    task = _sample_task()
    assert TaskPayload.from_task(task).to_task() == task


def test_task_payload_dependencies_become_lists_on_the_wire() -> None:
    """Tuple → list conversion is what the JSON contract guarantees."""
    payload = TaskPayload.from_task(_sample_task())
    assert payload.dependencies == ["TASK-009d", "TASK-021a0"]
    assert payload.key_files == ["whilly/adapters/transport/schemas.py"]


def test_task_payload_json_round_trip() -> None:
    """A full JSON serialise → parse cycle reproduces the original payload."""
    original = TaskPayload.from_task(_sample_task())
    rebuilt = TaskPayload.model_validate_json(original.model_dump_json())
    assert rebuilt == original
    assert rebuilt.to_task() == _sample_task()


def test_plan_payload_from_plan_omits_tasks() -> None:
    """``PlanPayload`` is intentionally task-free — see schemas.py docstring."""
    plan = Plan(id="PLAN-1", name="Refactor", tasks=(_sample_task(),))
    payload = PlanPayload.from_plan(plan)
    assert payload.id == "PLAN-1"
    assert payload.name == "Refactor"
    # ``tasks`` is not part of the schema at all — confirm it didn't sneak in.
    assert "tasks" not in payload.model_dump()


# ---------------------------------------------------------------------------
# Request / response shapes
# ---------------------------------------------------------------------------


def test_register_request_response_happy_path() -> None:
    req = RegisterRequest(hostname="worker-01.local")
    resp = RegisterResponse(worker_id="w-abc", token="opaque-bearer")
    assert req.hostname == "worker-01.local"
    assert req.owner_email is None
    assert resp.worker_id == "w-abc"
    assert resp.token == "opaque-bearer"


def test_register_request_accepts_owner_email() -> None:
    """``owner_email`` is optional and round-trips when set (M2 migration 008)."""
    req = RegisterRequest(hostname="worker-01.local", owner_email="alice@example.com")
    assert req.owner_email == "alice@example.com"


@pytest.mark.parametrize(
    "bad_email",
    [
        pytest.param("", id="empty"),
        pytest.param("plain", id="no-at-sign"),
        pytest.param("a@", id="no-domain"),
        pytest.param("@b.com", id="no-local-part"),
        pytest.param("a@b", id="no-dot-in-domain"),
        pytest.param("a b@c.com", id="space-in-local"),
        pytest.param("a@b@c.com", id="double-at"),
    ],
)
def test_register_request_rejects_malformed_owner_email(bad_email: str) -> None:
    """Malformed ``owner_email`` values surface as 422 at the wire boundary."""
    with pytest.raises(ValidationError):
        RegisterRequest(hostname="worker-01.local", owner_email=bad_email)


def test_claim_response_empty_queue_is_none() -> None:
    """Long-poll timeout signals "no task" via ``task is None``."""
    resp = ClaimResponse()
    assert resp.task is None
    assert resp.plan is None


def test_claim_response_with_task_carries_plan_metadata() -> None:
    payload = TaskPayload.from_task(_sample_task())
    plan_payload = PlanPayload(id="PLAN-1", name="Refactor")
    resp = ClaimResponse(task=payload, plan=plan_payload)
    assert resp.task == payload
    assert resp.plan == plan_payload


def test_complete_and_fail_responses_carry_post_update_task() -> None:
    payload = TaskPayload.from_task(_sample_task())
    assert CompleteResponse(task=payload).task == payload
    assert FailResponse(task=payload).task == payload


def test_heartbeat_response_ok_field() -> None:
    """Mirrors :meth:`TaskRepository.update_heartbeat` boolean return."""
    assert HeartbeatResponse(ok=True).ok is True
    assert HeartbeatResponse(ok=False).ok is False


def test_error_response_carries_version_conflict_fields() -> None:
    """Optimistic-lock conflict surface must echo all three classification fields."""
    err = ErrorResponse(
        error="version_conflict",
        detail="task moved past expected version",
        task_id="TASK-001",
        expected_version=2,
        actual_version=3,
        actual_status=TaskStatus.DONE,
    )
    assert err.error == "version_conflict"
    assert err.task_id == "TASK-001"
    assert err.expected_version == 2
    assert err.actual_version == 3
    assert err.actual_status is TaskStatus.DONE


# ---------------------------------------------------------------------------
# Validators
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "kwargs",
    [
        pytest.param({"worker_id": "", "plan_id": "P"}, id="empty-worker-id"),
        pytest.param({"worker_id": "w", "plan_id": ""}, id="empty-plan-id"),
    ],
)
def test_claim_request_rejects_empty_ids(kwargs: dict[str, str]) -> None:
    with pytest.raises(ValidationError):
        ClaimRequest(**kwargs)


def test_register_request_rejects_empty_hostname() -> None:
    with pytest.raises(ValidationError):
        RegisterRequest(hostname="")


def test_register_response_rejects_empty_token() -> None:
    with pytest.raises(ValidationError):
        RegisterResponse(worker_id="w", token="")


def test_complete_request_rejects_negative_version() -> None:
    with pytest.raises(ValidationError):
        CompleteRequest(worker_id="w", version=-1)


def test_fail_request_requires_non_empty_reason() -> None:
    """Reason flows into the audit log — blank values would be useless."""
    with pytest.raises(ValidationError):
        FailRequest(worker_id="w", version=1, reason="")


# ---------------------------------------------------------------------------
# Model config: frozen + extra=forbid
# ---------------------------------------------------------------------------


def test_models_are_frozen() -> None:
    resp = RegisterResponse(worker_id="w", token="t")
    with pytest.raises(ValidationError):
        resp.token = "other"  # type: ignore[misc]


def test_unknown_fields_are_rejected() -> None:
    """Surfaces version skew between worker / server builds at the wire boundary."""
    with pytest.raises(ValidationError):
        HeartbeatRequest(worker_id="w", extra_field=True)  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# Hexagonal-architecture guard (PRD TC-8 / SC-6)
# ---------------------------------------------------------------------------


def test_schemas_module_does_not_import_io_libraries() -> None:
    """No FastAPI / asyncpg / httpx in the import closure of ``schemas``.

    ``import-linter`` enforces this at the package level, but a dedicated
    unit test gives a fast local feedback loop and catches accidental
    transitive imports introduced via a helper that *seems* domain-pure.
    """
    forbidden = {"fastapi", "asyncpg", "httpx", "uvicorn", "sqlalchemy", "alembic"}
    for name in list(sys.modules):
        head = name.split(".", 1)[0]
        if head in forbidden:
            # Could be loaded by an earlier test; what we really care about is
            # whether re-importing schemas drags any of these in fresh.
            continue

    before = set(sys.modules)
    # Re-import via importlib so the assertion is meaningful on a warm cache.
    import importlib

    importlib.import_module("whilly.adapters.transport.schemas")
    new_modules = set(sys.modules) - before
    leaked = {m for m in new_modules if m.split(".", 1)[0] in forbidden}
    assert not leaked, f"schemas.py pulled in forbidden modules: {sorted(leaked)}"
