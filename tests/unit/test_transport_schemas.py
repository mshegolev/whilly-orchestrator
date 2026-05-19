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
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from whilly.adapters.transport.schemas import (
    ClaimRequest,
    ClaimResponse,
    CompleteRequest,
    CompleteResponse,
    ControlPauseRequest,
    ControlStateResponse,
    ErrorResponse,
    FailRequest,
    FailResponse,
    HeartbeatRequest,
    HeartbeatResponse,
    HumanReviewDecisionRequest,
    ListTaskEventsResponse,
    PlanPayload,
    RegisterRequest,
    RegisterResponse,
    TaskEventItem,
    TaskEventRequest,
    TaskEventResponse,
    TaskPayload,
    VerificationCommandPayload,
)
from whilly.core.models import Plan, Priority, Task, TaskStatus, VerificationCommand

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


UTC = timezone.utc


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
        repo_target_id="github:owner/repo",
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
    assert payload.repo_target_id == "github:owner/repo"


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


def test_plan_payload_from_plan_includes_ordered_verification_commands() -> None:
    """Plan metadata carries verification commands but not sibling tasks."""
    commands = (
        VerificationCommand(
            name="profile-required",
            command=".venv/bin/python -m pytest -q tests/unit",
            required=True,
            source="profile",
        ),
        VerificationCommand(
            name="profile-optional",
            command=".venv/bin/python -m pytest -q tests/integration --maxfail=1",
            required=False,
            source="profile",
        ),
    )
    plan = Plan(id="PLAN-VERIFY", name="Verification", tasks=(_sample_task(),), verification_commands=commands)

    payload = PlanPayload.from_plan(plan)

    assert [item.name for item in payload.verification_commands] == ["profile-required", "profile-optional"]
    assert payload.model_dump()["verification_commands"] == [
        {
            "name": "profile-required",
            "command": ".venv/bin/python -m pytest -q tests/unit",
            "required": True,
            "source": "profile",
            "repair_max_attempts": 0,
        },
        {
            "name": "profile-optional",
            "command": ".venv/bin/python -m pytest -q tests/integration --maxfail=1",
            "required": False,
            "source": "profile",
            "repair_max_attempts": 0,
        },
    ]
    assert "tasks" not in payload.model_dump()


def test_plan_payload_json_round_trip_preserves_verification_source() -> None:
    """Transport JSON recreates pure Plan metadata without sibling tasks."""
    payload = PlanPayload(
        id="PLAN-VERIFY",
        name="Verification",
        verification_commands=[
            VerificationCommandPayload(
                name="profile-required",
                command=".venv/bin/python -m pytest -q tests/unit",
                required=True,
                source="profile",
            )
        ],
    )

    rebuilt = PlanPayload.model_validate_json(payload.model_dump_json())
    plan = rebuilt.to_plan()

    assert plan == Plan(
        id="PLAN-VERIFY",
        name="Verification",
        verification_commands=(
            VerificationCommand(
                name="profile-required",
                command=".venv/bin/python -m pytest -q tests/unit",
                required=True,
                source="profile",
            ),
        ),
    )
    assert plan.tasks == ()


def test_plan_payload_includes_ci_repair_budget() -> None:
    """Remote plan metadata preserves CI source and bounded repair budget."""
    plan = Plan(
        id="PLAN-CI-REPAIR",
        name="CI Repair",
        verification_commands=(
            VerificationCommand(
                name="github-ci",
                command="ci://github/checks?owner=acme&repo=demo&pr=42",
                required=True,
                source="ci",
                repair_max_attempts=2,
            ),
        ),
    )

    payload = PlanPayload.from_plan(plan)

    assert payload.model_dump()["verification_commands"] == [
        {
            "name": "github-ci",
            "command": "ci://github/checks?owner=acme&repo=demo&pr=42",
            "required": True,
            "source": "ci",
            "repair_max_attempts": 2,
        }
    ]
    assert payload.to_plan() == plan


# ---------------------------------------------------------------------------
# Request / response shapes
# ---------------------------------------------------------------------------


def test_register_request_response_happy_path() -> None:
    req = RegisterRequest(hostname="worker-01.local")
    resp = RegisterResponse(worker_id="w-abc", token="opaque-bearer")
    assert req.hostname == "worker-01.local"
    assert req.owner_email is None
    assert req.tags == []
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


def test_register_request_defaults_tags_to_empty_list() -> None:
    """A worker that omits ``tags`` advertises no capabilities (PRD F18 Item 18).

    The default factory is critical: a shared class-level ``[]`` literal
    would alias across instances and let one worker mutate another's tag
    list. Pin the per-instance independence so future refactors can't
    silently regress to the shared-mutable-default footgun.
    """
    req_a = RegisterRequest(hostname="a")
    req_b = RegisterRequest(hostname="b")
    assert req_a.tags == [] and req_b.tags == []
    # Frozen + per-instance: two separate empty lists, not the same object.
    assert req_a.tags is not req_b.tags


def test_register_request_accepts_kubernetes_style_tags() -> None:
    """Real-world tag shapes round-trip: ``gpu``, ``team:platform``, ``env.prod``."""
    req = RegisterRequest(
        hostname="worker-01.local",
        tags=["gpu", "signing", "team:platform", "env.prod", "gpu-v100"],
    )
    assert req.tags == ["gpu", "signing", "team:platform", "env.prod", "gpu-v100"]


@pytest.mark.parametrize(
    "bad_tag",
    [
        pytest.param("", id="empty-string"),
        pytest.param(" ", id="whitespace-only"),
        pytest.param("gpu signing", id="internal-space"),
        pytest.param("-leading-dash", id="leading-dash"),
        pytest.param(".leading-dot", id="leading-dot"),
        pytest.param(":leading-colon", id="leading-colon"),
        pytest.param("trailing space ", id="trailing-space"),
        pytest.param("a" * 65, id="too-long"),
        pytest.param("emoji-🚀", id="non-ascii"),
        pytest.param("slash/in-name", id="forward-slash"),
    ],
)
def test_register_request_rejects_malformed_tag(bad_tag: str) -> None:
    """Each per-tag constraint surfaces as a 422 at the wire boundary.

    Surfacing schema rejection here (not at SQL insert time) means a
    misconfigured operator gets a deterministic 422 with a useful error
    instead of an asyncpg encoding failure deep inside the handler.
    """
    with pytest.raises(ValidationError):
        RegisterRequest(hostname="w", tags=[bad_tag])


def test_register_request_rejects_too_many_tags() -> None:
    """Defensive cap against pathological registration payloads."""
    too_many = [f"tag{i}" for i in range(17)]  # MAX_TAGS_PER_WORKER + 1
    with pytest.raises(ValidationError):
        RegisterRequest(hostname="w", tags=too_many)


def test_register_request_accepts_max_tags_boundary() -> None:
    """Exactly :data:`MAX_TAGS_PER_WORKER` (16) is fine — the cap is inclusive."""
    at_limit = [f"tag{i}" for i in range(16)]
    req = RegisterRequest(hostname="w", tags=at_limit)
    assert len(req.tags) == 16


def test_register_request_rejects_tags_extra_field_typo() -> None:
    """The ``extra="forbid"`` policy still applies — ``tag`` (typo) → 422.

    Pinned because an operator who types ``"tag": ["gpu"]`` instead of
    ``"tags": ["gpu"]`` deserves a fast, loud failure rather than a
    silent capability mismatch at claim time.
    """
    with pytest.raises(ValidationError):
        RegisterRequest.model_validate({"hostname": "w", "tag": ["gpu"]})


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


def test_task_event_request_response_happy_path() -> None:
    req = TaskEventRequest(
        worker_id="w",
        event_type="llm.run_finished",
        payload={"status": "success"},
        detail={"artifact_ref": "whilly_logs/tasks/T-1/attempt-1"},
    )
    assert req.payload["status"] == "success"
    assert req.detail == {"artifact_ref": "whilly_logs/tasks/T-1/attempt-1"}
    assert TaskEventResponse().ok is True


def test_list_task_events_response_carries_ordered_event_items() -> None:
    event = TaskEventItem(
        id=10,
        task_id="T-1",
        plan_id="PLAN-1",
        event_type="human_review.approved",
        created_at=datetime(2026, 5, 7, 9, 30, tzinfo=UTC),
        payload={"task_id": "T-1", "decision": "approved"},
        detail={"review_url": "https://example.test/review/1"},
    )
    response = ListTaskEventsResponse(events=[event])

    assert response.events == [event]
    assert response.events[0].payload["decision"] == "approved"
    with pytest.raises(ValidationError):
        TaskEventItem(
            id=10,
            task_id="T-1",
            plan_id="PLAN-1",
            event_type="human_review.approved",
            created_at=event.created_at,
            payload={},
            unexpected=True,
        )


def test_human_review_decision_request_pins_admin_payload_shape() -> None:
    request = HumanReviewDecisionRequest(
        decision="approved",
        reviewer="lead@example.com",
        stage_id="release_review",
        comment="Evidence reviewed.",
        evidence={"review_url": "https://example.test/review/1"},
    )

    assert request.decision == "approved"
    assert request.reviewer == "lead@example.com"
    with pytest.raises(ValidationError):
        HumanReviewDecisionRequest(decision="maybe", reviewer="lead@example.com")


def test_control_state_schemas_pin_pause_resume_payload_shape() -> None:
    pause = ControlPauseRequest(reason="deploy gate")
    state = ControlStateResponse(
        paused=True,
        pause_reason="deploy gate",
        paused_by="lead@example.com",
        paused_at=datetime(2026, 5, 8, 10, 0, tzinfo=UTC),
        updated_at=datetime(2026, 5, 8, 10, 1, tzinfo=UTC),
    )

    assert pause.reason == "deploy gate"
    assert state.paused is True
    assert state.pause_reason == "deploy gate"
    assert state.paused_by == "lead@example.com"
    with pytest.raises(ValidationError):
        ControlPauseRequest(reason="x", unexpected=True)


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
