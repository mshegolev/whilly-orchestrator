"""Core synchronous watch-loop for ``whilly jira watch``.

Wraps the live-validated one-shot poll cycle (``collect_jira_work_snapshot``)
on a configurable interval with:

- Graceful signal-driven stop via ``threading.Event``
- Atomic JSON status file (state running/stopped, pid, counters)
- Exponential backoff on consecutive transient failures
- PID-file single-instance guard (refuse-and-hint; never kills other process)
- Best-effort DB audit-event helper (warn-not-fail)

CLI wiring (``whilly jira watch`` action + credential gate) lands in plan 02.
Pause/readiness gates land in plan 03.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import sys
import tempfile
import threading
import time
from collections.abc import Callable, MutableMapping
from pathlib import Path
from typing import Any

from whilly.jira_watch import (
    JiraWorkSnapshot,
    collect_jira_work_snapshot,  # noqa: F401  (default for production callers)
)
from whilly.llm_ops import _log_dir

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Type aliases (redefined locally to avoid circular import from whilly.cli.jira)
# ---------------------------------------------------------------------------

SnapshotCollector = Callable[..., JiraWorkSnapshot]
ConfigLoader = Callable[[], Any]
ConfigReader = Callable[[], dict[str, Any]]
Prompt = Callable[[str], str]
BrowserOpener = Callable[[str], bool]
IsATTY = Callable[[], bool]

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

EXIT_OK = 0
EXIT_VALIDATION_ERROR = 1  # also used as single-instance refusal code

_BACKOFF_SEQUENCE = (5, 10, 20, 40, 60)
_DEFAULT_INTERVAL = 300  # seconds
_INTERVAL_ENV = "WHILLY_JIRA_WATCH_INTERVAL"

# Audit event types
EVENT_CYCLE = "watch.cycle"
EVENT_FAILURE = "watch.failure"

_SHUTDOWN_SIGNALS = (signal.SIGTERM, signal.SIGINT)

# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------


def _watch_dir() -> Path:
    """Return the watch log directory (WHILLY_LOG_DIR/watch/)."""
    return _log_dir() / "watch"


def _status_path() -> Path:
    """Return the canonical path for the watch status JSON file."""
    return _watch_dir() / "jira-watch-status.json"


def _pid_path() -> Path:
    """Return the canonical path for the watch PID lock file."""
    return _watch_dir() / "jira-watch.pid"


# ---------------------------------------------------------------------------
# Interval resolution
# ---------------------------------------------------------------------------


def _resolve_interval(
    args_interval: int | None,
    env: MutableMapping[str, str],
) -> int:
    """Resolve the poll interval (seconds).

    Priority: explicit ``--interval`` arg > ``WHILLY_JIRA_WATCH_INTERVAL`` env
    > 300-second default.
    """
    if args_interval is not None:
        return int(args_interval)
    raw = env.get(_INTERVAL_ENV, "").strip()
    if raw:
        try:
            return int(raw)
        except ValueError:
            log.warning(
                "%s=%r is not a valid integer; falling back to %d s default",
                _INTERVAL_ENV,
                raw,
                _DEFAULT_INTERVAL,
            )
    return _DEFAULT_INTERVAL


# ---------------------------------------------------------------------------
# Signal handler seam
# ---------------------------------------------------------------------------


def _install_watch_signal_handlers(stop: threading.Event) -> None:
    """Install SIGTERM/SIGINT handlers that set *stop*.

    Sync equivalent of the asyncio handler in ``whilly/worker/main.py``
    lines 171-252. Must not be called in tests — pass
    ``install_signal_handlers=False`` instead.
    """

    def _handler(signum: int, frame: object) -> None:
        log.info("received signal %d; requesting graceful shutdown", signum)
        stop.set()

    for sig in _SHUTDOWN_SIGNALS:
        signal.signal(sig, _handler)


# ---------------------------------------------------------------------------
# Interruptible sleep
# ---------------------------------------------------------------------------


def _interruptible_sleep(stop: threading.Event, seconds: float) -> bool:
    """Sleep for *seconds* or until *stop* is set.

    Returns ``True`` if the stop event fired (caller should exit the loop),
    ``False`` if the timeout elapsed normally.

    Uses ``threading.Event.wait`` instead of ``time.sleep`` so the loop is
    signal-responsive (see 20-RESEARCH.md Pitfall 2 / anti-patterns).
    """
    return stop.wait(timeout=seconds)


# ---------------------------------------------------------------------------
# Atomic status file write (verbatim model from whilly/state_store.py)
# ---------------------------------------------------------------------------


def _write_status(status: dict[str, Any], status_path: Path) -> None:
    """Atomic overwrite of the watch status file (T-20-05 mitigation).

    Uses tempfile + ``os.replace`` so a crash mid-write leaves a consistent
    previous version on disk (state_store.py model).
    """
    d = status_path.parent or Path(".")
    d.mkdir(parents=True, exist_ok=True)
    content = json.dumps(status, ensure_ascii=False, indent=2) + "\n"
    fd, tmp_path = tempfile.mkstemp(dir=str(d), suffix=".tmp", prefix=".jira-watch-status-")
    closed = False
    try:
        os.write(fd, content.encode("utf-8"))
        os.close(fd)
        closed = True
        os.replace(tmp_path, status_path)
    except BaseException:
        if not closed:
            os.close(fd)
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise


# ---------------------------------------------------------------------------
# PID-file single-instance guard (T-20-02 mitigation)
# ---------------------------------------------------------------------------


def _acquire_pid_lock(pid_path: Path) -> bool:
    """Attempt to acquire the PID lock.

    Returns ``True`` if the lock was acquired (no live instance found);
    ``False`` if a live instance already holds the lock.

    Algorithm (20-RESEARCH.md Pattern 2):
    - If the file does not exist → write our PID, return True.
    - If the file exists → read the stored PID.
      - ``os.kill(pid, 0)`` succeeds → live process exists → return False
        (refuse-and-hint; NEVER send a terminating signal, T-20-02).
      - ``os.kill`` raises ``OSError`` or the PID is unparseable → stale
        file → overwrite and return True.
    """
    if pid_path.exists():
        try:
            stored_pid = int(pid_path.read_text(encoding="utf-8").strip())
            os.kill(stored_pid, 0)
            # Signal 0 succeeded → process is alive → refuse
            return False
        except (OSError, ValueError):
            # OSError: process gone (ESRCH) or no permission (EPERM → alive but
            # not ours, treat as stale for our purposes — conservative choice).
            # ValueError: corrupt PID file — overwrite.
            pass

    # Write our PID atomically (T-20-05)
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    my_pid = os.getpid()
    fd, tmp_path = tempfile.mkstemp(dir=str(pid_path.parent), suffix=".tmp", prefix=".jira-watch-pid-")
    closed = False
    try:
        os.write(fd, str(my_pid).encode("utf-8"))
        os.close(fd)
        closed = True
        os.replace(tmp_path, pid_path)
    except BaseException:
        if not closed:
            os.close(fd)
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise
    return True


def _release_pid_lock(pid_path: Path) -> None:
    """Remove the PID lock file if it still holds our PID.

    Guards against removing a successor's lock after a fast restart.
    """
    if not pid_path.exists():
        return
    try:
        stored = int(pid_path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return
    if stored == os.getpid():
        try:
            pid_path.unlink(missing_ok=True)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Best-effort DB audit-event helper
# ---------------------------------------------------------------------------


async def _persist_watch_event(
    *,
    dsn: str,
    issue_key: str,
    event_type: str,
    payload: dict[str, Any],
    repo: Any = None,
) -> None:
    """Append a watch audit event to Postgres (best-effort).

    When *repo* is injected (tests) it is used directly; otherwise the
    standard pool/repository lifecycle is used.

    Payload MUST be secret-free (T-20-03): never include token or DSN value.
    """
    if repo is not None:
        await repo.append_jira_work_event(
            issue_key=issue_key,
            event_type=event_type,
            payload=payload,
        )
        return

    from whilly.adapters.db import close_pool, create_pool
    from whilly.adapters.db.repository import TaskRepository

    pool = await create_pool(dsn)
    try:
        task_repo = TaskRepository(pool)
        await task_repo.append_jira_work_event(
            issue_key=issue_key,
            event_type=event_type,
            payload=payload,
        )
    finally:
        await close_pool(pool)


# ---------------------------------------------------------------------------
# Main watch loop
# ---------------------------------------------------------------------------


def _run_jira_watch(
    args: Any,
    *,
    snapshot_collector: SnapshotCollector,
    environ: MutableMapping[str, str] | None = None,
    stop_event: threading.Event | None = None,
    install_signal_handlers: bool = True,
    # TODO(plan-02): add config_loader, config_reader, prompt, secret_prompt,
    #   browser_opener, stdin_isatty for credential gate wiring.
) -> int:
    """Run the Jira watch loop.

    Parameters
    ----------
    args:
        Parsed CLI arguments (or ``argparse.Namespace`` / ``SimpleNamespace``).
        Expected attributes: ``issues`` (list[str]), ``interval`` (int|None),
        ``timeout`` (int).
    snapshot_collector:
        Callable matching ``(issue_ref, *, timeout) -> JiraWorkSnapshot``.
        Injected for tests; production callers pass
        ``collect_jira_work_snapshot``.
    environ:
        Env-var mapping; defaults to ``os.environ``.
    stop_event:
        Shared stop signal. A pre-set event causes an immediate graceful exit.
        When ``None`` a fresh ``threading.Event`` is created.
    install_signal_handlers:
        Set to ``False`` in unit tests to prevent SIGTERM/SIGINT registration.

    Returns
    -------
    int
        ``EXIT_OK`` (0) on graceful stop, ``EXIT_VALIDATION_ERROR`` (1) when a
        live watcher already holds the PID lock.
    """
    effective_env: MutableMapping[str, str] = environ if environ is not None else os.environ
    stop = stop_event if stop_event is not None else threading.Event()

    if install_signal_handlers:
        _install_watch_signal_handlers(stop)

    # TODO(plan-02): credential gate (_ensure_jira_config) wired here once
    #   the watch subparser is registered in build_jira_parser().

    # Resolve interval before acquiring the PID lock so the status file can
    # record it even when we refuse due to a live instance.
    interval = _resolve_interval(getattr(args, "interval", None), effective_env)
    issues: list[str] = list(getattr(args, "issues", []) or [])
    timeout: int = int(getattr(args, "timeout", 15))

    # --- PID-file single-instance guard (T-20-02) ---
    pid_path = _pid_path()
    if not _acquire_pid_lock(pid_path):
        try:
            stored_pid = int(pid_path.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            stored_pid = -1
        print(
            f"whilly jira watch: another watcher is already running (pid={stored_pid}); stop it first.",
            file=sys.stderr,
        )
        return EXIT_VALIDATION_ERROR

    # --- Status dict (T-20-03: secret-free) ---
    started_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    status: dict[str, Any] = {
        "state": "running",
        "pid": os.getpid(),
        "issues": issues,
        "interval_seconds": interval,
        "cycle_count": 0,
        "error_count": 0,
        "last_poll_at": None,
        "last_poll_result": None,
        "backoff_seconds": 0,
        "started_at": started_at,
        "stopped_at": None,
    }
    status_file = _status_path()
    _write_status(status, status_file)

    try:
        consecutive_failures = 0

        while not stop.is_set():
            # Interruptible sleep (skip on first cycle when interval==0 or
            # when stop is already set).
            backoff = status["backoff_seconds"]
            if _interruptible_sleep(stop, interval + backoff):
                break  # stop was set during sleep

            if stop.is_set():
                break

            # --- Execute one cycle (serial per-issue) ---
            cycle_ok = True
            for issue_ref in issues:
                try:
                    snapshot_collector(issue_ref, timeout=timeout)
                    consecutive_failures = 0
                    status["backoff_seconds"] = 0
                    status["last_poll_result"] = "ok"
                except Exception:  # noqa: BLE001
                    cycle_ok = False
                    consecutive_failures += 1
                    status["error_count"] = status["error_count"] + 1
                    idx = min(consecutive_failures - 1, len(_BACKOFF_SEQUENCE) - 1)
                    status["backoff_seconds"] = _BACKOFF_SEQUENCE[idx]
                    status["last_poll_result"] = "error"
                    log.warning(
                        "watch cycle error for %s (consecutive=%d, backoff=%ds)",
                        issue_ref,
                        consecutive_failures,
                        status["backoff_seconds"],
                    )

            status["cycle_count"] = status["cycle_count"] + 1
            status["last_poll_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            _write_status(status, status_file)

            # --- Best-effort DB audit event (T-20-03: payload secret-free) ---
            dsn = effective_env.get("WHILLY_DATABASE_URL", "").strip()
            if dsn:
                evt_type = EVENT_CYCLE if cycle_ok else EVENT_FAILURE
                cycle_payload: dict[str, Any] = {
                    "cycle_count": status["cycle_count"],
                    "issues": issues,
                    "result": status["last_poll_result"],
                    "backoff_seconds": status["backoff_seconds"],
                }
                try:
                    asyncio.run(
                        _persist_watch_event(
                            dsn=dsn,
                            issue_key=issues[0] if issues else "",
                            event_type=evt_type,
                            payload=cycle_payload,
                        )
                    )
                except Exception as exc:  # noqa: BLE001 — best-effort must not kill the watcher
                    print(
                        f"whilly jira watch: persist failed ({exc.__class__.__name__}) — "
                        "best-effort, check WHILLY_DATABASE_URL connectivity.",
                        file=sys.stderr,
                    )

    finally:
        # Graceful exit — write final status and release PID lock.
        status["state"] = "stopped"
        status["stopped_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        _write_status(status, status_file)
        _release_pid_lock(pid_path)

    return EXIT_OK
