"""Unit tests for ``whilly/cli/jira_watch_loop.py`` — loop core (Task 1).

Tests are deterministic: no real wall-clock sleeps, collector injected, signal
handlers disabled (install_signal_handlers=False).
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from whilly.jira_watch import JiraWorkSnapshot


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _jira_env() -> dict[str, str]:
    return {
        "JIRA_SERVER_URL": "https://company.atlassian.net",
        "JIRA_USERNAME": "dev@example.com",
        "JIRA_API_TOKEN": "jira-token",
    }


def _fake_snapshot(issue_key: str = "ABC-123") -> JiraWorkSnapshot:
    return JiraWorkSnapshot(
        issue_key=issue_key,
        summary="Fix ETL job",
        description="desc",
        comments=({"id": "20001", "body": "first comment"},),
        changelog_ids=("10001",),
        links=(),
        repo_targets=(),
        context_hashes={"combined_hash": "hash"},
        classification={"kind": "bug", "urgency": "normal"},
        comment_commands=(),
        last_seen_comment_id="20001",
    )


def _watch_args(
    issues: list[str] | None = None,
    interval: int = 0,
    timeout: int = 15,
) -> SimpleNamespace:
    """Build a minimal args namespace for _run_jira_watch."""
    return SimpleNamespace(
        issues=issues or ["ABC-123"],
        interval=interval,
        timeout=timeout,
    )


# ---------------------------------------------------------------------------
# Task 1 – Test 1: loop calls collector exactly N times
# ---------------------------------------------------------------------------


def test_watch_loop_calls_collector_per_cycle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Loop calls the injected snapshot_collector once per cycle.

    The collector sets stop_event on the 2nd call, so the loop must exit
    after exactly 2 collector invocations.
    """
    monkeypatch.setenv("WHILLY_LOG_DIR", str(tmp_path))

    call_count = 0
    stop = threading.Event()

    def fake_collector(ref: str, *, timeout: int = 15) -> JiraWorkSnapshot:
        nonlocal call_count
        call_count += 1
        if call_count >= 2:
            stop.set()
        return _fake_snapshot(ref)

    from whilly.cli.jira_watch_loop import _run_jira_watch

    rc = _run_jira_watch(
        _watch_args(interval=0),
        snapshot_collector=fake_collector,
        environ=_jira_env(),
        stop_event=stop,
        install_signal_handlers=False,
    )

    assert call_count == 2
    assert rc == 0


# ---------------------------------------------------------------------------
# Task 1 – Test 2: interval resolution (--interval > env > 300 default)
# ---------------------------------------------------------------------------


def test_interval_resolution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Interval resolves: explicit arg > WHILLY_JIRA_WATCH_INTERVAL env > 300."""
    from whilly.cli.jira_watch_loop import _resolve_interval

    # Explicit arg wins over env
    env = {"WHILLY_JIRA_WATCH_INTERVAL": "120"}
    assert _resolve_interval(60, env) == 60

    # Env wins over default
    assert _resolve_interval(None, env) == 120

    # Default when neither set
    assert _resolve_interval(None, {}) == 300

    # Bad env value falls back to default
    assert _resolve_interval(None, {"WHILLY_JIRA_WATCH_INTERVAL": "notanint"}) == 300


def test_interval_recorded_in_status_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The status file records the resolved interval_seconds."""
    monkeypatch.setenv("WHILLY_LOG_DIR", str(tmp_path))
    monkeypatch.setenv("WHILLY_JIRA_WATCH_INTERVAL", "42")

    stop = threading.Event()
    stop.set()  # exit before first cycle

    from whilly.cli.jira_watch_loop import _run_jira_watch

    rc = _run_jira_watch(
        _watch_args(interval=None, timeout=15),
        snapshot_collector=lambda ref, *, timeout=15: _fake_snapshot(ref),
        environ=_jira_env(),
        stop_event=stop,
        install_signal_handlers=False,
    )

    assert rc == 0
    status_path = tmp_path / "watch" / "jira-watch-status.json"
    assert status_path.exists()
    status = json.loads(status_path.read_text(encoding="utf-8"))
    # env-resolved interval must be honoured (42s)
    assert status["interval_seconds"] == 42


# ---------------------------------------------------------------------------
# Task 1 – Test 3: pre-set stop_event exits before first cycle with rc 0
# ---------------------------------------------------------------------------


def test_pre_set_stop_event_exits_zero(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stop_event that is already set causes the loop to exit immediately
    without calling the collector; final status has state='stopped'."""
    monkeypatch.setenv("WHILLY_LOG_DIR", str(tmp_path))

    call_count = 0
    stop = threading.Event()
    stop.set()  # pre-set

    def fake_collector(ref: str, *, timeout: int = 15) -> JiraWorkSnapshot:
        nonlocal call_count
        call_count += 1
        return _fake_snapshot(ref)

    from whilly.cli.jira_watch_loop import _run_jira_watch

    rc = _run_jira_watch(
        _watch_args(interval=0),
        snapshot_collector=fake_collector,
        environ=_jira_env(),
        stop_event=stop,
        install_signal_handlers=False,
    )

    assert rc == 0
    assert call_count == 0

    # Status file must say state=stopped
    status_path = tmp_path / "watch" / "jira-watch-status.json"
    assert status_path.exists(), "status file must be written"
    status = json.loads(status_path.read_text(encoding="utf-8"))
    assert status["state"] == "stopped"


# ---------------------------------------------------------------------------
# Task 1 – Test 4: status file written under WHILLY_LOG_DIR/watch/ and is
#           valid JSON with only non-secret fields
# ---------------------------------------------------------------------------


def test_status_file_location_and_no_secrets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Status file written to WHILLY_LOG_DIR/watch/jira-watch-status.json;
    valid JSON; no JIRA_API_TOKEN, no WHILLY_DATABASE_URL value in the data.
    """
    monkeypatch.setenv("WHILLY_LOG_DIR", str(tmp_path))

    env = {
        **_jira_env(),
        "WHILLY_DATABASE_URL": "postgres://user:pass@host/db",
    }

    stop = threading.Event()

    def fake_collector(ref: str, *, timeout: int = 15) -> JiraWorkSnapshot:
        stop.set()
        return _fake_snapshot(ref)

    from whilly.cli.jira_watch_loop import _run_jira_watch

    rc = _run_jira_watch(
        _watch_args(interval=0),
        snapshot_collector=fake_collector,
        environ=env,
        stop_event=stop,
        install_signal_handlers=False,
    )

    assert rc == 0
    watch_dir = tmp_path / "watch"
    assert watch_dir.is_dir(), "watch/ dir must be created"

    status_path = watch_dir / "jira-watch-status.json"
    assert status_path.exists(), "status file must exist"

    raw = status_path.read_text(encoding="utf-8")
    # Must be valid JSON
    status = json.loads(raw)

    # Non-secret fields must be present
    assert "state" in status
    assert "pid" in status
    assert "cycle_count" in status

    # Secret fields must NOT appear as values in the JSON
    assert "jira-token" not in raw
    assert "postgres://user:pass@host/db" not in raw


# ---------------------------------------------------------------------------
# Task 1 – Test 5: install_signal_handlers=False installs no signal handlers
# ---------------------------------------------------------------------------


def test_no_signal_handlers_when_disabled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """install_signal_handlers=False must not register any SIGTERM/SIGINT handler."""
    import signal

    monkeypatch.setenv("WHILLY_LOG_DIR", str(tmp_path))

    # Record handlers before
    original_sigterm = signal.getsignal(signal.SIGTERM)
    original_sigint = signal.getsignal(signal.SIGINT)

    stop = threading.Event()
    stop.set()

    from whilly.cli.jira_watch_loop import _run_jira_watch

    _run_jira_watch(
        _watch_args(interval=0),
        snapshot_collector=lambda ref, *, timeout=15: _fake_snapshot(ref),
        environ=_jira_env(),
        stop_event=stop,
        install_signal_handlers=False,  # key
    )

    # Handlers must be unchanged
    assert signal.getsignal(signal.SIGTERM) == original_sigterm
    assert signal.getsignal(signal.SIGINT) == original_sigint
