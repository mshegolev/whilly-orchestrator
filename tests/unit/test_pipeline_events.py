from __future__ import annotations

from types import SimpleNamespace

from whilly.pipeline import (
    PIPELINE_STAGE_FAILED,
    PIPELINE_STAGE_SKIPPED,
    PIPELINE_STAGE_STARTED,
    PIPELINE_STAGE_SUCCEEDED,
    PipelineStageContext,
    make_stage_failed_event,
    make_stage_skipped_event,
    make_stage_started_event,
    make_stage_succeeded_event,
    stage_context_from_task,
)


def test_stage_event_constants_match_audit_taxonomy() -> None:
    assert PIPELINE_STAGE_STARTED == "pipeline.stage.started"
    assert PIPELINE_STAGE_SUCCEEDED == "pipeline.stage.succeeded"
    assert PIPELINE_STAGE_FAILED == "pipeline.stage.failed"
    assert PIPELINE_STAGE_SKIPPED == "pipeline.stage.skipped"


def test_started_stage_event_carries_deterministic_stage_snapshot() -> None:
    context = PipelineStageContext(
        task_id="task-123",
        plan_id="plan-abc",
        stage_id="quality-gate",
        project_type="python",
        profile_id="strict",
    )

    event = make_stage_started_event(context)

    assert event is not None
    assert event.task_id == "task-123"
    assert event.event_type == PIPELINE_STAGE_STARTED
    assert event.payload == {
        "task_id": "task-123",
        "plan_id": "plan-abc",
        "stage_id": "quality-gate",
        "project_type": "python",
        "profile_id": "strict",
    }
    assert event.detail is None
    assert event.record_task_event_args() == ("task-123", PIPELINE_STAGE_STARTED, event.payload)
    assert event.record_task_event_kwargs() == {}


def test_stage_event_payload_omits_unavailable_optional_context() -> None:
    context = PipelineStageContext(task_id="task-legacy", stage_id="execute")

    event = make_stage_succeeded_event(context)

    assert event is not None
    assert event.payload == {
        "task_id": "task-legacy",
        "stage_id": "execute",
    }


def test_stage_event_helpers_emit_nothing_without_stage_context() -> None:
    assert make_stage_started_event(None) is None
    assert make_stage_succeeded_event(None) is None
    assert make_stage_failed_event(None, reason="boom") is None
    assert make_stage_skipped_event(None, reason="not-applicable") is None


def test_stage_context_from_task_accepts_raw_configured_step_ids() -> None:
    task = SimpleNamespace(
        id="CFG-001-RELEASE-REVIEW",
        prd_requirement="Configured documentation pipeline step: Release review / QA sign-off",
    )
    plan = SimpleNamespace(
        id="plan-docs",
        origin=SimpleNamespace(system="project_config", ref="docs-profile"),
    )

    context = stage_context_from_task(task, plan)

    assert context == PipelineStageContext(
        task_id="CFG-001-RELEASE-REVIEW",
        plan_id="plan-docs",
        stage_id="Release review / QA sign-off",
        project_type="documentation",
        profile_id="docs-profile",
    )


def test_failed_and_skipped_events_include_reason_and_detail_when_available() -> None:
    context = PipelineStageContext(task_id="task-123", plan_id="plan-abc", stage_id="deploy")

    failed = make_stage_failed_event(context, reason="tests failed", detail={"exit_code": 1})
    skipped = make_stage_skipped_event(context, reason="profile disabled")

    assert failed is not None
    assert failed.event_type == PIPELINE_STAGE_FAILED
    assert failed.payload == {
        "task_id": "task-123",
        "plan_id": "plan-abc",
        "stage_id": "deploy",
        "reason": "tests failed",
    }
    assert failed.detail == {"exit_code": 1}
    assert failed.record_task_event_kwargs() == {"detail": {"exit_code": 1}}

    assert skipped is not None
    assert skipped.event_type == PIPELINE_STAGE_SKIPPED
    assert skipped.payload == {
        "task_id": "task-123",
        "plan_id": "plan-abc",
        "stage_id": "deploy",
        "reason": "profile disabled",
    }
