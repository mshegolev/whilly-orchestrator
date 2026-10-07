"""Pure contracts for bounded research and evidence-based retrospectives."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Mapping, Protocol


class ResearchBlocked(RuntimeError):
    """A named policy boundary prevented research I/O or interpretation."""


@dataclass(frozen=True)
class FetchPolicy:
    allowed_hosts: tuple[str, ...]
    max_redirects: int
    max_bytes: int
    timeout_seconds: int

    def __post_init__(self) -> None:
        if not self.allowed_hosts or any(not isinstance(host, str) or not host.strip() for host in self.allowed_hosts):
            raise ValueError("allowed_hosts must contain non-empty hostnames")
        for name in ("max_redirects", "max_bytes", "timeout_seconds"):
            value = getattr(self, name)
            minimum = 0 if name == "max_redirects" else 1
            if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
                raise ValueError(f"{name} is invalid")


@dataclass(frozen=True)
class FetchResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes

    def __post_init__(self) -> None:
        object.__setattr__(self, "headers", MappingProxyType({str(k).lower(): str(v) for k, v in self.headers.items()}))


@dataclass(frozen=True)
class ResearchDocument:
    uri: str
    retrieved_at: datetime
    media_type: str
    body: bytes


@dataclass(frozen=True)
class ReportSource:
    uri: str
    source_date: str | None
    retrieved_at: str


@dataclass(frozen=True)
class DailyReport:
    run_id: str
    outcome: str
    generated_at: datetime
    sources: tuple[ReportSource, ...]
    observations: tuple[str, ...]
    sample_size: int
    missing_data: tuple[str, ...]
    hypotheses: tuple[str, ...]
    proposals: tuple[str, ...]
    latency_ms: int | None
    known_cost: float
    unknown_cost_count: int
    policy_actions: tuple[str, ...] = ()


class ResearchNetwork(Protocol):
    async def resolve(self, host: str) -> tuple[str, ...]: ...

    async def request(self, url: str, *, connect_ip: str, host: str, timeout_seconds: int) -> FetchResponse: ...


class ReportStore(Protocol):
    async def save_report(self, report: DailyReport) -> None: ...

    async def get_report(self, run_id: str) -> DailyReport | None: ...

    async def stop(self, run_id: str) -> bool: ...

    async def is_stopped(self, run_id: str) -> bool: ...


@dataclass(frozen=True)
class ResearchRunResult:
    outcome: str
    blocker: str | None
    report: DailyReport | None


class ResearchController:
    """Manual fixture-only controller; provider execution is composed elsewhere."""

    def __init__(self, store: ReportStore, retrospective: RetrospectiveService) -> None:
        self._store = store
        self._retrospective = retrospective

    async def run_fixture(self, run_id: str, events: list[dict]) -> ResearchRunResult:
        if await self._store.is_stopped(run_id):
            raise ResearchBlocked("stop_requested")
        if any(event.get("raw_query") is not None for event in events):
            raise ResearchBlocked("unredacted_query_denied")
        report = self._retrospective.analyze(run_id, events)
        try:
            await self._store.save_report(report)
        except Exception:
            return ResearchRunResult("partial_failure", "report_persistence_failed", None)
        return ResearchRunResult(report.outcome, None, report)

    async def report(self, run_id: str) -> DailyReport | None:
        return await self._store.get_report(run_id)

    async def stop(self, run_id: str) -> bool:
        return await self._store.stop(run_id)


class RetrospectiveService:
    """Summarize structured events without treating retrieved text as authority."""

    def analyze(self, run_id: str, events: list[dict]) -> DailyReport:
        now = datetime.now(timezone.utc)
        if not events:
            return DailyReport(
                run_id=run_id,
                outcome="no_data",
                generated_at=now,
                sources=(),
                observations=(),
                sample_size=0,
                missing_data=("authorized_structured_events",),
                hypotheses=(),
                proposals=(),
                latency_ms=None,
                known_cost=0,
                unknown_cost_count=0,
            )
        sources = tuple(
            ReportSource(str(event["source"]), event.get("source_date"), str(event["retrieved_at"]))
            for event in events
            if event.get("kind") == "research_document" and event.get("source") and event.get("retrieved_at")
        )
        outcomes = [event for event in events if event.get("kind") == "task_outcome"]
        blocked = sum(event.get("outcome") == "blocked" for event in outcomes)
        costs = [event.get("cost") for event in outcomes]
        known_cost = sum(float(cost) for cost in costs if isinstance(cost, (int, float)) and not isinstance(cost, bool))
        latencies = [event.get("latency_ms") for event in outcomes if isinstance(event.get("latency_ms"), int)]
        observations = (f"blocked outcomes: {blocked}/{len(outcomes)}",) if outcomes else ()
        proposals = ("review repeated blocked outcomes",) if blocked else ()
        return DailyReport(
            run_id=run_id,
            outcome="observed",
            generated_at=now,
            sources=sources,
            observations=observations,
            sample_size=len(events),
            missing_data=() if outcomes else ("task_outcomes",),
            hypotheses=("blocked outcomes may indicate a recurring setup gap",) if blocked else (),
            proposals=proposals,
            latency_ms=sum(latencies) if latencies else None,
            known_cost=known_cost,
            unknown_cost_count=sum(cost is None for cost in costs),
        )


__all__ = [
    "DailyReport",
    "FetchPolicy",
    "FetchResponse",
    "ReportSource",
    "ResearchBlocked",
    "ResearchController",
    "ResearchDocument",
    "ResearchNetwork",
    "ResearchRunResult",
    "RetrospectiveService",
]
