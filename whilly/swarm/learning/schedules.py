"""Pure scheduling policies and value objects for disabled-by-default research runs."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time
from re import fullmatch
from types import MappingProxyType
from typing import Mapping, Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def _non_empty_text(value: str, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be non-empty")


@dataclass(frozen=True)
class RunLimits:
    """Hard per-run limits; seconds are conservatively reserved before launch."""

    max_calls: int
    max_seconds: int
    max_documents: int
    max_bytes: int

    def __post_init__(self) -> None:
        for name in ("max_calls", "max_seconds", "max_documents", "max_bytes"):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")


@dataclass(frozen=True)
class Schedule:
    """A validated daily local-time window and its explicit bounded policies."""

    id: str
    timezone: str
    run_window: str
    enabled: bool
    model_profiles: Mapping[str, str]
    limits: RunLimits
    source_policy: Mapping[str, object]
    retention_policy: Mapping[str, object]

    def __post_init__(self) -> None:
        _non_empty_text(self.id, "id")
        _non_empty_text(self.timezone, "timezone")
        try:
            ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("timezone is not a valid IANA ZoneInfo name") from exc
        _parse_window(self.run_window)
        if not isinstance(self.enabled, bool):
            raise ValueError("enabled must be boolean")
        if not isinstance(self.model_profiles, Mapping):
            raise ValueError("model_profiles must be an explicit mapping")
        if not isinstance(self.limits, RunLimits):
            raise ValueError("limits must be RunLimits")
        if not isinstance(self.source_policy, Mapping):
            raise ValueError("source_policy must be an explicit mapping")
        if not isinstance(self.retention_policy, Mapping):
            raise ValueError("retention_policy must be an explicit mapping")
        if self.source_policy and not _valid_source_policy(self.source_policy):
            raise ValueError("source_policy must explicitly allow bounded HTTPS hosts")
        if self.retention_policy and not _valid_retention_policy(self.retention_policy):
            raise ValueError("retention_policy must contain positive day limits")
        if self.enabled and self.activation_blockers():
            raise ValueError(f"schedule configuration is incomplete: {self.activation_blockers()[0]}")
        object.__setattr__(self, "model_profiles", _freeze(self.model_profiles))
        object.__setattr__(self, "source_policy", _freeze(self.source_policy))
        object.__setattr__(self, "retention_policy", _freeze(self.retention_policy))

    def activation_blockers(self) -> tuple[str, ...]:
        blockers: list[str] = []
        if not self.model_profiles:
            blockers.append("missing_model_profiles")
        elif any(
            not isinstance(key, str) or not key.strip() or not isinstance(value, str) or not value.strip()
            for key, value in self.model_profiles.items()
        ):
            blockers.append("invalid_model_profiles")
        if not self.source_policy:
            blockers.append("missing_source_policy")
        if not self.retention_policy:
            blockers.append("missing_retention_policy")
        return tuple(blockers)


@dataclass(frozen=True)
class ResearchRun:
    """Durable run identity returned after one schedule window is claimed."""

    id: str
    product_id: str
    schedule_id: str
    window_date: date
    window_key: str
    status: str
    blocker: str | None


@dataclass(frozen=True)
class BackgroundAdmission:
    """Controller-provided interactive capacity reservation for background work."""

    product_id: str
    run_id: str
    reserved_interactive_slots: int

    def __post_init__(self) -> None:
        _non_empty_text(self.product_id, "product_id")
        _non_empty_text(self.run_id, "run_id")
        if (
            isinstance(self.reserved_interactive_slots, bool)
            or not isinstance(self.reserved_interactive_slots, int)
            or self.reserved_interactive_slots not in range(1, 5)
        ):
            raise ValueError("reserved_interactive_slots must be between 1 and 4")


class ScheduleStore(Protocol):
    async def claim_due(
        self, schedule: Schedule, *, window_date: date, window_key: str, now: datetime
    ) -> ResearchRun | None: ...


class Scheduler:
    """Apply pure window policy, leaving durable uniqueness and leases to a port."""

    def __init__(self, schedules: list[Schedule] | tuple[Schedule, ...], store: ScheduleStore) -> None:
        self._schedules = tuple(schedules)
        self._store = store

    async def claim_due(self, now: datetime) -> ResearchRun | None:
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("now must be timezone-aware")
        for schedule in self._schedules:
            if not schedule.enabled or schedule.activation_blockers():
                continue
            local = now.astimezone(ZoneInfo(schedule.timezone))
            start, end = _parse_window(schedule.run_window)
            if not (start <= local.timetz().replace(tzinfo=None) < end):
                continue
            claimed = await self._store.claim_due(
                schedule,
                window_date=local.date(),
                window_key=schedule.run_window,
                now=now,
            )
            if claimed is not None:
                return claimed
        return None


def _parse_window(value: str) -> tuple[time, time]:
    if not isinstance(value, str):
        raise ValueError("run_window must use HH:MM-HH:MM")
    match = fullmatch(r"([01]\d|2[0-3]):([0-5]\d)-([01]\d|2[0-3]):([0-5]\d)", value)
    if match is None:
        raise ValueError("run_window must use HH:MM-HH:MM")
    start = time(int(match.group(1)), int(match.group(2)))
    end = time(int(match.group(3)), int(match.group(4)))
    if start >= end:
        raise ValueError("run_window must be a same-day interval with start before end")
    return start, end


def _valid_source_policy(policy: Mapping[str, object]) -> bool:
    if not policy or policy.get("https_only") is not True:
        return False
    hosts = policy.get("allowed_hosts")
    if (
        not isinstance(hosts, (list, tuple))
        or not hosts
        or any(not isinstance(host, str) or not host.strip() for host in hosts)
    ):
        return False
    for key in ("max_bytes", "timeout_seconds", "max_redirects"):
        if key in policy and (isinstance(policy[key], bool) or not isinstance(policy[key], int) or policy[key] <= 0):
            return False
    return True


def _valid_retention_policy(policy: Mapping[str, object]) -> bool:
    if not policy:
        return False
    return all(
        isinstance(key, str)
        and bool(key.strip())
        and isinstance(value, int)
        and not isinstance(value, bool)
        and value > 0
        for key, value in policy.items()
    )


def _freeze(value: object) -> object:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, tuple):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, set):
        return frozenset(_freeze(item) for item in value)
    return value


__all__ = [
    "BackgroundAdmission",
    "ResearchRun",
    "RunLimits",
    "Schedule",
    "ScheduleStore",
    "Scheduler",
]
