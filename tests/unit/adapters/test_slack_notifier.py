"""Unit tests for :class:`whilly.adapters.notifications.slack.SlackNotifier`.

The adapter takes ``http_post`` as a constructor argument so we never
touch the network and never monkey-patch :mod:`urllib`. Each test feeds
a hand-rolled stub and asserts on what the adapter sent / how it
reacted to the response.

Failure-mode tests are the load-bearing ones: the orchestrator's exit
code MUST stay clean even when Slack is unreachable or rejects the
call. See the parent plan's "What NOT to do" section.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any
from urllib.error import URLError

import pytest

from whilly.adapters.notifications.slack import SlackNotifier
from whilly.core.notifications import MessageTemplate, RunCompletedEvent


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


@dataclass
class _StubResponse:
    payload: Mapping[str, Any]

    def json(self) -> Mapping[str, Any]:
        return self.payload


@dataclass
class _RecordingHttp:
    """Stub for the ``HttpPost`` callable: records call-args, returns ``response``."""

    response: _StubResponse | None = None
    raise_with: Exception | None = None
    calls: list[tuple[str, Mapping[str, str], Mapping[str, Any], float]] = field(default_factory=list)

    def __call__(
        self,
        url: str,
        headers: Mapping[str, str],
        payload: Mapping[str, Any],
        timeout_s: float,
    ) -> _StubResponse:
        self.calls.append((url, dict(headers), dict(payload), timeout_s))
        if self.raise_with is not None:
            raise self.raise_with
        assert self.response is not None  # configured by the test
        return self.response


def _make_notifier(http: _RecordingHttp, *, template: str | None = None) -> SlackNotifier:
    return SlackNotifier(
        token="xoxe.xoxp-test",
        channel="C0B1WT58EBE",
        api_base_url="https://slack.test/api",
        timeout_s=2.5,
        template=MessageTemplate(template or "done {plan_id} completed={completed}"),
        http_post=http,
        logger=logging.getLogger("whilly.test.slack"),
    )


# ─── happy path ──────────────────────────────────────────────────────────


def test_post_sends_to_chat_postmessage_with_bearer_and_json_body() -> None:
    http = _RecordingHttp(response=_StubResponse({"ok": True}))
    notifier = _make_notifier(http)
    notifier.notify_run_completed(_sample_event())
    assert len(http.calls) == 1
    url, headers, payload, timeout = http.calls[0]
    assert url == "https://slack.test/api/chat.postMessage"
    assert headers["Authorization"] == "Bearer xoxe.xoxp-test"
    assert headers["Content-Type"] == "application/json; charset=utf-8"
    assert payload == {"channel": "C0B1WT58EBE", "text": "done plan-42 completed=5"}
    assert timeout == pytest.approx(2.5)


def test_payload_serialises_to_valid_json() -> None:
    """The default ``urllib_post`` will ``json.dumps`` whatever the adapter hands it.

    Using a dict-only payload keeps that contract honest — anything
    non-serialisable would break in production but only after a real
    Slack call. Catching it here costs nothing.
    """
    http = _RecordingHttp(response=_StubResponse({"ok": True}))
    notifier = _make_notifier(http)
    notifier.notify_run_completed(_sample_event())
    _url, _headers, payload, _timeout = http.calls[0]
    encoded = json.dumps(dict(payload))
    assert json.loads(encoded) == dict(payload)


# ─── api-level failure: HTTP 200 with ok=false ───────────────────────────


def test_api_error_logged_but_not_raised(caplog: pytest.LogCaptureFixture) -> None:
    http = _RecordingHttp(response=_StubResponse({"ok": False, "error": "channel_not_found"}))
    notifier = _make_notifier(http)
    with caplog.at_level(logging.WARNING, logger="whilly.test.slack"):
        notifier.notify_run_completed(_sample_event())
    assert "channel_not_found" in caplog.text


# ─── transport-level failure: urllib raises ──────────────────────────────


def test_url_error_is_swallowed(caplog: pytest.LogCaptureFixture) -> None:
    http = _RecordingHttp(raise_with=URLError("boom"))
    notifier = _make_notifier(http)
    with caplog.at_level(logging.WARNING, logger="whilly.test.slack"):
        # Must not raise.
        notifier.notify_run_completed(_sample_event())
    assert "transport error" in caplog.text


def test_oserror_is_swallowed(caplog: pytest.LogCaptureFixture) -> None:
    http = _RecordingHttp(raise_with=ConnectionRefusedError("nope"))
    notifier = _make_notifier(http)
    with caplog.at_level(logging.WARNING, logger="whilly.test.slack"):
        notifier.notify_run_completed(_sample_event())
    assert "transport error" in caplog.text
