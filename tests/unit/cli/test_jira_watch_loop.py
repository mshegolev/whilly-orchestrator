"""Unit tests for ``whilly/cli/jira_watch_loop.py`` — loop core (Task 1).

Tests are deterministic: no real wall-clock sleeps, collector injected, signal
handlers disabled (install_signal_handlers=False).
"""

from __future__ import annotations

import json
import os
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

    # Include the interval env var in the injected environ dict so that
    # _resolve_interval sees it via effective_env (the injected mapping takes
    # precedence over os.environ when passed explicitly).
    env = {**_jira_env(), "WHILLY_JIRA_WATCH_INTERVAL": "42"}

    stop = threading.Event()
    stop.set()  # exit before first cycle

    from whilly.cli.jira_watch_loop import _run_jira_watch

    rc = _run_jira_watch(
        _watch_args(interval=None, timeout=15),
        snapshot_collector=lambda ref, *, timeout=15: _fake_snapshot(ref),
        environ=env,
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


# ===========================================================================
# Task 2 – Backoff, PID guard, DB audit event helper
# ===========================================================================


# ---------------------------------------------------------------------------
# Task 2 – Test 1: consecutive failures drive backoff through 5/10/20/40/60
# ---------------------------------------------------------------------------


def test_backoff_increases_on_consecutive_failures_and_resets_on_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Consecutive RuntimeError raises increment backoff through
    5,10,20,40,60 (capped at 60); one success resets backoff to 0.

    Uses interval=0 and captures the backoff_seconds visible in the status
    file after each cycle by reading the file between calls.
    """
    monkeypatch.setenv("WHILLY_LOG_DIR", str(tmp_path))

    # We'll run the loop in a controlled way: each fake_collector call
    # either raises or succeeds depending on a list of outcomes.
    outcomes = [
        "fail",
        "fail",
        "fail",
        "fail",
        "fail",
        "fail",  # 6th failure: backoff still capped at 60
        "ok",  # success: resets backoff
    ]
    call_index = [0]
    stop = threading.Event()

    backoff_snapshots: list[int] = []
    status_path = tmp_path / "watch" / "jira-watch-status.json"

    def fake_collector(ref: str, *, timeout: int = 15) -> JiraWorkSnapshot:
        idx = call_index[0]
        call_index[0] += 1
        outcome = outcomes[idx] if idx < len(outcomes) else "ok"
        # After last call, set stop so the loop exits
        if call_index[0] >= len(outcomes):
            stop.set()
        if outcome == "fail":
            raise RuntimeError(f"simulated failure {idx + 1}")
        return _fake_snapshot(ref)

    # Patch _interruptible_sleep to capture backoff and not actually sleep
    from whilly.cli import jira_watch_loop

    def patched_sleep(stop_evt: threading.Event, seconds: float) -> bool:
        # Record the backoff (seconds - interval; interval == 0 here)
        backoff_snapshots.append(int(seconds))
        return stop_evt.is_set()

    monkeypatch.setattr(jira_watch_loop, "_interruptible_sleep", patched_sleep)

    rc = jira_watch_loop._run_jira_watch(
        _watch_args(interval=0),
        snapshot_collector=fake_collector,
        environ=_jira_env(),
        stop_event=stop,
        install_signal_handlers=False,
    )

    assert rc == 0
    # Read final status
    status = json.loads(status_path.read_text(encoding="utf-8"))
    # After success the backoff should be 0
    assert status["backoff_seconds"] == 0, "backoff must reset to 0 after success"
    # error_count must equal the number of failures (6)
    assert status["error_count"] == 6, f"expected 6 errors, got {status['error_count']}"

    # The backoff_snapshots list captured the sleep(interval + backoff)
    # argument on each cycle (interval=0, so sleep == backoff).
    # Backoff is applied to the UPCOMING gap after a failure, so:
    #   Before cycle 1 (no prior failure): sleep(0)
    #   Before cycle 2 (after 1 fail):     sleep(5)
    #   Before cycle 3 (after 2 fails):    sleep(10)
    #   Before cycle 4 (after 3 fails):    sleep(20)
    #   Before cycle 5 (after 4 fails):    sleep(40)
    #   Before cycle 6 (after 5 fails):    sleep(60)
    #   Before cycle 7 (after 6 fails):    sleep(60)  — still capped
    #   (cycle 7 is success, sets stop — loop exits without another sleep)
    expected_backoffs = [0, 5, 10, 20, 40, 60, 60]
    assert backoff_snapshots == expected_backoffs, (
        f"backoff sequence mismatch: {backoff_snapshots} != {expected_backoffs}"
    )


# ---------------------------------------------------------------------------
# Task 2 – Test 2: _acquire_pid_lock behaviour
# ---------------------------------------------------------------------------


def test_acquire_pid_lock(tmp_path: Path) -> None:
    """_acquire_pid_lock returns True (no existing file), False (live PID),
    True (stale/garbage PID)."""
    from whilly.cli.jira_watch_loop import _acquire_pid_lock

    pid_file = tmp_path / "watch" / "jira-watch.pid"

    # 1. No file → returns True, writes our PID
    result = _acquire_pid_lock(pid_file)
    assert result is True, "should acquire when no file exists"
    assert pid_file.exists(), "pid file must be created"
    stored = int(pid_file.read_text(encoding="utf-8").strip())
    assert stored == os.getpid()

    # Clean up for next check
    pid_file.unlink()

    # 2. File holds a live PID (our own PID is definitely live) → returns False
    pid_file.parent.mkdir(parents=True, exist_ok=True)
    pid_file.write_text(str(os.getpid()), encoding="utf-8")
    result = _acquire_pid_lock(pid_file)
    assert result is False, "should refuse when PID file holds a live process"

    # 3. File holds a dead/garbage PID → returns True (stale → overwrite)
    pid_file.write_text("99999999", encoding="utf-8")  # very likely not alive
    # If by chance 99999999 is alive on this system, use a bad string instead
    try:
        os.kill(99999999, 0)
        # PID IS alive — fall back to garbage string to trigger ValueError path
        pid_file.write_text("not-a-pid", encoding="utf-8")
    except OSError:
        pass  # good: 99999999 is dead, stale path will be taken

    result = _acquire_pid_lock(pid_file)
    assert result is True, "should acquire when PID file holds a dead PID"


# ---------------------------------------------------------------------------
# Task 2 – Test 3: live PID file causes loop to refuse (no collector calls)
# ---------------------------------------------------------------------------


def test_live_pid_file_refuses_second_watcher(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """When a live PID file exists, _run_jira_watch exits with
    EXIT_VALIDATION_ERROR (1) without calling the collector.
    It MUST NOT send a real signal to the stored PID.
    """
    monkeypatch.setenv("WHILLY_LOG_DIR", str(tmp_path))

    # Write a live PID file (our own PID is alive)
    watch_dir = tmp_path / "watch"
    watch_dir.mkdir(parents=True, exist_ok=True)
    pid_file = watch_dir / "jira-watch.pid"
    pid_file.write_text(str(os.getpid()), encoding="utf-8")

    call_count = [0]

    def fake_collector(ref: str, *, timeout: int = 15) -> JiraWorkSnapshot:
        call_count[0] += 1
        return _fake_snapshot(ref)

    from whilly.cli.jira_watch_loop import _run_jira_watch

    rc = _run_jira_watch(
        _watch_args(interval=0),
        snapshot_collector=fake_collector,
        environ=_jira_env(),
        stop_event=threading.Event(),
        install_signal_handlers=False,
    )

    assert rc == 1, "must return EXIT_VALIDATION_ERROR (1)"
    assert call_count[0] == 0, "collector must not be called when another watcher is live"

    captured = capsys.readouterr()
    assert "already running" in captured.err, "hint about other watcher must be printed to stderr"
    assert str(os.getpid()) in captured.err, "hint must include the existing pid"


# ---------------------------------------------------------------------------
# Task 2 – Test 4: _persist_watch_event calls append_jira_work_event on
#           the injected repo; a raising persist is swallowed (warn-not-fail)
# ---------------------------------------------------------------------------


class _FakeRepo:
    """Minimal fake repo for DB audit tests."""

    def __init__(self) -> None:
        self.events: list[dict[str, object]] = []

    async def append_jira_work_event(self, **kwargs: object) -> int:
        self.events.append(kwargs)
        return 1


class _RaisingRepo:
    """Fake repo that always raises on append."""

    async def append_jira_work_event(self, **kwargs: object) -> int:
        raise RuntimeError("simulated DB error")


def test_persist_watch_event_calls_repo(tmp_path: Path) -> None:
    """_persist_watch_event calls append_jira_work_event on the injected repo
    with the expected issue_key and event_type.
    """
    import asyncio as _asyncio

    from whilly.cli.jira_watch_loop import _persist_watch_event

    repo = _FakeRepo()
    _asyncio.run(
        _persist_watch_event(
            dsn="",  # unused when repo is injected
            issue_key="ABC-123",
            event_type="watch.cycle",
            payload={"cycle_count": 1, "result": "ok"},
            repo=repo,
        )
    )

    assert len(repo.events) == 1
    evt = repo.events[0]
    assert evt["issue_key"] == "ABC-123"
    assert evt["event_type"] == "watch.cycle"
    assert evt["payload"]["cycle_count"] == 1  # type: ignore[index]


def test_persist_watch_event_raises_are_swallowed_by_loop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """When _persist_watch_event raises, the watch loop continues (warn-not-fail).

    We simulate this by running the loop with a DSN and a patched
    _persist_watch_event that always raises, and verify the loop still completes
    normally.
    """
    monkeypatch.setenv("WHILLY_LOG_DIR", str(tmp_path))

    stop = threading.Event()
    call_count = [0]

    def fake_collector(ref: str, *, timeout: int = 15) -> JiraWorkSnapshot:
        call_count[0] += 1
        stop.set()
        return _fake_snapshot(ref)

    from whilly.cli import jira_watch_loop

    async def raising_persist(**kwargs: object) -> None:
        raise RuntimeError("simulated DB error")

    monkeypatch.setattr(jira_watch_loop, "_persist_watch_event", raising_persist)

    env = {**_jira_env(), "WHILLY_DATABASE_URL": "postgres://fake/db"}

    rc = jira_watch_loop._run_jira_watch(
        _watch_args(interval=0),
        snapshot_collector=fake_collector,
        environ=env,
        stop_event=stop,
        install_signal_handlers=False,
    )

    assert rc == 0, "loop must not fail when persist raises"
    assert call_count[0] == 1, "collector must still be called"
    captured = capsys.readouterr()
    assert "persist failed" in captured.err, "warning must be printed to stderr"


# ===========================================================================
# Plan 02 Task 1 – Pause gate: poll-no-dispatch with watch.paused audit event
# ===========================================================================


def _watch_args_with_pause(
    issues: list[str] | None = None,
    interval: int = 0,
    timeout: int = 15,
) -> SimpleNamespace:
    """Build args with dispatch=False (default-off) for pause gate tests."""
    return SimpleNamespace(
        issues=issues or ["ABC-123"],
        interval=interval,
        timeout=timeout,
        dispatch=False,
        readiness_repo_path=None,
        allow_unready_run=False,
    )


# ---------------------------------------------------------------------------
# Plan 02 Task 1 – Test 1: paused → collector still called, dispatch not called
# ---------------------------------------------------------------------------


def test_pause_gate_collector_called_dispatch_not(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When paused: collector (read-only polling) is still called, but the
    dispatch runner is NOT invoked and last_poll_result == 'paused'.
    """
    monkeypatch.setenv("WHILLY_LOG_DIR", str(tmp_path))

    # Create a tmp pause file with a JSON reason payload
    pause_file = tmp_path / ".whilly_pause"
    pause_file.write_text(
        '{"paused": true, "reason": "maintenance", "timestamp": "2026-01-01T00:00:00Z"}',
        encoding="utf-8",
    )

    from whilly.pause_control import PauseControl

    pause_ctrl = PauseControl(pause_file=str(pause_file))

    collector_calls = [0]
    dispatch_calls = [0]
    stop = threading.Event()

    def fake_collector(ref: str, *, timeout: int = 15) -> JiraWorkSnapshot:
        collector_calls[0] += 1
        stop.set()  # one cycle only
        return _fake_snapshot(ref)

    def fake_dispatch_runner(*args: object, **kwargs: object) -> int:
        dispatch_calls[0] += 1
        return 0

    from whilly.cli.jira_watch_loop import _run_jira_watch

    rc = _run_jira_watch(
        _watch_args_with_pause(),
        snapshot_collector=fake_collector,
        environ=_jira_env(),
        stop_event=stop,
        install_signal_handlers=False,
        pause_control=pause_ctrl,
        dispatch_runner=fake_dispatch_runner,
    )

    assert rc == 0
    assert collector_calls[0] == 1, "collector must be called (read-only polling continues)"
    assert dispatch_calls[0] == 0, "dispatch runner must NOT be called when paused"

    # Status must record the paused result
    status_path = tmp_path / "watch" / "jira-watch-status.json"
    status = json.loads(status_path.read_text(encoding="utf-8"))
    assert status["last_poll_result"] == "paused"


# ---------------------------------------------------------------------------
# Plan 02 Task 1 – Test 2: paused → watch.paused audit event emitted
# ---------------------------------------------------------------------------


def test_pause_gate_emits_watch_paused_event(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When paused, a watch.paused audit event is appended via the injected
    _FakeRepo with reason in the payload and no secret leaks.
    """
    monkeypatch.setenv("WHILLY_LOG_DIR", str(tmp_path))

    pause_file = tmp_path / ".whilly_pause"
    pause_file.write_text(
        '{"paused": true, "reason": "deploy-in-progress", "timestamp": "2026-01-01T00:00:00Z"}',
        encoding="utf-8",
    )

    from whilly.pause_control import PauseControl

    pause_ctrl = PauseControl(pause_file=str(pause_file))

    stop = threading.Event()

    def fake_collector(ref: str, *, timeout: int = 15) -> JiraWorkSnapshot:
        stop.set()
        return _fake_snapshot(ref)

    repo = _FakeRepo()
    env = {**_jira_env(), "WHILLY_DATABASE_URL": "postgres://fake/db"}

    from whilly.cli import jira_watch_loop
    from whilly.cli.jira_watch_loop import EVENT_PAUSED

    # Patch _persist_watch_event to use the fake repo
    orig_persist = jira_watch_loop._persist_watch_event

    async def fake_persist(*, dsn: str, issue_key: str, event_type: str, payload: dict, repo: object = None) -> None:
        await orig_persist(
            dsn=dsn,
            issue_key=issue_key,
            event_type=event_type,
            payload=payload,
            repo=repo if repo is not None else _repo_holder[0],
        )

    _repo_holder = [repo]
    monkeypatch.setattr(jira_watch_loop, "_persist_watch_event", fake_persist)

    jira_watch_loop._run_jira_watch(
        _watch_args_with_pause(),
        snapshot_collector=fake_collector,
        environ=env,
        stop_event=stop,
        install_signal_handlers=False,
        pause_control=pause_ctrl,
    )

    paused_events = [e for e in repo.events if e.get("event_type") == EVENT_PAUSED]
    assert len(paused_events) >= 1, "watch.paused event must be emitted"
    evt_payload = paused_events[0]["payload"]
    assert evt_payload["reason"] == "deploy-in-progress"
    # Payload must be secret-free (no token or DSN)
    raw = json.dumps(evt_payload)
    assert "jira-token" not in raw
    assert "postgres://" not in raw


# ---------------------------------------------------------------------------
# Plan 02 Task 1 – Test 3: not paused → paused branch not taken
# ---------------------------------------------------------------------------


def test_no_pause_gate_not_taken_when_unpaused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without the pause file, last_poll_result must not be 'paused'."""
    monkeypatch.setenv("WHILLY_LOG_DIR", str(tmp_path))

    from whilly.pause_control import PauseControl

    # No pause file exists — use a path that definitely does not exist
    pause_ctrl = PauseControl(pause_file=str(tmp_path / "no_such_pause_file"))

    stop = threading.Event()

    def fake_collector(ref: str, *, timeout: int = 15) -> JiraWorkSnapshot:
        stop.set()
        return _fake_snapshot(ref)

    from whilly.cli.jira_watch_loop import _run_jira_watch

    rc = _run_jira_watch(
        _watch_args_with_pause(),
        snapshot_collector=fake_collector,
        environ=_jira_env(),
        stop_event=stop,
        install_signal_handlers=False,
        pause_control=pause_ctrl,
    )

    assert rc == 0
    status_path = tmp_path / "watch" / "jira-watch-status.json"
    status = json.loads(status_path.read_text(encoding="utf-8"))
    assert status["last_poll_result"] != "paused"
