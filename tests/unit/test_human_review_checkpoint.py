from __future__ import annotations

from whilly.core.models import Task, TaskStatus
from whilly.pipeline.human_review import (
    HUMAN_REVIEW_APPROVED,
    HUMAN_REVIEW_CHANGES_REQUESTED,
    HUMAN_REVIEW_REJECTED,
    HUMAN_REVIEW_REQUIRED,
    HumanReviewCheckpoint,
    HumanReviewDecision,
    build_human_review_checkpoint,
    make_human_review_approved_event,
    make_human_review_changes_requested_event,
    make_human_review_rejected_event,
    make_human_review_required_event,
    requires_human_review,
)
from whilly.project_config.models import HumanLoopConfig, PipelineStepConfig


def test_human_review_event_constants_match_checkpoint_taxonomy() -> None:
    assert HUMAN_REVIEW_REQUIRED == "human_review.required"
    assert HUMAN_REVIEW_APPROVED == "human_review.approved"
    assert HUMAN_REVIEW_REJECTED == "human_review.rejected"
    assert HUMAN_REVIEW_CHANGES_REQUESTED == "human_review.changes_requested"


def test_required_event_payload_carries_auditable_checkpoint_context() -> None:
    checkpoint = HumanReviewCheckpoint(
        task_id="task-123",
        plan_id="plan-abc",
        stage_id="build-qa-test-plan",
        approval_channel="#qa-approvals",
        required_steps=("build-qa-test-plan", "release-decision"),
        reason="stage_human_gate",
        source="pipeline_step",
    )

    event = make_human_review_required_event(checkpoint)

    assert event.task_id == "task-123"
    assert event.event_type == HUMAN_REVIEW_REQUIRED
    assert event.payload == {
        "task_id": "task-123",
        "plan_id": "plan-abc",
        "stage_id": "build-qa-test-plan",
        "approval_channel": "#qa-approvals",
        "required_steps": ["build-qa-test-plan", "release-decision"],
        "reason": "stage_human_gate",
        "source": "pipeline_step",
    }
    assert event.detail is None
    assert event.record_task_event_args() == ("task-123", HUMAN_REVIEW_REQUIRED, event.payload)
    assert event.record_task_event_kwargs() == {}


def test_decision_events_store_approval_evidence_without_task_status_change() -> None:
    checkpoint = HumanReviewCheckpoint(task_id="task-123", plan_id="plan-abc", stage_id="release-decision")
    decision = HumanReviewDecision(
        reviewer="qa@example.com",
        comment="Coverage and evidence reviewed.",
        evidence={"review_url": "https://example.test/reviews/42", "commit": "abc123"},
    )

    approved = make_human_review_approved_event(checkpoint, decision)
    rejected = make_human_review_rejected_event(checkpoint, decision)
    changes = make_human_review_changes_requested_event(
        checkpoint,
        HumanReviewDecision(
            reviewer="qa@example.com",
            comment="Add regression evidence.",
            requested_changes=("Attach regression run",),
        ),
    )

    assert approved.event_type == HUMAN_REVIEW_APPROVED
    assert approved.payload == {
        "task_id": "task-123",
        "plan_id": "plan-abc",
        "stage_id": "release-decision",
        "decision": "approved",
        "reviewer": "qa@example.com",
        "comment": "Coverage and evidence reviewed.",
        "evidence": {"review_url": "https://example.test/reviews/42", "commit": "abc123"},
    }
    assert "status" not in approved.payload
    assert "task_status" not in approved.payload

    assert rejected.event_type == HUMAN_REVIEW_REJECTED
    assert rejected.payload["decision"] == "rejected"

    assert changes.event_type == HUMAN_REVIEW_CHANGES_REQUESTED
    assert changes.payload["decision"] == "changes_requested"
    assert changes.payload["requested_changes"] == ["Attach regression run"]


def test_predicate_requires_review_from_configured_stage_gate_or_required_steps() -> None:
    human_loop = HumanLoopConfig(
        enabled=True,
        approval_channel="#release-review",
        required_steps=("release-decision",),
    )
    gated = PipelineStepConfig(
        id="build-qa-test-plan",
        kind="qa_test_plan",
        title="Build QA/STLC test plan",
        human_gate=True,
    )
    required_by_policy = PipelineStepConfig(id="release-decision", kind="decision", title="Release decision")

    assert requires_human_review(stage=gated, human_loop=human_loop)
    assert requires_human_review(stage=required_by_policy, human_loop=human_loop)
    assert not requires_human_review(
        stage=PipelineStepConfig(id="run-tests", kind="test_execution", title="Run tests"),
        human_loop=human_loop,
    )
    assert not requires_human_review(stage=gated, human_loop=HumanLoopConfig(enabled=False))


def test_predicate_detects_human_review_cues_in_task_acceptance_or_test_text() -> None:
    plain_task = Task(id="plain", status=TaskStatus.PENDING, acceptance_criteria=("Automated tests pass.",))
    approval_task = Task(
        id="approval",
        status=TaskStatus.PENDING,
        acceptance_criteria=("QA engineer reviews and approves the generated test plan before implementation.",),
    )
    signoff_task = Task(
        id="signoff",
        status=TaskStatus.PENDING,
        test_steps=("Capture manual release sign-off with evidence.",),
    )

    assert not requires_human_review(task=plain_task)
    assert requires_human_review(task=approval_task)
    assert requires_human_review(task=signoff_task)
    assert approval_task.status is TaskStatus.PENDING


def test_build_checkpoint_returns_requirement_data_when_review_is_needed() -> None:
    stage = PipelineStepConfig(id="release-decision", kind="human_gate", title="Release decision", human_gate=True)
    human_loop = HumanLoopConfig(enabled=True, approval_channel="#release-review", required_steps=("release-decision",))
    task = Task(id="task-123", status=TaskStatus.IN_PROGRESS)

    checkpoint = build_human_review_checkpoint(
        task=task,
        stage=stage,
        human_loop=human_loop,
        plan_id="plan-abc",
    )

    assert checkpoint == HumanReviewCheckpoint(
        task_id="task-123",
        plan_id="plan-abc",
        stage_id="release-decision",
        approval_channel="#release-review",
        required_steps=("release-decision",),
        reason="stage_human_gate",
        source="pipeline_step",
    )
