from __future__ import annotations

from typing import Any

import pytest

from whilly.pipeline.human_review import (
    HUMAN_REVIEW_APPROVED,
    HUMAN_REVIEW_CHANGES_REQUESTED,
)
from whilly.pipeline.human_review_decisions import HumanReviewDecisionCommand, record_human_review_decision


class RecordingRepo:
    def __init__(self) -> None:
        self.events: list[tuple[str, str, dict[str, Any]]] = []

    async def record_task_event(self, task_id: str, event_type: str, payload: dict[str, Any]) -> None:
        self.events.append((task_id, event_type, payload))


@pytest.mark.asyncio
async def test_record_human_review_decision_maps_admin_command_to_event_payload() -> None:
    repo = RecordingRepo()

    await record_human_review_decision(
        repo,
        HumanReviewDecisionCommand(
            task_id="TASK-1",
            decision="approved",
            reviewer="lead@example.com",
            source="admin_api",
            stage_id="release_review",
            comment="Evidence reviewed.",
            evidence={"review_url": "https://example.test/reviews/42"},
            operator="admin@example.com",
        ),
    )

    assert repo.events == [
        (
            "TASK-1",
            HUMAN_REVIEW_APPROVED,
            {
                "task_id": "TASK-1",
                "decision": "approved",
                "reviewer": "lead@example.com",
                "source": "admin_api",
                "stage_id": "release_review",
                "comment": "Evidence reviewed.",
                "operator": "admin@example.com",
                "evidence": {"review_url": "https://example.test/reviews/42"},
            },
        )
    ]


@pytest.mark.asyncio
async def test_record_human_review_decision_maps_tui_changes_request_to_same_payload_contract() -> None:
    repo = RecordingRepo()

    await record_human_review_decision(
        repo,
        HumanReviewDecisionCommand(
            task_id="TASK-2",
            decision="changes_requested",
            reviewer="lead@example.com",
            source="tui",
            stage_id="release_review",
            requested_changes=("Requested from TUI operator controls.",),
        ),
    )

    assert repo.events == [
        (
            "TASK-2",
            HUMAN_REVIEW_CHANGES_REQUESTED,
            {
                "task_id": "TASK-2",
                "decision": "changes_requested",
                "reviewer": "lead@example.com",
                "source": "tui",
                "stage_id": "release_review",
                "requested_changes": ["Requested from TUI operator controls."],
            },
        )
    ]
