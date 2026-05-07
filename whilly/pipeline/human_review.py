"""Pure model helpers for human-review pipeline checkpoints."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from whilly.pipeline.events import PipelineTaskEvent

HUMAN_REVIEW_REQUIRED = "human_review.required"
HUMAN_REVIEW_APPROVED = "human_review.approved"
HUMAN_REVIEW_REJECTED = "human_review.rejected"
HUMAN_REVIEW_CHANGES_REQUESTED = "human_review.changes_requested"

_DECISION_APPROVED = "approved"
_DECISION_REJECTED = "rejected"
_DECISION_CHANGES_REQUESTED = "changes_requested"

_HUMAN_REVIEW_CUES = (
    "human approval",
    "human review",
    "human release decision",
    "manual approval",
    "manual review",
    "manual sign-off",
    "manual release sign-off",
    "review and approve",
    "reviewed and approved",
    "reviews and approves",
    "approval or requested changes",
)


@dataclass(frozen=True, slots=True)
class HumanReviewCheckpoint:
    """Auditable requirement for a human decision before a pipeline stage proceeds."""

    task_id: str
    plan_id: str = ""
    stage_id: str = ""
    approval_channel: str = ""
    required_steps: tuple[str, ...] = ()
    reason: str = ""
    source: str = ""


@dataclass(frozen=True, slots=True)
class HumanReviewDecision:
    """Human decision evidence captured as data, independent of task status."""

    reviewer: str
    comment: str = ""
    evidence: Mapping[str, Any] | None = None
    requested_changes: tuple[str, ...] = ()


def requires_human_review(
    *,
    stage: Any | None = None,
    task: Any | None = None,
    human_loop: Any | None = None,
) -> bool:
    """Return whether a configured stage or task text requires human review."""

    return _human_review_requirement(stage=stage, task=task, human_loop=human_loop) is not None


def build_human_review_checkpoint(
    *,
    task: Any,
    stage: Any | None = None,
    human_loop: Any | None = None,
    plan_id: str = "",
) -> HumanReviewCheckpoint | None:
    """Build checkpoint requirement data, or ``None`` when review is not required."""

    requirement = _human_review_requirement(stage=stage, task=task, human_loop=human_loop)
    if requirement is None:
        return None

    task_id = _string_field(task, "id")
    stage_id = _string_field(stage, "id") or _string_field(stage, "stage_id")
    return HumanReviewCheckpoint(
        task_id=task_id,
        plan_id=plan_id,
        stage_id=stage_id,
        approval_channel=_string_field(human_loop, "approval_channel"),
        required_steps=_tuple_field(human_loop, "required_steps"),
        reason=requirement["reason"],
        source=requirement["source"],
    )


def make_human_review_required_event(checkpoint: HumanReviewCheckpoint) -> PipelineTaskEvent:
    """Build a record-ready ``human_review.required`` event."""

    return PipelineTaskEvent(
        task_id=checkpoint.task_id,
        event_type=HUMAN_REVIEW_REQUIRED,
        payload=_checkpoint_payload(checkpoint),
    )


def make_human_review_approved_event(
    checkpoint: HumanReviewCheckpoint,
    decision: HumanReviewDecision,
) -> PipelineTaskEvent:
    """Build a record-ready approval event with auditable evidence."""

    return _make_decision_event(HUMAN_REVIEW_APPROVED, _DECISION_APPROVED, checkpoint, decision)


def make_human_review_rejected_event(
    checkpoint: HumanReviewCheckpoint,
    decision: HumanReviewDecision,
) -> PipelineTaskEvent:
    """Build a record-ready rejection event with auditable evidence."""

    return _make_decision_event(HUMAN_REVIEW_REJECTED, _DECISION_REJECTED, checkpoint, decision)


def make_human_review_changes_requested_event(
    checkpoint: HumanReviewCheckpoint,
    decision: HumanReviewDecision,
) -> PipelineTaskEvent:
    """Build a record-ready changes-requested event with auditable evidence."""

    return _make_decision_event(
        HUMAN_REVIEW_CHANGES_REQUESTED,
        _DECISION_CHANGES_REQUESTED,
        checkpoint,
        decision,
    )


def _make_decision_event(
    event_type: str,
    decision_value: str,
    checkpoint: HumanReviewCheckpoint,
    decision: HumanReviewDecision,
) -> PipelineTaskEvent:
    payload = _checkpoint_identity_payload(checkpoint)
    payload["decision"] = decision_value
    payload["reviewer"] = decision.reviewer
    _put_if_available(payload, "approval_channel", checkpoint.approval_channel)
    _put_if_available(payload, "comment", decision.comment)
    if decision.evidence:
        payload["evidence"] = dict(decision.evidence)
    if decision.requested_changes:
        payload["requested_changes"] = list(decision.requested_changes)
    return PipelineTaskEvent(task_id=checkpoint.task_id, event_type=event_type, payload=payload)


def _checkpoint_payload(checkpoint: HumanReviewCheckpoint) -> dict[str, Any]:
    payload = _checkpoint_identity_payload(checkpoint)
    _put_if_available(payload, "approval_channel", checkpoint.approval_channel)
    if checkpoint.required_steps:
        payload["required_steps"] = list(checkpoint.required_steps)
    _put_if_available(payload, "reason", checkpoint.reason)
    _put_if_available(payload, "source", checkpoint.source)
    return payload


def _checkpoint_identity_payload(checkpoint: HumanReviewCheckpoint) -> dict[str, Any]:
    payload: dict[str, Any] = {"task_id": checkpoint.task_id}
    _put_if_available(payload, "plan_id", checkpoint.plan_id)
    _put_if_available(payload, "stage_id", checkpoint.stage_id)
    return payload


def _human_review_requirement(
    *,
    stage: Any | None,
    task: Any | None,
    human_loop: Any | None,
) -> dict[str, str] | None:
    if human_loop is not None and not bool(_field(human_loop, "enabled", True)):
        return None

    if bool(_field(stage, "human_gate", False)):
        return {"reason": "stage_human_gate", "source": "pipeline_step"}

    stage_id = _string_field(stage, "id") or _string_field(stage, "stage_id")
    if stage_id and stage_id in set(_tuple_field(human_loop, "required_steps")):
        return {"reason": "human_loop_required_step", "source": "human_loop"}

    if _contains_human_review_cue(_review_texts(task)):
        return {"reason": "task_review_text", "source": "task_text"}
    if _contains_human_review_cue(_review_texts(stage)):
        return {"reason": "stage_review_text", "source": "pipeline_step"}

    return None


def _review_texts(value: Any | None) -> tuple[str, ...]:
    texts: list[str] = []
    for field_name in ("acceptance_criteria", "test_steps"):
        texts.extend(str(item) for item in _tuple_field(value, field_name) if str(item).strip())
    return tuple(texts)


def _contains_human_review_cue(texts: Iterable[str]) -> bool:
    surface = "\n".join(texts).casefold()
    return bool(surface and any(cue in surface for cue in _HUMAN_REVIEW_CUES))


def _field(value: Any | None, name: str, default: Any = None) -> Any:
    if value is None:
        return default
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _string_field(value: Any | None, name: str) -> str:
    raw = _field(value, name, "")
    if raw is None:
        return ""
    return str(raw)


def _tuple_field(value: Any | None, name: str) -> tuple[str, ...]:
    raw = _field(value, name, ())
    if raw is None:
        return ()
    if isinstance(raw, str):
        return (raw,)
    return tuple(str(item) for item in raw)


def _put_if_available(payload: dict[str, Any], key: str, value: str) -> None:
    if value:
        payload[key] = value


__all__ = [
    "HUMAN_REVIEW_APPROVED",
    "HUMAN_REVIEW_CHANGES_REQUESTED",
    "HUMAN_REVIEW_REJECTED",
    "HUMAN_REVIEW_REQUIRED",
    "HumanReviewCheckpoint",
    "HumanReviewDecision",
    "build_human_review_checkpoint",
    "make_human_review_approved_event",
    "make_human_review_changes_requested_event",
    "make_human_review_rejected_event",
    "make_human_review_required_event",
    "requires_human_review",
]
