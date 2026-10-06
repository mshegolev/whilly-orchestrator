from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from whilly.swarm.learning.schedules import (
    BackgroundAdmission,
    ResearchRun,
    RunLimits,
    Schedule,
    Scheduler,
    ScheduleStore,
)


def _schedule(**overrides) -> Schedule:
    values = {
        "id": "daily",
        "timezone": "Europe/Berlin",
        "run_window": "09:00-10:00",
        "enabled": True,
        "model_profiles": {"research": "private-cheap"},
        "limits": RunLimits(max_calls=2, max_seconds=120, max_documents=10, max_bytes=1000),
        "source_policy": {"https_only": True, "allowed_hosts": ["example.com"]},
        "retention_policy": {"documents_days": 30, "events_days": 30},
    }
    values.update(overrides)
    return Schedule(**values)


class MemoryScheduleStore(ScheduleStore):
    def __init__(self) -> None:
        self.claimed: set[tuple[str, str, str]] = set()

    async def claim_due(self, schedule, *, window_date, window_key, now):
        identity = (schedule.id, window_date.isoformat(), window_key)
        if identity in self.claimed:
            return None
        self.claimed.add(identity)
        return ResearchRun(
            id=f"run-{len(self.claimed)}",
            product_id="product-a",
            schedule_id=schedule.id,
            window_date=window_date,
            window_key=window_key,
            status="running",
            blocker=None,
        )


@pytest.mark.asyncio
async def test_two_schedulers_one_run_and_missed_window_is_not_replayed() -> None:
    store = MemoryScheduleStore()
    first = Scheduler([_schedule()], store)
    second = Scheduler([_schedule()], store)

    now = datetime(2026, 9, 29, 9, 30, tzinfo=ZoneInfo("Europe/Berlin"))
    run = await first.claim_due(now)
    duplicate = await second.claim_due(now)
    missed = await first.claim_due(datetime(2026, 9, 29, 12, 0, tzinfo=ZoneInfo("Europe/Berlin")))

    assert run is not None
    assert duplicate is None
    assert missed is None


@pytest.mark.asyncio
async def test_dst_fold_uses_one_local_window_identity() -> None:
    store = MemoryScheduleStore()
    scheduler = Scheduler([_schedule(timezone="America/New_York", run_window="01:00-02:00")], store)
    first_fold = datetime(2026, 11, 1, 1, 30, tzinfo=ZoneInfo("America/New_York"), fold=0)
    second_fold = datetime(2026, 11, 1, 1, 30, tzinfo=ZoneInfo("America/New_York"), fold=1)

    first = await scheduler.claim_due(first_fold)
    second = await scheduler.claim_due(second_fold)

    assert first is not None
    assert second is None
    assert first.window_key == "01:00-02:00"


def test_invalid_zone_window_and_incomplete_enabled_config_fail_closed() -> None:
    with pytest.raises(ValueError, match="timezone"):
        _schedule(timezone="Not/AZone")
    with pytest.raises(ValueError, match="run_window"):
        _schedule(run_window="10:00-09:00")
    with pytest.raises(ValueError, match="model_profiles"):
        _schedule(model_profiles={})
    with pytest.raises(ValueError, match="source_policy"):
        _schedule(source_policy={"allowed_hosts": ["example.com"]})
    with pytest.raises(ValueError, match="retention_policy"):
        _schedule(retention_policy={"documents_days": 0})

    disabled = _schedule(enabled=False, model_profiles={}, source_policy={}, retention_policy={})
    assert disabled.activation_blockers()


def test_background_admission_requires_explicit_interactive_reservation() -> None:
    admission = BackgroundAdmission(product_id="product-a", run_id="run-1", reserved_interactive_slots=2)
    assert admission.reserved_interactive_slots == 2
    with pytest.raises(ValueError, match="reserved_interactive_slots"):
        BackgroundAdmission(product_id="product-a", run_id="run-1", reserved_interactive_slots=0)
    with pytest.raises(ValueError, match="reserved_interactive_slots"):
        BackgroundAdmission(product_id="product-a", run_id="run-1", reserved_interactive_slots=True)
    with pytest.raises(ValueError, match="reserved_interactive_slots"):
        BackgroundAdmission(product_id="product-a", run_id="run-1", reserved_interactive_slots=1.0)
