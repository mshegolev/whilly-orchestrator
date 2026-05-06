"""Composition test for the Slack hook in :func:`whilly.cli.run.run_run_command`.

Asserts that on a clean run, the CLI builds a :class:`RunCompletedEvent`
from the actual ``WorkerStats`` and the resolved worker id, and hands
it to the injected :class:`NotificationPort`. Failure-mode of the
notifier itself (transport / api errors) is covered in the slack
adapter tests; here we only care about the wiring.

We patch ``_async_run`` rather than the pool so no asyncpg / Postgres
is touched — same trick the existing ``tests/unit/test_cli_run.py``
suite uses.
"""

from __future__ import annotations

import socket

import pytest

from whilly.cli import run as cli_run
from whilly.cli.run import (
    DATABASE_URL_ENV,
    EXIT_ENVIRONMENT_ERROR,
    EXIT_OK,
    _PlanNotFoundError,
    run_run_command,
)
from whilly.core.notifications import NotificationPort, RunCompletedEvent
from whilly.worker.local import WorkerStats


class _RecorderNotifier(NotificationPort):
    def __init__(self) -> None:
        self.events: list[RunCompletedEvent] = []

    def notify_run_completed(self, event: RunCompletedEvent) -> None:
        self.events.append(event)


def _patch_async_run(monkeypatch: pytest.MonkeyPatch, stats: WorkerStats) -> None:
    async def _fake_async_run(**kwargs: object) -> WorkerStats:
        return stats

    monkeypatch.setattr(cli_run, "_async_run", _fake_async_run)


# ─── happy path: notifier sees an event with stats + plan id ─────────────


def test_notifier_receives_event_with_stats_and_plan_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(DATABASE_URL_ENV, "postgresql://user@127.0.0.1/whilly")
    _patch_async_run(
        monkeypatch,
        WorkerStats(iterations=4, completed=3, failed=1, idle_polls=0, released_on_shutdown=0),
    )
    notifier = _RecorderNotifier()

    code = run_run_command(
        ["--plan", "P-Slack", "--worker-id", "w-slack-test"],
        notifier=notifier,
    )

    assert code == EXIT_OK
    assert len(notifier.events) == 1
    event = notifier.events[0]
    assert event.plan_id == "P-Slack"
    assert event.worker_id == "w-slack-test"
    assert event.hostname == socket.gethostname()
    assert event.iterations == 4
    assert event.completed == 3
    assert event.failed == 1
    assert event.idle_polls == 0
    assert event.released_on_shutdown == 0
    assert event.duration_s >= 0.0


# ─── error path: misconfig → no notification ─────────────────────────────


def test_no_notification_when_plan_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Plan-not-found is an env misconfig, not a "work complete" signal.

    Pinning this contract keeps the Slack channel free of false
    "finished" pings when the operator has the wrong plan id.
    """
    monkeypatch.setenv(DATABASE_URL_ENV, "postgresql://user@127.0.0.1/whilly")

    async def _fake_async_run(**kwargs: object) -> WorkerStats:
        raise _PlanNotFoundError(str(kwargs["plan_id"]))

    monkeypatch.setattr(cli_run, "_async_run", _fake_async_run)
    notifier = _RecorderNotifier()

    code = run_run_command(["--plan", "P-MISSING"], notifier=notifier)
    assert code == EXIT_ENVIRONMENT_ERROR
    assert notifier.events == []


# ─── safety net: notifier exception must not change exit code ────────────


def test_notifier_exception_does_not_break_exit_code(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Even a bug in a custom notifier leaves the CLI returning EXIT_OK.

    The slack adapter swallows internally; this test exercises the
    outer ``try/except`` belt that protects the CLI from third-party
    notifier impls (and from a hypothetical regression in the slack
    adapter that lets an exception escape).
    """
    monkeypatch.setenv(DATABASE_URL_ENV, "postgresql://user@127.0.0.1/whilly")
    _patch_async_run(monkeypatch, WorkerStats(iterations=1))

    class _Boom(NotificationPort):
        def notify_run_completed(self, event: RunCompletedEvent) -> None:
            raise RuntimeError("notifier blew up")

    code = run_run_command(["--plan", "P-Boom"], notifier=_Boom())
    assert code == EXIT_OK
    assert "notifier raised" in caplog.text
