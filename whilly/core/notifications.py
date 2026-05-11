"""Outbound notifications — domain layer.

Pure data + protocol; no I/O, no env reads, no third-party imports.
Concrete delivery (Slack, voice, …) lives in :mod:`whilly.adapters.notifications`
and is wired in by the composition root in :mod:`whilly.cli.run`.

The shape mirrors the rest of :mod:`whilly.core`: frozen dataclasses for
value-object semantics, a ``Protocol`` for the outbound port so callers
type-check against the interface rather than a concrete adapter.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Protocol


@dataclass(frozen=True)
class RunCompletedEvent:
    """Snapshot of one ``whilly run`` invocation at the moment it finished.

    Fields are 1:1 with the stderr summary printed by
    :func:`whilly.cli.run.run_run_command` plus the counters carried by
    :class:`whilly.worker.local.WorkerStats`. Frozen so it can travel
    through the notifier port without defensive copies.
    """

    plan_id: str
    worker_id: str
    hostname: str
    iterations: int
    completed: int
    failed: int
    idle_polls: int
    released_on_shutdown: int
    duration_s: float
    completed_at: datetime


class NotificationPort(Protocol):
    """Outbound notification port (Hexagonal architecture).

    Implementations must be best-effort: a transport failure must never
    raise out of the call site — observability is logging's job, the
    orchestrator's exit code stays clean. See
    :class:`whilly.adapters.notifications.slack.SlackNotifier` for the
    canonical impl and :class:`whilly.adapters.notifications.null.NullNotifier`
    for the disabled-feature case.
    """

    def notify_run_completed(self, event: RunCompletedEvent) -> None: ...


@dataclass(frozen=True)
class MessageTemplate:
    """Pure formatter — turns a :class:`RunCompletedEvent` into a string.

    Kept in the domain layer (not the adapter) because the *shape* of the
    summary is a domain concern; the *delivery* is adapter concern. The
    template string itself is configuration, loaded from
    :class:`whilly.config.WhillyConfig`, so no copy is hard-coded here.

    Substitutions use :py:meth:`str.format` semantics against
    ``asdict(event)``. ``completed_at_iso`` is a convenience pre-render
    of ``event.completed_at`` so templates don't need a format spec to
    get a stable ISO-8601 string.
    """

    template: str

    def render(self, event: RunCompletedEvent) -> str:
        fields = asdict(event)
        fields["completed_at_iso"] = event.completed_at.isoformat()
        return self.template.format(**fields)


__all__ = [
    "MessageTemplate",
    "NotificationPort",
    "RunCompletedEvent",
]
