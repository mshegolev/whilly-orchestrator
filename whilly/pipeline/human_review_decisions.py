"""Shared command path for operator human-review decisions."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from whilly.pipeline.human_review import (
    HUMAN_REVIEW_APPROVED,
    HUMAN_REVIEW_CHANGES_REQUESTED,
    HUMAN_REVIEW_REJECTED,
)

HumanReviewDecisionValue = Literal["approved", "rejected", "changes_requested"]

HUMAN_REVIEW_DECISION_EVENT_TYPES: dict[HumanReviewDecisionValue, str] = {
    "approved": HUMAN_REVIEW_APPROVED,
    "rejected": HUMAN_REVIEW_REJECTED,
    "changes_requested": HUMAN_REVIEW_CHANGES_REQUESTED,
}


class HumanReviewDecisionRecorder(Protocol):
    async def record_task_event(self, task_id: str, event_type: str, payload: dict[str, Any]) -> None: ...


@dataclass(frozen=True, slots=True)
class HumanReviewDecisionCommand:
    """Surface-neutral request to record a human-review decision."""

    task_id: str
    decision: HumanReviewDecisionValue
    reviewer: str
    source: str
    stage_id: str = ""
    comment: str = ""
    evidence: Mapping[str, Any] | None = None
    requested_changes: tuple[str, ...] = ()
    operator: str = ""


async def record_human_review_decision(
    recorder: HumanReviewDecisionRecorder,
    command: HumanReviewDecisionCommand,
) -> None:
    """Record one human-review decision through the shared audit payload contract."""

    event_type = HUMAN_REVIEW_DECISION_EVENT_TYPES[command.decision]
    payload: dict[str, Any] = {
        "task_id": command.task_id,
        "decision": command.decision,
        "reviewer": command.reviewer,
        "source": command.source,
    }
    _put_if_non_empty(payload, "stage_id", command.stage_id)
    _put_if_non_empty(payload, "comment", command.comment)
    _put_if_non_empty(payload, "operator", command.operator)
    if command.evidence:
        payload["evidence"] = dict(command.evidence)
    if command.requested_changes:
        payload["requested_changes"] = list(command.requested_changes)
    await recorder.record_task_event(command.task_id, event_type, payload)


def _put_if_non_empty(payload: dict[str, Any], key: str, value: str | None) -> None:
    if value:
        payload[key] = value
