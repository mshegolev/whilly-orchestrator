"""Unit tests for :mod:`whilly.core.notifications` — pure domain layer.

The layer is import-pure: only stdlib + ``whilly.core.notifications``.
That keeps the failure surface narrow — these tests fail only when the
event shape, the protocol, or the template-render contract regresses.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timezone
from typing import Any

import pytest

from whilly.core.notifications import (
    MessageTemplate,
    NotificationPort,
    RunCompletedEvent,
)


_BASE_EVENT = RunCompletedEvent(
    plan_id="plan-42",
    worker_id="vm1-deadbeef",
    hostname="vm1",
    iterations=7,
    completed=5,
    failed=1,
    idle_polls=1,
    released_on_shutdown=0,
    duration_s=12.345,
    completed_at=datetime(2026, 5, 5, 12, 0, 0, tzinfo=timezone.utc),
)


def _sample_event(**overrides: Any) -> RunCompletedEvent:
    return replace(_BASE_EVENT, **overrides)


# ─── RunCompletedEvent ───────────────────────────────────────────────────


def test_event_is_frozen() -> None:
    event = _sample_event()
    with pytest.raises(FrozenInstanceError):
        event.completed = 999  # type: ignore[misc]


def test_event_equality_by_value() -> None:
    assert _sample_event() == _sample_event()


def test_event_distinct_when_any_field_differs() -> None:
    assert _sample_event(completed=5) != _sample_event(completed=6)


# ─── MessageTemplate.render ──────────────────────────────────────────────


def test_render_with_default_template_shape() -> None:
    template = MessageTemplate(
        ":white_check_mark: whilly run finished — "
        "plan={plan_id} completed={completed} failed={failed} "
        "iterations={iterations} duration={duration_s:.1f}s "
        "worker={worker_id}@{hostname}"
    )
    rendered = template.render(_sample_event())
    assert "plan=plan-42" in rendered
    assert "completed=5" in rendered
    assert "failed=1" in rendered
    assert "iterations=7" in rendered
    # ``:.1f`` rounds 12.345 → "12.3" (banker's rounding picks down here)
    assert "duration=12.3s" in rendered
    assert "worker=vm1-deadbeef@vm1" in rendered


def test_render_supports_custom_template() -> None:
    rendered = MessageTemplate("done {plan_id}").render(_sample_event(plan_id="abc"))
    assert rendered == "done abc"


def test_render_exposes_completed_at_iso() -> None:
    template = MessageTemplate("at {completed_at_iso}")
    assert template.render(_sample_event()) == "at 2026-05-05T12:00:00+00:00"


# ─── NotificationPort ────────────────────────────────────────────────────


def test_protocol_is_satisfied_structurally() -> None:
    """Anything with ``notify_run_completed(event)`` is a NotificationPort.

    The protocol is the import seam between core and adapters; this test
    pins the duck-typing contract so a future signature change here
    breaks tests *before* it breaks downstream type checks.
    """

    class Recorder:
        def __init__(self) -> None:
            self.events: list[RunCompletedEvent] = []

        def notify_run_completed(self, event: RunCompletedEvent) -> None:
            self.events.append(event)

    port: NotificationPort = Recorder()
    port.notify_run_completed(_sample_event())
    assert len(port.events) == 1  # type: ignore[attr-defined]
