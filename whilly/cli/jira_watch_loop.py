"""Core synchronous watch-loop for ``whilly jira watch``.

Wraps the live-validated one-shot poll cycle (``collect_jira_work_snapshot``)
on a configurable interval with:

- Graceful signal-driven stop via ``threading.Event``
- Atomic JSON status file (state running/stopped, pid, counters)
- Exponential backoff on consecutive transient failures
- PID-file single-instance guard (refuse-and-hint; never kills other process)
- Best-effort DB audit-event helper (warn-not-fail)
- Global pause gate (``PauseControl`` / ``.whilly_pause``): read-only polling
  continues, dispatch is suppressed, ``watch.paused`` audit event emitted
- Readiness gate: dispatch blocked with ``watch.block`` event when verdict
  is not ``ready_for_testing`` (unless ``--allow-unready-run``); default-off
  dispatch path gated behind explicit ``--dispatch`` flag

CLI wiring (``whilly jira watch`` action) lives in ``whilly/cli/jira.py``; the
credential gate (``_ensure_jira_config``) runs there BEFORE this loop starts.
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
from whilly.pause_control import PauseControl

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
EVENT_PAUSED = "watch.paused"
EVENT_BLOCK = "watch.block"
EVENT_DISPATCH = "watch.dispatch"

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

    Algorithm (20-RESEARCH.md Pattern 2, hardened per review WR-02):
    - Creation uses ``os.open(..., O_CREAT | O_EXCL)`` so two concurrent
      watchers cannot both pass a check-then-write race: exactly one
      ``O_EXCL`` create succeeds.
    - When the file already exists, probe the stored PID with
      ``os.kill(pid, 0)`` (NEVER send a terminating signal, T-20-02):
      - probe succeeds → live process → refuse.
      - ``ProcessLookupError`` (ESRCH) → stale → reclaim (one retry).
      - ``PermissionError`` (EPERM) → the process IS alive (owned by
        another user) → refuse. Fail closed on any other ``OSError``.
      - unparseable PID file → stale → reclaim.
    - After creating, re-read the file and verify it still holds our PID
      (write-then-verify shrinks the residual stale-reclaim TOCTOU window).
    """
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    my_pid = os.getpid()

    for _attempt in range(2):
        try:
            fd = os.open(pid_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            try:
                stored_pid = int(pid_path.read_text(encoding="utf-8").strip())
            except (OSError, ValueError):
                stored_pid = None  # corrupt/unreadable → stale
            if stored_pid is not None:
                try:
                    os.kill(stored_pid, 0)
                    return False  # signal 0 succeeded → process is alive → refuse
                except ProcessLookupError:
                    pass  # ESRCH: process gone → stale → reclaim
                except PermissionError:
                    return False  # EPERM: alive but not ours → refuse (fail closed)
                except OSError:
                    return False  # unknown probe failure → fail closed
            # Stale lock → remove and retry the O_EXCL create exactly once.
            try:
                pid_path.unlink(missing_ok=True)
            except OSError:
                return False
            continue
        try:
            os.write(fd, str(my_pid).encode("utf-8"))
        finally:
            os.close(fd)
        # Write-then-verify: confirm we still own the lock.
        try:
            return int(pid_path.read_text(encoding="utf-8").strip()) == my_pid
        except (OSError, ValueError):
            return False

    return False  # lost the reclaim race twice → another watcher won


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
# Readiness gate helper (re-implemented locally — do NOT import from
# whilly.cli.jira to avoid circular import; mirrors jira.py lines 1138-1144)
# ---------------------------------------------------------------------------


def _read_watch_readiness(plan_path: Path) -> dict[str, Any] | None:
    """Read the ``jira_work.readiness`` dict from the plan JSON.

    Returns the readiness dict if present, else ``None``.  Mirrors
    ``_read_jira_work_readiness`` in ``whilly/cli/jira.py`` without importing
    it (Pitfall 5: no import from ``whilly.cli.jira``).
    """
    try:
        data = json.loads(plan_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    jira_work = data.get("jira_work")
    if not isinstance(jira_work, dict):
        return None
    readiness = jira_work.get("readiness")
    return readiness if isinstance(readiness, dict) else None


def _evaluate_watch_readiness(readiness_repo_path: str | None) -> dict[str, Any] | None:
    """Evaluate dispatch readiness from ``--readiness-repo-path``.

    Two accepted inputs (review CR-02):

    - a **repository directory** → probed with ``probe_code_readiness``, the
      same Phase-17 evaluation that ``whilly jira readiness`` and intake use;
    - a **plan JSON file** containing a ``jira_work.readiness`` block.

    Returns ``None`` whenever readiness cannot be determined (no path given,
    path missing, unreadable file, malformed JSON, probe error).  Callers MUST
    treat ``None`` as NOT ready — the gate fails closed.
    """
    if not readiness_repo_path:
        return None
    path = Path(readiness_repo_path)
    if path.is_dir():
        from whilly.jira_work import probe_code_readiness

        try:
            return probe_code_readiness(path).to_dict()
        except Exception:  # noqa: BLE001 — undeterminable readiness must block, not crash
            return None
    if path.is_file():
        return _read_watch_readiness(path)
    return None


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
    pause_control: PauseControl | None = None,
    dispatch_runner: Callable[..., int] | None = None,
) -> int:
    """Run the Jira watch loop.

    Parameters
    ----------
    args:
        Parsed CLI arguments (or ``argparse.Namespace`` / ``SimpleNamespace``).
        Expected attributes: ``issues`` (list[str]), ``interval`` (int|None),
        ``timeout`` (int), ``dispatch`` (bool, default False),
        ``readiness_repo_path`` (str|None), ``allow_unready_run`` (bool).
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
    pause_control:
        ``PauseControl`` instance for the global pause gate.  Defaults to
        ``PauseControl()`` reading ``.whilly_pause`` in the current directory.
    dispatch_runner:
        Injectable callable for the Phase-17-gated dispatch hook.  Only
        invoked when ``--dispatch`` is set, unpaused, and readiness satisfied.
        ``None`` means no dispatch is wired (the production closure is built
        in ``whilly/cli/jira.py`` only when ``--dispatch`` is passed).

    Returns
    -------
    int
        ``EXIT_OK`` (0) on graceful stop, ``EXIT_VALIDATION_ERROR`` (1) when a
        live watcher already holds the PID lock.
    """
    effective_env: MutableMapping[str, str] = environ if environ is not None else os.environ
    stop = stop_event if stop_event is not None else threading.Event()
    effective_pause_ctrl = pause_control if pause_control is not None else PauseControl()

    if install_signal_handlers:
        _install_watch_signal_handlers(stop)

    # The credential gate (_ensure_jira_config) runs in the CLI layer
    # (whilly/cli/jira.py watch branch) before this loop is entered.

    # Resolve interval and issue list up front. (The refusal path below
    # intentionally writes NO status file — it must not clobber the live
    # watcher's status.)
    interval = _resolve_interval(getattr(args, "interval", None), effective_env)
    issues: list[str] = list(getattr(args, "issues", []) or [])
    timeout: int = int(getattr(args, "timeout", 15))

    # Read dispatch-gate args via getattr so the function is robust when args
    # namespace omits these attributes (plan 03 wires the full subparser).
    wants_dispatch: bool = bool(getattr(args, "dispatch", False))
    allow_unready: bool = bool(getattr(args, "allow_unready_run", False))
    readiness_repo_path: str | None = getattr(args, "readiness_repo_path", None)

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
        "last_error": None,
        "backoff_seconds": 0,
        "last_dispatch_rc": None,
        "dispatched": {},
        "started_at": started_at,
        "stopped_at": None,
    }
    status_file = _status_path()
    _write_status(status, status_file)

    try:
        consecutive_failures = 0
        first_cycle = True

        while not stop.is_set():
            # Poll immediately on start (WR-04); sleep interval + backoff
            # BETWEEN cycles only.
            if first_cycle:
                first_cycle = False
            else:
                backoff = status["backoff_seconds"]
                if _interruptible_sleep(stop, interval + backoff):
                    break  # stop was set during sleep

            if stop.is_set():
                break

            # --- Execute one cycle (serial per-issue) ---
            # Per-issue outcomes are tracked separately so a later issue's
            # success cannot erase an earlier issue's failure/backoff within
            # the same cycle (review WR-01).
            issue_results: dict[str, str] = {}
            failed_issues: list[str] = []
            snapshot_hashes: dict[str, str] = {}
            for issue_ref in issues:
                try:
                    snapshot = snapshot_collector(issue_ref, timeout=timeout)
                    issue_results[issue_ref] = "ok"
                    hashes = getattr(snapshot, "context_hashes", None) or {}
                    snapshot_hashes[issue_ref] = str(hashes.get("combined_hash", "") or "")
                except Exception as exc:  # noqa: BLE001
                    issue_results[issue_ref] = "error"
                    failed_issues.append(issue_ref)
                    status["error_count"] = status["error_count"] + 1
                    status["last_error"] = exc.__class__.__name__
                    log.warning(
                        "watch cycle error for %s (%s)",
                        issue_ref,
                        exc.__class__.__name__,
                    )

            cycle_ok = not failed_issues
            if cycle_ok:
                consecutive_failures = 0
                status["backoff_seconds"] = 0
                status["last_poll_result"] = "ok"
            else:
                consecutive_failures += 1
                idx = min(consecutive_failures - 1, len(_BACKOFF_SEQUENCE) - 1)
                status["backoff_seconds"] = _BACKOFF_SEQUENCE[idx]
                status["last_poll_result"] = "error" if len(failed_issues) == len(issues) else "partial"
                log.warning(
                    "watch cycle had failures (failed=%s, consecutive=%d, backoff=%ds)",
                    ",".join(failed_issues),
                    consecutive_failures,
                    status["backoff_seconds"],
                )

            status["cycle_count"] = status["cycle_count"] + 1
            status["last_poll_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

            # --- Pause gate (T-20-06/T-20-07): check AFTER collect, BEFORE dispatch ---
            # Read-only polling (collect) already happened above; only dispatch
            # is suppressed while paused (CONTEXT.md locked decision).
            if effective_pause_ctrl.is_paused():
                pause_info = effective_pause_ctrl.get_pause_info() or {}
                reason = pause_info.get("reason", "unknown")
                log.info(
                    "whilly jira watch: global pause active (%s); skipping dispatch",
                    reason,
                )
                status["last_poll_result"] = "paused"
                _write_status(status, status_file)
                # Best-effort audit event (T-20-07: secret-free payload)
                dsn = effective_env.get("WHILLY_DATABASE_URL", "").strip()
                if dsn:
                    try:
                        asyncio.run(
                            _persist_watch_event(
                                dsn=dsn,
                                issue_key=issues[0] if issues else "",
                                event_type=EVENT_PAUSED,
                                payload={
                                    "reason": reason,
                                    "issue_key": issues[0] if issues else "",
                                    "cycle_ok": cycle_ok,
                                    "issue_results": issue_results,
                                    "error_count": status["error_count"],
                                },
                            )
                        )
                    except Exception as exc:  # noqa: BLE001
                        print(
                            f"whilly jira watch: persist failed ({exc.__class__.__name__}) — "
                            "best-effort, check WHILLY_DATABASE_URL connectivity.",
                            file=sys.stderr,
                        )
                continue  # read-only polling continues next cycle; no dispatch

            _write_status(status, status_file)

            # --- Best-effort DB audit event for regular cycle (T-20-03: payload secret-free) ---
            dsn = effective_env.get("WHILLY_DATABASE_URL", "").strip()
            if dsn:
                evt_type = EVENT_CYCLE if cycle_ok else EVENT_FAILURE
                cycle_payload: dict[str, Any] = {
                    "cycle_count": status["cycle_count"],
                    "issues": issues,
                    "result": status["last_poll_result"],
                    "backoff_seconds": status["backoff_seconds"],
                    "issue_results": issue_results,
                }
                if failed_issues:
                    cycle_payload["failed_issues"] = failed_issues
                # watch.failure events are attributed to the (first) failing
                # issue so the audit trail names the actual offender (WR-01).
                evt_issue_key = failed_issues[0] if failed_issues else (issues[0] if issues else "")
                try:
                    asyncio.run(
                        _persist_watch_event(
                            dsn=dsn,
                            issue_key=evt_issue_key,
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

            # --- Readiness gate + default-off dispatch (T-20-06) ---
            # Dispatch is structurally unreachable unless --dispatch is set:
            # the entire branch is inside `if wants_dispatch`.
            if wants_dispatch:
                _run_dispatch_if_ready(
                    args=args,
                    issues=issues,
                    status=status,
                    status_file=status_file,
                    effective_env=effective_env,
                    allow_unready=allow_unready,
                    readiness_repo_path=readiness_repo_path,
                    dispatch_runner=dispatch_runner,
                    snapshot_hashes=snapshot_hashes,
                )

    finally:
        # Graceful exit — write final status and release PID lock.
        status["state"] = "stopped"
        status["stopped_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        _write_status(status, status_file)
        _release_pid_lock(pid_path)

    return EXIT_OK


def _run_watch_status(args: Any, *, environ: MutableMapping[str, str] | None = None) -> int:
    """Print the current watcher status from the status JSON file.

    Reads ``_status_path()`` and prints either a human-readable summary
    (default) or the raw JSON (when ``args.json`` is True).

    Returns ``EXIT_OK`` in both the "found" and "not found" cases — a missing
    status file is not an error (the watcher may simply not have been started).
    """
    status_file = _status_path()
    if not status_file.exists():
        print(
            f"whilly jira watch-status: no watcher status found at {status_file}",
            file=sys.stderr,
        )
        return EXIT_OK

    try:
        data = json.loads(status_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(
            f"whilly jira watch-status: could not read status file: {exc}",
            file=sys.stderr,
        )
        return EXIT_OK

    if not isinstance(data, dict):
        print(
            "whilly jira watch-status: could not read status file: not a JSON object",
            file=sys.stderr,
        )
        return EXIT_OK

    # Liveness check (review WR-05): a crashed watcher (SIGKILL, OOM, power
    # loss) never writes its final "stopped" status — verify the recorded PID
    # instead of trusting the file forever.
    state = data.get("state", "unknown")
    pid = data.get("pid", "?")
    if state == "running":
        try:
            os.kill(int(pid), 0)
        except (ProcessLookupError, ValueError, TypeError):
            state = f"stale (pid {pid} not running)"
            data["state"] = state
        except OSError:
            pass  # EPERM etc.: the process exists

    if getattr(args, "json", False):
        print(json.dumps(data, ensure_ascii=False))
        return EXIT_OK

    # Human-readable summary of key fields (T-20-11: no secrets)
    issues = data.get("issues", [])
    cycle_count = data.get("cycle_count", 0)
    error_count = data.get("error_count", 0)
    last_poll_at = data.get("last_poll_at") or "never"
    backoff = data.get("backoff_seconds", 0)
    started_at = data.get("started_at") or "?"
    stopped_at = data.get("stopped_at")

    print(f"whilly jira watch-status: state={state} pid={pid}")
    print(f"  issues:        {', '.join(issues) if issues else 'none'}")
    print(f"  started_at:    {started_at}")
    if stopped_at:
        print(f"  stopped_at:    {stopped_at}")
    print(f"  last_poll_at:  {last_poll_at}")
    print(f"  cycle_count:   {cycle_count}")
    print(f"  error_count:   {error_count}")
    print(f"  backoff_seconds: {backoff}")
    return EXIT_OK


def _run_dispatch_if_ready(
    *,
    args: Any,
    issues: list[str],
    status: dict[str, Any],
    status_file: Path,
    effective_env: MutableMapping[str, str],
    allow_unready: bool,
    readiness_repo_path: str | None,
    dispatch_runner: Callable[..., int] | None,
    snapshot_hashes: dict[str, str] | None = None,
) -> None:
    """Check readiness gate and invoke dispatch_runner if clear (T-20-06).

    Extracted to a helper so the dispatch call site is visually isolated and
    easy to grep-gate.  Called only when ``wants_dispatch`` is True.

    The readiness gate FAILS CLOSED (review CR-02): when readiness cannot be
    determined (missing/garbled input, directory probe error, no
    ``--readiness-repo-path`` at all), dispatch is blocked with a
    ``watch.block`` event carrying ``verdict="unknown"`` — never silently
    dispatched.  Only ``--allow-unready-run`` overrides the gate.
    """
    readiness = _evaluate_watch_readiness(readiness_repo_path)

    issue_ref = issues[0] if issues else ""

    if not allow_unready and (readiness is None or readiness.get("verdict") != "ready_for_testing"):
        verdict = readiness.get("verdict") if readiness is not None else "unknown"
        missing = (readiness.get("missing_context") or []) if readiness is not None else []
        log.info(
            "whilly jira watch: readiness gate failed; verdict=%s missing=%s; skipping dispatch",
            verdict,
            ",".join(str(m) for m in missing),
        )
        status["last_poll_result"] = "blocked"
        _write_status(status, status_file)
        # Best-effort watch.block audit event (T-20-07: secret-free payload)
        dsn = effective_env.get("WHILLY_DATABASE_URL", "").strip()
        if dsn:
            try:
                asyncio.run(
                    _persist_watch_event(
                        dsn=dsn,
                        issue_key=issue_ref,
                        event_type=EVENT_BLOCK,
                        payload={
                            "verdict": verdict,
                            "missing_context": missing,
                            "issue_key": issue_ref,
                        },
                    )
                )
            except Exception as exc:  # noqa: BLE001
                print(
                    f"whilly jira watch: persist failed ({exc.__class__.__name__}) — "
                    "best-effort, check WHILLY_DATABASE_URL connectivity.",
                    file=sys.stderr,
                )
        return

    # Readiness satisfied (or allow_unready) — invoke runner per issue.
    #
    # Honest dispatch contract (review CR-03): success means rc == EXIT_OK.
    # EVENT_DISPATCH is emitted ONLY for rc == 0; any non-zero rc or raised
    # exception emits EVENT_FAILURE with the real rc — the audit trail never
    # claims a dispatch that did not happen. A dispatch failure must never
    # kill the watcher (review CR-01): exceptions are contained here.
    #
    # Dedup (review WR-06): after a successful dispatch the issue's snapshot
    # combined_hash is recorded in status["dispatched"]; the issue is NOT
    # re-dispatched until that hash changes (new comments/changelog). When no
    # hash is available the issue is dispatched at most once per watcher run.
    # Failed dispatches are not recorded and are retried next cycle.
    if dispatch_runner is None:
        return
    dispatched: dict[str, str] = status.setdefault("dispatched", {})
    dsn = effective_env.get("WHILLY_DATABASE_URL", "").strip()
    for issue_ref in issues:
        current_hash = (snapshot_hashes or {}).get(issue_ref, "")
        if issue_ref in dispatched and dispatched[issue_ref] == current_hash:
            continue  # already dispatched this snapshot
        dispatch_error: str | None = None
        try:
            dispatch_rc = int(dispatch_runner(args, issue_ref))
        except Exception as exc:  # noqa: BLE001 — dispatch must never kill the watcher
            dispatch_rc = EXIT_VALIDATION_ERROR
            dispatch_error = exc.__class__.__name__
            log.warning(
                "dispatch failed for %s (%s); watcher continues",
                issue_ref,
                dispatch_error,
            )
        dispatch_ok = dispatch_rc == EXIT_OK
        status["last_dispatch_rc"] = dispatch_rc
        if dispatch_ok:
            dispatched[issue_ref] = current_hash
        else:
            status["error_count"] = status["error_count"] + 1
            if dispatch_error is not None:
                status["last_error"] = dispatch_error
        _write_status(status, status_file)
        if dsn:
            payload: dict[str, Any] = {
                "issue_key": issue_ref,
                "rc": dispatch_rc,
                "ok": dispatch_ok,
            }
            if dispatch_error is not None:
                payload["error"] = dispatch_error
            try:
                asyncio.run(
                    _persist_watch_event(
                        dsn=dsn,
                        issue_key=issue_ref,
                        event_type=EVENT_DISPATCH if dispatch_ok else EVENT_FAILURE,
                        payload=payload,
                    )
                )
            except Exception as exc:  # noqa: BLE001
                print(
                    f"whilly jira watch: persist failed ({exc.__class__.__name__}) — "
                    "best-effort, check WHILLY_DATABASE_URL connectivity.",
                    file=sys.stderr,
                )
