from __future__ import annotations

import pytest

from whilly.swarm.learning.research import DailyReport, ResearchController, ResearchBlocked, RetrospectiveService


class MemoryReportStore:
    def __init__(self) -> None:
        self.reports: dict[str, DailyReport] = {}
        self.stopped: set[str] = set()
        self.fail_save = False

    async def save_report(self, report: DailyReport) -> None:
        if self.fail_save:
            raise RuntimeError("database unavailable")
        self.reports[report.run_id] = report

    async def get_report(self, run_id: str) -> DailyReport | None:
        return self.reports.get(run_id)

    async def stop(self, run_id: str) -> bool:
        self.stopped.add(run_id)
        return True

    async def is_stopped(self, run_id: str) -> bool:
        return run_id in self.stopped


@pytest.mark.asyncio
async def test_report_survives_service_restart() -> None:
    store = MemoryReportStore()
    first = ResearchController(store, RetrospectiveService())
    await first.run_fixture("run-1", [{"kind": "task_outcome", "outcome": "completed", "cost": 1.5}])

    restored = await ResearchController(store, RetrospectiveService()).report("run-1")

    assert restored is not None
    assert restored.run_id == "run-1"
    assert restored.known_cost == 1.5


@pytest.mark.asyncio
async def test_stop_blocks_new_calls() -> None:
    store = MemoryReportStore()
    controller = ResearchController(store, RetrospectiveService())
    await controller.stop("run-2")

    with pytest.raises(ResearchBlocked, match="stop_requested"):
        await controller.run_fixture("run-2", [{"kind": "task_outcome", "outcome": "completed"}])


@pytest.mark.asyncio
async def test_partial_failure_visible_when_report_cannot_be_saved() -> None:
    store = MemoryReportStore()
    store.fail_save = True

    result = await ResearchController(store, RetrospectiveService()).run_fixture(
        "run-3", [{"kind": "task_outcome", "outcome": "blocked"}]
    )

    assert result.outcome == "partial_failure"
    assert result.blocker == "report_persistence_failed"


@pytest.mark.asyncio
async def test_unredacted_query_denied() -> None:
    controller = ResearchController(MemoryReportStore(), RetrospectiveService())

    with pytest.raises(ResearchBlocked, match="unredacted_query_denied"):
        await controller.run_fixture("run-4", [{"kind": "research_query", "raw_query": "INC000012345678"}])
