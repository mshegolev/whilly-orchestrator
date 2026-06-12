# Phase 20: Jira Watcher Daemon — Research

**Researched:** 2026-06-12
**Domain:** Python foreground daemon loop wrapping the existing `whilly jira poll` one-shot
cycle, with signal-based graceful stop, status-file lifecycle, single-instance guard,
exponential backoff, PauseControl gate, and readiness gate.
**Confidence:** HIGH — all findings verified directly against codebase source files.

---

<user_constraints>
## User Constraints (from CONTEXT.md)

### Locked Decisions

- `whilly jira watch` as a new ACTION in the existing `whilly jira` subparser — a thin
  foreground loop wrapping the validated one-shot poll cycle
  (`collect_jira_work_snapshot` path), with `--issue KEY` repeatable.
- Foreground process; the operator backgrounds it via tmux/systemd. No fork/detach magic.
- Interval via `--interval SECONDS` and `WHILLY_JIRA_WATCH_INTERVAL` env; default 300 s.
- Independent of `SchedulerWorker` / `scheduler_rules` tables.
- SIGINT/SIGTERM handled gracefully: finish current cycle, write final status, exit 0.
- Status file `whilly_logs/watch/jira-watch-status.json` plus `whilly jira watch-status` reader.
- Single-instance guard: lock/pid check; second watcher exits with a clear hint.
- Audit trail: file log always; DB audit event per cycle/failure when WHILLY_DATABASE_URL set.
- Exponential backoff 5/10/20/40/60 s cap, reset on success.
- Global pause (`PauseControl` / `.whilly_pause`): while paused, keep read-only polling,
  dispatch nothing, record the paused state in status/audit.
- Readiness gate: when code/test readiness not satisfied, record block reason as an audit
  event and wait — never dispatch.
- Watch is intake/refresh-only by default. Autonomous dispatch only behind explicit
  `--dispatch` flag, routed through the existing Phase-17-gated `jira run` path.

### Claude's Discretion

- Internal module layout (e.g., `whilly/cli/jira_watch_loop.py` vs inline in `cli/jira.py`
  vs extending `whilly/jira_watch.py`).
- Status-file JSON schema field names.
- Lock mechanism choice (pidfile vs lockfile).
- How watch-status renders.
- Exact audit event types/payloads, consistent with existing `append_jira_work_event` usage.

### Deferred Ideas (OUT OF SCOPE)

- Merging watch with SchedulerWorker JQL-rule intake into one daemon framework.
- systemd unit / launchd plist packaging for the watcher.
- WUI surface for watcher status.
</user_constraints>

<phase_requirements>
## Phase Requirements

| ID | Description | Research Support |
|----|-------------|------------------|
| WATCH-01 | Operator can run a long-running `whilly jira watch` daemon that wraps the one-shot poll cycle on a configurable interval | New ACTION added to `build_jira_parser()` / `run_jira_command()`; loop wraps `collect_jira_work_snapshot` |
| WATCH-02 | Operator can start, stop, and inspect watcher status; transient Jira/GitLab failures retried with backoff and recorded as audit events | SIGINT/SIGTERM → asyncio.Event stop; status JSON at `whilly_logs/watch/jira-watch-status.json`; `append_jira_work_event` for each failure |
| WATCH-03 | Watcher honors global worker pause and code/test readiness gates before dispatching any autonomous work | `PauseControl.is_paused()` / `.whilly_pause` file gate; `_read_jira_work_readiness` gate before dispatch |
</phase_requirements>

---

## Summary

Phase 20 adds `whilly jira watch` as a thin synchronous foreground loop that wraps
`collect_jira_work_snapshot` on a configurable interval. All the building blocks are
already in the codebase and fully verified: the one-shot poll path, the PauseControl
file-based gate, the `append_jira_work_event` audit trail, the signal-handler pattern from
`whilly.worker.main`, and the smoke command as the freshest CLI pattern to copy.

The loop itself is straightforward — the interesting design surface is (1) the two-mechanism
pause question (file-based `.whilly_pause` vs DB `control_state`), (2) the async vs
synchronous choice for the loop, and (3) the single-instance guard. These are all resolved
below with concrete recommendations.

**Primary recommendation:** Implement a new `whilly/cli/jira_watch_loop.py` module
containing the `_run_jira_watch` loop function and its helpers, invoked from `run_jira_command`
as `action == "watch"` and `action == "watch-status"`. Keep the loop synchronous
(`time.sleep` with a `threading.Event` for wakeup) because `collect_jira_work_snapshot` is
synchronous and the loop needs no concurrent I/O — the async overhead buys nothing here.
Use a PID file at `whilly_logs/watch/jira-watch.pid` for single-instance guard (no
external dependency, consistent with the `.whilly_state.json` pattern).

---

## Architectural Responsibility Map

| Capability | Primary Tier | Secondary Tier | Rationale |
|------------|-------------|----------------|-----------|
| Poll cycle execution | CLI/daemon loop | `whilly.jira_watch` collector | Loop owns cadence + gates; collector owns Jira HTTP |
| Pause gate | CLI/daemon loop | `PauseControl` (file sentinel) | Loop reads `.whilly_pause` before dispatch; PauseControl owns the file protocol |
| Readiness gate | CLI/daemon loop | `whilly.jira_work.probe_code_readiness` | Loop reads plan-level readiness verdict; no extra Jira call |
| Audit events | CLI/daemon loop (caller) | DB via `append_jira_work_event` (optional) | Loop constructs payload; DB is best-effort |
| Status file | CLI/daemon loop | `whilly_logs/watch/` directory | Operator reads independently of daemon |
| Single-instance guard | CLI/daemon loop | PID file in `whilly_logs/watch/` | Consistent with `.whilly_state.json` atomic-write pattern |
| Signal handling | CLI/daemon loop | `threading.Event` stop | Synchronous loop → `signal.signal` + threading.Event is correct (not asyncio.Event) |
| Dispatch (--dispatch) | CLI/daemon loop | `_run_intake` / `_run_plan_command` path | Only behind explicit flag; readiness already checked before reaching it |

---

## Standard Stack

All packages below are part of Python stdlib or are already project dependencies.
No new packages need to be installed.

### Core (all stdlib — no install required)

| Module | Version | Purpose | Why Standard |
|--------|---------|---------|--------------|
| `signal` | stdlib | SIGINT/SIGTERM handlers | Same approach as `whilly/worker/main.py` line 108-110 |
| `threading.Event` | stdlib | Interrupt-safe sleep for synchronous loop | Allows `stop_event.wait(timeout=interval)` wakeup on signal |
| `time` | stdlib | Timestamps, monotonic for backoff | Used by `PauseControl`, `StateStore` |
| `json` | stdlib | Status file serialization | Used by `StateStore`, `PauseControl` |
| `os` | stdlib | PID file write / process existence check | `os.getpid()`, `os.kill(pid, 0)` for liveness |
| `pathlib.Path` | stdlib | File path operations | Project-wide convention |
| `logging` | stdlib | Structured log output | Project-wide convention |

### Supporting (already project deps)

| Library | Purpose | Import Path |
|---------|---------|-------------|
| `PauseControl` | Read `.whilly_pause` file gate | `whilly.pause_control.PauseControl` |
| `collect_jira_work_snapshot` | One-shot Jira refresh | `whilly.jira_watch.collect_jira_work_snapshot` |
| `persist_jira_work_snapshot` | DB upsert on success | `whilly.jira_watch.persist_jira_work_snapshot` |
| `append_jira_work_event` (via TaskRepository) | Per-event audit trail | `whilly.adapters.db.repository.TaskRepository` |
| `_log_dir` | Honoring `WHILLY_LOG_DIR` env | `whilly.llm_ops._log_dir` |
| `_ensure_jira_config` | Credential gate | `whilly.cli.jira._ensure_jira_config` |
| `_read_jira_work_readiness` | Readiness verdict from plan JSON | `whilly.cli.jira._read_jira_work_readiness` |

**Installation:** No new packages — `pip install -e '.[dev]'` already covers all dependencies.

---

## Package Legitimacy Audit

No new external packages are introduced by this phase. All dependencies are stdlib or
existing project dependencies already installed.

**Packages removed due to slopcheck [SLOP] verdict:** none
**Packages flagged as suspicious [SUS]:** none

---

## Architecture Patterns

### System Architecture Diagram

```
Operator shell
     │
     │  whilly jira watch --issue ABC-123 --interval 300
     ▼
┌─────────────────────────────────────────────────────────────────┐
│  _run_jira_watch()  [whilly/cli/jira_watch_loop.py]             │
│                                                                  │
│  SIGINT/SIGTERM ──► signal.signal ──► stop_event.set()          │
│                                                                  │
│  ┌─────────── per-cycle logic ──────────────────────────────┐   │
│  │  1. stop_event.wait(timeout=interval)  [backoff-adjusted] │   │
│  │  2. PauseControl.is_paused()  ──► .whilly_pause file     │   │
│  │     paused? ── record audit event, skip dispatch, loop   │   │
│  │  3. collect_jira_work_snapshot(issue_ref, timeout=N)     │   │
│  │     failure? ── record audit event, apply backoff        │   │
│  │     success? ── reset backoff                            │   │
│  │  4. --dispatch only: _read_jira_work_readiness check     │   │
│  │     unready? ── record block audit event, skip dispatch  │   │
│  │  5. optional DB persist (best-effort)                    │   │
│  │  6. write jira-watch-status.json (atomic)                │   │
│  └──────────────────────────────────────────────────────────┘   │
│                                                                  │
│  on stop_event: write final status file, exit 0                 │
└─────────────────────────────────────────────────────────────────┘
         │                         │
         ▼ (optional, best-effort) ▼ (optional, --dispatch only)
  Postgres jira_work_events     _run_intake / jira run path
  via append_jira_work_event    (Phase-17 readiness-gated)
```

### Recommended Project Structure

```
whilly/
├── cli/
│   ├── jira.py              # existing — add watch/watch-status parser entries
│   └── jira_watch_loop.py   # NEW — _run_jira_watch, _run_watch_status, helpers
tests/
└── unit/
    └── cli/
        └── test_jira_watch_loop.py  # NEW — loop unit tests with injected deps
```

Status and lock files land under `whilly_logs/watch/` (honoring `WHILLY_LOG_DIR`):

```
whilly_logs/
└── watch/
    ├── jira-watch-status.json   # runtime status (atomic overwrite each cycle)
    └── jira-watch.pid           # single-instance guard
```

### Pattern 1: Synchronous loop with threading.Event for interruptible sleep

**What:** A `while not stop_event.is_set()` loop where each iteration's sleep uses
`stop_event.wait(timeout=interval)` rather than `time.sleep(interval)`. Signals set the
event, which wakes the wait immediately.

**When to use:** Any synchronous foreground daemon that calls synchronous I/O (like
`collect_jira_work_snapshot` which uses `requests`/`_jira_get`) and needs signal-responsive
shutdown without the overhead of asyncio.

**Source:** [VERIFIED: whilly/worker/main.py lines 108-250] — async equivalent;
[VERIFIED: whilly/github_projects.py lines 713-732] — sync loop with `time.sleep` but
lacking signal responsiveness (the pattern to improve on).

```python
# Source: pattern derived from whilly/worker/main.py + stdlib signal docs
import signal
import threading
import time

stop_event = threading.Event()

def _install_watch_signal_handlers() -> None:
    def _handler(signum: int, frame: object) -> None:
        stop_event.set()
    signal.signal(signal.SIGINT, _handler)
    signal.signal(signal.SIGTERM, _handler)

def _watch_loop(interval: int) -> None:
    _install_watch_signal_handlers()
    backoff_seconds = 0
    while not stop_event.is_set():
        sleep_time = interval + backoff_seconds
        # wait() returns False on timeout (keep going), True if event set (stop)
        if stop_event.wait(timeout=sleep_time):
            break
        _run_one_cycle(...)
```

**Important:** `signal.signal()` is synchronous-safe and is the correct API here.
The asyncio `loop.add_signal_handler()` from `whilly.worker.main` only works inside
a running asyncio event loop. This phase uses `signal.signal()` because the loop is
synchronous.

### Pattern 2: PID-file single-instance guard

**What:** Write `os.getpid()` to `whilly_logs/watch/jira-watch.pid` on start; on start,
check if the file exists and if the recorded pid is still alive via `os.kill(pid, 0)`.
Remove PID file on exit (in a `finally` block).

**When to use:** Single-instance guard for any foreground daemon process without external
coordination services.

**Source:** [ASSUMED] — stdlib pattern; no existing pidfile in codebase, but the
`StateStore` atomic-write pattern in `whilly/state_store.py:51-63` provides the model
for atomic write.

```python
# Source: model from whilly/state_store.py lines 51-63
import os
import tempfile
from pathlib import Path

def _acquire_pid_lock(pid_path: Path) -> bool:
    """Return True if we acquired the lock, False if another instance is running."""
    if pid_path.exists():
        try:
            pid = int(pid_path.read_text().strip())
            os.kill(pid, 0)  # raises OSError if pid is dead
            return False  # another instance is alive
        except (OSError, ValueError):
            pass  # stale pid file, proceed
    # Atomic write via tempfile + rename (same pattern as StateStore)
    dir_path = pid_path.parent
    dir_path.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(dir_path), suffix=".tmp", prefix=".jira-watch-pid-")
    try:
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        os.replace(tmp, pid_path)
    except BaseException:
        os.close(fd)
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    return True
```

### Pattern 3: append_jira_work_event for watch-cycle audit events

**What:** Per-cycle, per-failure, per-block events written to `jira_work_events` table
via `TaskRepository.append_jira_work_event`. Optional — only when `WHILLY_DATABASE_URL`
is set. Error is caught and logged, never fatal (WR-03 best-effort pattern).

**Source:** [VERIFIED: whilly/adapters/db/repository.py lines 1884-1908]
[VERIFIED: whilly/cli/jira.py lines 904-918] — `_persist_smoke_event` as exact model.

`append_jira_work_event` signature:
```python
async def append_jira_work_event(
    self,
    *,
    issue_key: str,       # e.g. "ABC-123"
    event_type: str,      # e.g. "watch.cycle", "watch.failure", "watch.paused", "watch.block"
    command: str = "",    # optional free-text
    payload: Mapping[str, Any] | None = None,
) -> int:
```

Existing event types to follow as naming convention:
- `"jira.refreshed"` — used by `persist_jira_work_snapshot` (jira_watch.py:151)
- `"smoke"` — used by `_persist_smoke_event` (cli/jira.py:917)

Proposed watch event types:
- `"watch.cycle"` — successful poll cycle
- `"watch.failure"` — transient failure during poll, backoff applied
- `"watch.paused"` — skipped dispatch due to global pause
- `"watch.block"` — skipped dispatch due to readiness gate failure
- `"watch.dispatch"` — issue dispatched via --dispatch path

The persist helper must use `asyncio.run(...)` just like `_persist_smoke_event` (cli/jira.py:873),
wrapped in a `try/except Exception` with best-effort warn-not-fail semantics.

### Pattern 4: Status file (atomic overwrite each cycle)

**What:** A JSON file at `_log_dir() / "watch" / "jira-watch-status.json"` written after
every cycle and on exit. Schema mirrors `StateStore.save()` pattern with an atomic
tempfile rename. The `watch-status` command reads and prints it.

**Source:** [VERIFIED: whilly/state_store.py lines 36-63] — atomic write pattern.
[VERIFIED: whilly/pause_control.py lines 15-24] — JSON status file model.

Recommended schema:
```json
{
  "state": "running | stopped",
  "pid": 12345,
  "issues": ["ABC-123", "DEF-456"],
  "interval_seconds": 300,
  "cycle_count": 42,
  "error_count": 3,
  "last_poll_at": "2026-06-12T10:00:00Z",
  "last_poll_result": "ok | error | paused | blocked",
  "backoff_seconds": 0,
  "started_at": "2026-06-12T09:00:00Z",
  "stopped_at": null
}
```

### Anti-Patterns to Avoid

- **Plain `time.sleep(interval)` in the loop:** Not signal-responsive. Use
  `stop_event.wait(timeout=interval)` so SIGINT/SIGTERM wakes the sleep immediately.
  [VERIFIED: whilly/github_projects.py:722 uses `time.sleep` and is the weaker pattern
  to avoid.]
- **Hardcoding the sleep as a blocking `time.sleep` loop without a stop check:** Means the
  process ignores SIGTERM until the sleep completes. Use `threading.Event.wait(timeout=N)`
  which returns early when the event is set.
- **Crashing the loop on DB persist failure:** The `append_jira_work_event` persist must be
  wrapped in `try/except Exception` — DB down must not kill the watcher.
  [VERIFIED: whilly/cli/jira.py:875-883]
- **Using `asyncio` for the outer watch loop:** `collect_jira_work_snapshot` is sync
  (`_jira_get` uses `requests` under the hood). Running `asyncio.run(...)` repeatedly
  inside a loop is fine for the DB persist helper but the outer loop stays sync.
- **Calling `_ensure_jira_config` with `args.interactive_config` if args namespace
  doesn't include that attribute:** The watch subparser must explicitly add
  `--interactive-config` / `--no-interactive-config` flags, as smoke does
  (cli/jira.py:308-318). `_ensure_jira_config` accesses `args.interactive_config`.

---

## Research Findings (8 required areas)

### 1. Poll cycle invocation (`_run_poll` / `collect_jira_work_snapshot`)

**Source:** [VERIFIED: whilly/cli/jira.py lines 679-712]

`_run_poll` calls:
```python
snapshot = snapshot_collector(args.jira_ref, timeout=args.timeout)
```
where `snapshot_collector` defaults to `collect_jira_work_snapshot` from `whilly.jira_watch`.

`collect_jira_work_snapshot(jira_ref: str, *, timeout: int = 15) -> JiraWorkSnapshot`
[VERIFIED: whilly/jira_watch.py lines 58-75]

The watch loop must call `collect_jira_work_snapshot(issue_ref, timeout=args.timeout)` directly
(or via injectable `snapshot_collector` for testability — same pattern as `_run_poll`).

For `--persist`, the path is `asyncio.run(_persist_poll_snapshot(...))`:
[VERIFIED: whilly/cli/jira.py lines 687-695, 921-930]

`persist_jira_work_snapshot` signature:
[VERIFIED: whilly/jira_watch.py lines 128-164]
```python
async def persist_jira_work_snapshot(
    repo: JiraWorkStateRepo,
    snapshot: JiraWorkSnapshot,
    *,
    plan_id: str = "",
    state: str = "refreshed",
    readiness_verdict: str = "",
) -> dict[str, Any]:
```

The watch loop must wrap this in `asyncio.run(...)` identically to `_persist_poll_snapshot`.

### 2. PauseControl semantics — which mechanism is the "global worker pause"

**CRITICAL FINDING:** There are TWO pause mechanisms in the codebase with different scopes.

**Mechanism A — `PauseControl` (`.whilly_pause` file):**
[VERIFIED: whilly/pause_control.py lines 9-51]
- File-based; `PauseControl(pause_file=".whilly_pause")`
- `is_paused()` → `self.pause_file.exists()`
- `get_pause_info()` → reads JSON from the file
- This is a **local file sentinel** used by the Whilly main loop
  (`wait_if_paused` blocks until the file is removed)
- The dashboard TUI `d`/`p` hotkeys interact with this file for plan execution

**Mechanism B — `ControlState` (DB `control_state` table):**
[VERIFIED: whilly/adapters/db/repository.py lines 1519-1528, 1795-1835]
- Postgres-backed; managed via `repo.pause_workers()` / `repo.resume_workers()`
- Available via `GET /workers/control_state` HTTP endpoint
- Used by the WUI operator dashboard to pause the distributed worker pool
- `repo.is_paused()` is a DB-backed check

**Resolution for the watch gate:**

The CONTEXT.md decision says "Global pause (`PauseControl` / `.whilly_pause`)". This maps
explicitly to Mechanism A — the file-based PauseControl. The watcher MUST honor this because:
1. The CONTEXT locked `.whilly_pause` as the mechanism.
2. The watcher is a CLI daemon that may run without a Postgres connection.
3. The file-based gate works without DB access.

The watcher should ALSO optionally check `ControlState` via DB if `WHILLY_DATABASE_URL` is
set (checking `repo.is_paused()` via `asyncio.run()`), but the file-based check is the
hard gate. [ASSUMED: the DB-based check as an "also check" is a discretion decision
not explicitly locked in CONTEXT.md.]

**Concrete gate logic:**
```python
from whilly.pause_control import PauseControl
pc = PauseControl()  # defaults to .whilly_pause in cwd
if pc.is_paused():
    info = pc.get_pause_info()
    reason = info.get("reason", "unknown") if info else "unknown"
    # record watch.paused audit event, skip dispatch, continue loop
```

### 3. Readiness gate — `_run_readiness` and dispatch path

**Source:** [VERIFIED: whilly/cli/jira.py lines 622-638] — `_run_intake` run path

The `_run_intake` "run" action demonstrates the readiness gate:
```python
readiness = _read_jira_work_readiness(plan_path)
if readiness and readiness.get("verdict") != "ready_for_testing" \
        and not bool(args.allow_unready_run):
    # gate fails → print error, return EXIT_VALIDATION_ERROR
```

`_read_jira_work_readiness(plan_path: Path) -> dict[str, Any] | None`
[VERIFIED: whilly/cli/jira.py lines 1138-1144] — reads `data["jira_work"]["readiness"]`
from the plan JSON on disk.

**For the watch loop's `--dispatch` path:**

1. After each successful poll, if `--dispatch` is set:
   - Read the plan file (via `_read_jira_work_readiness` or inline equivalent)
   - If `readiness.get("verdict") != "ready_for_testing"` → record `watch.block` event,
     skip dispatch, continue loop
   - If ready → call the gated dispatch path (below)

2. The "dispatch through the gated path" means invoking the same flow as `_run_intake` with
   `action=run`: `_run_plan_command(["apply", str(plan_path), "--strict"])` then
   `_run_plan_worker(...)`. However, for the watcher, this is done as a subprocess call
   via the `runner` injectable, not inline. [ASSUMED: exact dispatch hook shape is
   Claude's discretion per CONTEXT.md.]

`--readiness-repo-path` / `--allow-unready-run` flags from the `intake` and `run` actions
must be forwarded to the dispatch path. The watch subparser should accept these same flags
so the watch loop can pass them through.

### 4. `append_jira_work_event` signature, event kinds, required fields

**Source:** [VERIFIED: whilly/adapters/db/repository.py lines 1884-1908]
[VERIFIED: whilly/cli/jira.py lines 904-918] — `_persist_smoke_event` as model
[VERIFIED: whilly/jira_watch.py lines 151-163] — `persist_jira_work_snapshot` as model

Required fields: `issue_key` (non-empty str), `event_type` (non-empty str).
Optional: `command` (str, default `""`), `payload` (mapping, default `{}`).

SQL table: `jira_work_events (issue_key, event_type, command, payload, created_at)`
[VERIFIED: whilly/adapters/db/repository.py lines 846-850]

The watch-event persist helper follows `_persist_smoke_event` exactly:
```python
async def _persist_watch_event(
    *, dsn: str, issue_key: str, event_type: str, payload: dict
) -> None:
    from whilly.adapters.db import close_pool, create_pool
    from whilly.adapters.db.repository import TaskRepository
    pool = await create_pool(dsn)
    try:
        repo = TaskRepository(pool)
        await repo.append_jira_work_event(
            issue_key=issue_key,
            event_type=event_type,
            payload=payload,
        )
    finally:
        await close_pool(pool)
```

Called as:
```python
dsn = os.environ.get("WHILLY_DATABASE_URL", "").strip()
if dsn:
    try:
        asyncio.run(_persist_watch_event(dsn=dsn, issue_key=issue, ...))
    except Exception as exc:
        print(f"whilly jira watch: persist failed ({exc.__class__.__name__}) ...",
              file=sys.stderr)
```

### 5. Signal handling + graceful loop — prior art

**Signal handling:**
[VERIFIED: whilly/worker/main.py lines 108-252]

The asyncio version uses `loop.add_signal_handler()`. For the synchronous watch loop,
use `signal.signal()` directly:

```python
# Source: derived from whilly/worker/main.py lines 232-252 (sync equivalent)
import signal
import threading

stop_event = threading.Event()

def _install_watch_signal_handlers(stop: threading.Event) -> None:
    def _handler(signum: int, frame: object) -> None:
        stop.set()
    signal.signal(signal.SIGINT, _handler)
    signal.signal(signal.SIGTERM, _handler)
```

The `install_signal_handlers=False` parameter exists in `run_worker` for test isolation.
The watch loop should provide the same injectable `_stop_event` / `_signal_installer`
seam so tests don't install real signal handlers (same rationale as
`test_worker_shutdown.py` line 415).

**Prior `signal.signal` usage in codebase:**
[VERIFIED: whilly/pipeline/verification.py line 360] — `os.killpg` usage (unrelated)
[VERIFIED: whilly/worker/main.py lines 108-252] — asyncio-based signal handling

**Single-instance lock — no prior art in codebase.** The codebase has no existing pidfile
or fcntl lockfile. The simplest approach is a PID file with `os.kill(pid, 0)` liveness
check, following `StateStore.save()`'s atomic-write pattern. No `fcntl` needed on macOS
and Linux for a single-file PID approach.

### 6. Smoke command structure as freshest CLI pattern

**Source:** [VERIFIED: whilly/cli/jira.py lines 715-901]

Key elements to replicate:

| Element | Reference | Watch equivalent |
|---------|-----------|-----------------|
| Exit codes | `EXIT_OK=0`, `EXIT_VALIDATION_ERROR=1`, `EXIT_CONFIG_MISSING` from smoke.py | Same constants |
| Credential gate | `_ensure_jira_config(args, ...)` with `args.interactive_config` | Must add `--interactive-config` flag to watch subparser |
| `environ` injection | `environ: MutableMapping[str, str] | None = None` | Same pattern |
| Best-effort persist | `try: asyncio.run(...) except Exception: warn, don't fail` | Watch uses same |
| `_log_dir()` | `from whilly.llm_ops import _log_dir` | Watch dir: `_log_dir() / "watch"` |
| Optional `--json` output | `args.json` | `watch-status` uses `--json` |

The watch subparser must include `--interactive-config` / `--no-interactive-config` because
`_ensure_jira_config` accesses `args.interactive_config` directly (not via `getattr`).
[VERIFIED: whilly/cli/jira.py line 1259]

Missing this was a Phase 19 pitfall: [VERIFIED: STATE.md decision] "Smoke subparser
requires --interactive-config flags because `_ensure_jira_config` accesses
`args.interactive_config`".

### 7. Test patterns for the watch loop

**Source:** [VERIFIED: tests/unit/test_worker_shutdown.py] — best model for
injectable-stop + signal isolation
[VERIFIED: tests/unit/test_jira_watch.py] — FakeRepo inject pattern for async persist
[VERIFIED: tests/unit/cli/test_jira_smoke.py] — smoke test pattern for CLI action tests

**Key test design decisions (from test_worker_shutdown.py):**

1. Never install real signal handlers in unit tests.
   Provide `install_signal_handlers: bool = True` param (or `_stop_event` param that
   can be pre-set externally).

2. Inject `snapshot_collector` as a callable to replace `collect_jira_work_snapshot`.
   This is already the pattern in `run_jira_command` (passes `snapshot_collector` through).

3. Use `threading.Event` + a second thread or a callable-counting injected collector
   to trigger the stop after N cycles without real timing:

```python
# Deterministic loop test — no real sleep
def test_watch_loop_stops_after_n_cycles(tmp_path):
    call_count = 0
    stop = threading.Event()

    def fake_collector(ref, *, timeout=15):
        nonlocal call_count
        call_count += 1
        if call_count >= 2:
            stop.set()
        return _fake_snapshot()

    _run_jira_watch(
        ["--issue", "ABC-123", "--interval", "0"],
        snapshot_collector=fake_collector,
        stop_event=stop,        # injected stop event
        install_signal_handlers=False,
        environ=_jira_env(),
        ...
    )
    assert call_count == 2
```

4. Test the pause gate by pre-creating a `.whilly_pause` file in tmp_path and verifying
   dispatch is not called.

5. Test the backoff sequence by injecting a collector that raises `RuntimeError` N times
   then succeeds; verify the sleep intervals passed to a mock sleep.

6. The `pytest-asyncio` + `asyncio_mode = "auto"` setting (pyproject.toml) applies to
   async tests; the watch loop tests are synchronous and use regular `def test_...`.

### 8. Status file conventions — model from `state_store.py`

**Source:** [VERIFIED: whilly/state_store.py lines 22-85]
[VERIFIED: whilly/pause_control.py lines 15-24]

`StateStore.save()` uses the atomic tempfile-rename pattern:
```python
fd, tmp_path = tempfile.mkstemp(dir=str(dir_path), suffix=".tmp", prefix=".whilly_state_")
os.write(fd, content.encode("utf-8"))
os.close(fd)
os.replace(tmp_path, self.state_file)
```

The watch status writer must use the same pattern. `StateStore.load()` also implements
a 24-hour staleness check — the watch status reader (`watch-status` command) should
check `state == "running"` and optionally warn if `started_at` is very old (likely stale
process), but the 24-hour hard expiry only makes sense for crash-recovery state, not
watch status. The watch status file is always current.

---

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| Jira HTTP fetch + normalization | Custom HTTP calls in loop | `collect_jira_work_snapshot` | Already handles ADF, comments, changelog, links, repo hints |
| DB audit persistence | Custom SQL | `append_jira_work_event` via `TaskRepository` | Table schema, pool management already exist |
| Session upsert | Custom SQL | `persist_jira_work_snapshot` → `upsert_jira_work_session` | Hashing, indexing, state machine already implemented |
| Credential gate | Custom config reading | `_ensure_jira_config` from `cli/jira.py` | Handles env vars, toml section, interactive prompt, secret resolution |
| Log dir resolution | Hardcoded path | `_log_dir()` from `whilly.llm_ops` | Respects `WHILLY_LOG_DIR` env var |
| Pause check | Custom file-check | `PauseControl.is_paused()` / `get_pause_info()` | File protocol already defined |
| Readiness verdict | Custom probe | `_read_jira_work_readiness(plan_path)` | Reads from plan JSON; `probe_code_readiness` for live check |

**Key insight:** Every non-trivial piece of this phase is already implemented and tested.
The loop itself is the new code — everything it calls is existing.

---

## Common Pitfalls

### Pitfall 1: Missing `--interactive-config` on watch subparser
**What goes wrong:** `_ensure_jira_config` calls `bool(args.interactive_config)`, which
raises `AttributeError` if the watch subparser didn't add the flag.
**Why it happens:** Phase 19 hit this exact bug; the fix is documented in STATE.md.
**How to avoid:** Add both `--interactive-config` (store_true) and `--no-interactive-config`
(store_true) to the watch subparser, identically to the smoke subparser (jira.py:308-318).
**Warning signs:** `AttributeError: Namespace object has no attribute 'interactive_config'`.

### Pitfall 2: Using `asyncio.Event` for the synchronous loop stop
**What goes wrong:** `asyncio.Event.set()` from a `signal.signal` handler (which runs in
the main thread, not an asyncio task) is not safe and does not wake `asyncio.sleep`.
**Why it happens:** Copying the async worker pattern directly without noting that the watch
loop is synchronous.
**How to avoid:** Use `threading.Event` for the stop event; use `stop_event.wait(timeout=N)`
for the interruptible sleep. The `asyncio.run(...)` calls for DB persist are one-shot and
don't need the event.
**Warning signs:** `SIGTERM` doesn't stop the loop until the current sleep expires.

### Pitfall 3: PID file leftover from crash → false "already running" block
**What goes wrong:** If the previous watcher was killed with SIGKILL (no cleanup), the PID
file exists with a stale PID. The next `watch` invocation refuses to start.
**Why it happens:** PID file not cleaned on crash.
**How to avoid:** On start, if PID file exists, check `os.kill(pid, 0)` — if that raises
`OSError(ESRCH)`, the process is gone; overwrite the stale PID file. See Pattern 2.
**Warning signs:** "whilly jira watch: another watcher is already running (pid=XXXX)" when
no watcher process exists.

### Pitfall 4: DB persist called with `asyncio.run` inside an already-running event loop
**What goes wrong:** `asyncio.run()` raises `RuntimeError: This event loop is already running`
if called from within a coroutine or from a thread that has an active asyncio loop.
**Why it happens:** In the synchronous watch loop this is not an issue, but watch-loop tests
that use `pytest-asyncio` with `asyncio_mode = "auto"` run inside an asyncio loop.
**How to avoid:** The watch loop is synchronous, so `asyncio.run()` is safe. Tests for the
DB-persist helper should be `async def` tests calling `await _persist_watch_event(...)` directly.
**Warning signs:** `RuntimeError: This event loop is already running` in tests.

### Pitfall 5: `_run_intake` / `_run_poll` import dependency cycle if dispatch is inlined
**What goes wrong:** `jira_watch_loop.py` importing from `whilly.cli.jira` directly creates
a mutual import if `jira.py` also imports from `jira_watch_loop.py`.
**Why it happens:** `cli/jira.py` is both the entry point and the implementation module.
**How to avoid:** `jira_watch_loop.py` should not import from `whilly.cli.jira`. Instead,
the dispatch path (if --dispatch) should accept the runner callables as injected parameters
(same pattern as `plan_runner`, `runner` in `_run_intake`).
**Warning signs:** `ImportError: cannot import name 'X' from partially initialized module`.

### Pitfall 6: Backoff applied to the pre-sleep interval, causing drift
**What goes wrong:** Adding backoff to the interval before sleeping means that on the next
*success*, the sleep is still the full backoff duration before resetting. The sequence should
be: sleep(interval) → collect → on failure → next sleep = interval + backoff.
**Why it happens:** Applying backoff to the wrong side of the sleep.
**How to avoid:** Apply backoff to the *current* cycle's sleep, then reset after a successful
collect: `current_sleep = interval + consecutive_failures_backoff`. Reset `consecutive_failures = 0`
on success.

---

## Code Examples

### Watch loop skeleton (verified against codebase patterns)

```python
# Source: patterns from whilly/worker/main.py + whilly/state_store.py + whilly/cli/jira.py
import json
import logging
import os
import signal
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from whilly.jira_watch import collect_jira_work_snapshot
from whilly.llm_ops import _log_dir
from whilly.pause_control import PauseControl

log = logging.getLogger(__name__)

_BACKOFF_SEQUENCE = (5, 10, 20, 40, 60)


def _watch_dir() -> Path:
    return _log_dir() / "watch"


def _status_path() -> Path:
    return _watch_dir() / "jira-watch-status.json"


def _pid_path() -> Path:
    return _watch_dir() / "jira-watch.pid"


def _write_status(status: dict[str, Any]) -> None:
    """Atomic overwrite of the status file (model: StateStore.save)."""
    d = _status_path().parent
    d.mkdir(parents=True, exist_ok=True)
    content = json.dumps(status, ensure_ascii=False, indent=2) + "\n"
    fd, tmp = tempfile.mkstemp(
        dir=str(d), suffix=".tmp", prefix=".jira-watch-status-"
    )
    try:
        os.write(fd, content.encode("utf-8"))
        os.close(fd)
        os.replace(tmp, _status_path())
    except BaseException:
        os.close(fd)
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
```

### _ensure_jira_config call in watch (must include args.interactive_config)

```python
# Source: verified against whilly/cli/jira.py lines 1253-1297
config_rc = _ensure_jira_config(
    args,   # args must have .interactive_config and .no_interactive_config attrs
    config_reader=effective_config_reader,
    env=effective_env,
    prompt=prompt or input,
    secret_prompt=secret_prompt or getpass.getpass,
    browser_opener=browser_opener or webbrowser.open,
    stdin_isatty=stdin_isatty or sys.stdin.isatty,
    command_label="whilly jira watch",
)
if config_rc != EXIT_OK:
    return EXIT_CONFIG_MISSING
```

### PauseControl gate in the cycle

```python
# Source: whilly/pause_control.py lines 32-43
pc = PauseControl()  # .whilly_pause in cwd
if pc.is_paused():
    info = pc.get_pause_info() or {}
    reason = info.get("reason", "unknown")
    log.info("whilly jira watch: global pause active (%s); skipping dispatch", reason)
    # optional: append watch.paused audit event
    continue  # skip dispatch, continue next cycle
```

---

## State of the Art

| Old Approach | Current Approach | When Changed | Impact |
|--------------|------------------|--------------|--------|
| `time.sleep(N)` in watch loops (github_projects.py:722) | `threading.Event.wait(timeout=N)` | Phase 20 (new) | SIGINT/SIGTERM wake the sleep immediately |
| No jira watcher | `whilly jira watch` foreground daemon | Phase 20 (new) | Operators can run continuous Jira intake |
| Manual one-shot `whilly jira poll` | Watch loop wraps poll automatically | Phase 20 (new) | Reduces operator toil for repeated polling |

**Not deprecated by this phase:**
- `whilly jira poll` remains the one-shot command — watch wraps it logically
- `SchedulerWorker` / JQL-rule intake remains independent (locked decision)

---

## Assumptions Log

| # | Claim | Section | Risk if Wrong |
|---|-------|---------|---------------|
| A1 | The DB-based `ControlState` check is an optional "also check" for the watcher; the file-based PauseControl is the hard gate | Research Finding 2 | Watch could miss a DB-level pause; but CONTEXT.md explicitly names `.whilly_pause` as the gate |
| A2 | The `--dispatch` path should accept `--readiness-repo-path` / `--allow-unready-run` forwarded from the watch args | Research Finding 3 | Dispatch might gate on stale plan readiness if no live probe is done |
| A3 | The watch loop is synchronous (uses `signal.signal` + `threading.Event`), not async | Architecture section | If future phases need concurrent multi-issue polling, loop would need asyncio refactor |
| A4 | Exact field names in `jira-watch-status.json` are Claude's discretion | Standard Stack section | No contract impact — not consumed by other code |

**Low-risk assumptions:** A1 is explicitly supported by CONTEXT.md. A2 is consistent with
the Phase 17 `_run_intake` precedent. A3 is driven by the fact that `collect_jira_work_snapshot`
is synchronous. A4 is explicitly Claude's discretion.

---

## Open Questions

1. **Multi-issue watch: same event loop or serial?**
   - What we know: `--issue KEY` is repeatable; each call to `collect_jira_work_snapshot`
     is synchronous and takes ~2-5s.
   - What's unclear: Should issues be polled serially in one loop iteration, or in parallel?
   - **RESOLVED recommendation:** Serial within one cycle. With 1-5 issues and 300s interval,
     serial polling is fast enough. Parallel would require `asyncio.gather` or `ThreadPoolExecutor`
     and complicates error attribution. Deferred to future if needed.

2. **Watch status command: does it need `--json` flag?**
   - What we know: `whilly jira smoke --json` and `whilly jira poll --json` exist.
   - What's unclear: Is structured JSON output needed for `watch-status` from the start?
   - **RESOLVED recommendation:** Add `--json` to `watch-status` subparser. It's trivial to
     add and follows the established CLI pattern. Human-readable default, JSON on request.

3. **PID file path: absolute or relative to cwd?**
   - What we know: `_log_dir()` resolves via `WHILLY_LOG_DIR` env or defaults to `whilly_logs/`
     relative to cwd.
   - What's unclear: If the operator runs `whilly jira watch` from different directories,
     will the PID file location differ?
   - **RESOLVED recommendation:** Always use `_log_dir() / "watch" / "jira-watch.pid"` so it
     follows `WHILLY_LOG_DIR`. Document this in `--help`. Operators who use a non-default
     `WHILLY_LOG_DIR` should set it consistently.

---

## Environment Availability

No external dependencies beyond what Phase 19 already requires. All needed tools are available.

| Dependency | Required By | Available | Version | Fallback |
|------------|------------|-----------|---------|----------|
| Python 3.10+ | Loop, signal, threading | ✓ | 3.12 (CI matrix) | — |
| `JIRA_SERVER_URL`, `JIRA_API_TOKEN` | `collect_jira_work_snapshot` | operator-provided | — | `_ensure_jira_config` handles missing creds |
| `WHILLY_DATABASE_URL` | DB audit persist | optional | — | Best-effort: skipped if not set |
| `WHILLY_LOG_DIR` | Status/PID file location | optional | — | Defaults to `whilly_logs/` |
| `WHILLY_JIRA_WATCH_INTERVAL` | Default poll interval | optional | — | Defaults to 300s |

**Missing dependencies with no fallback:** None.

---

## Validation Architecture

### Test Framework

| Property | Value |
|----------|-------|
| Framework | pytest + pytest-asyncio (pyproject.toml) |
| Config file | pyproject.toml `[tool.pytest.ini_options]` |
| Quick run command | `pytest tests/unit/cli/test_jira_watch_loop.py -q` |
| Full suite command | `pytest -q` |

### Phase Requirements → Test Map

| Req ID | Behavior | Test Type | Automated Command | File Exists? |
|--------|----------|-----------|-------------------|-------------|
| WATCH-01 | Loop calls snapshot_collector on each cycle | unit | `pytest tests/unit/cli/test_jira_watch_loop.py::test_watch_loop_calls_collector_per_cycle -x` | ❌ Wave 0 |
| WATCH-01 | Interval is configurable via --interval and WHILLY_JIRA_WATCH_INTERVAL | unit | `pytest tests/unit/cli/test_jira_watch_loop.py::test_watch_interval_env_override -x` | ❌ Wave 0 |
| WATCH-02 | Stop event terminates loop gracefully | unit | `pytest tests/unit/cli/test_jira_watch_loop.py::test_watch_stop_event_terminates_loop -x` | ❌ Wave 0 |
| WATCH-02 | Status file written after each cycle | unit | `pytest tests/unit/cli/test_jira_watch_loop.py::test_watch_status_file_written -x` | ❌ Wave 0 |
| WATCH-02 | watch-status command reads and prints status file | unit | `pytest tests/unit/cli/test_jira_watch_loop.py::test_watch_status_command -x` | ❌ Wave 0 |
| WATCH-02 | Backoff increases on consecutive failures | unit | `pytest tests/unit/cli/test_jira_watch_loop.py::test_watch_backoff_sequence -x` | ❌ Wave 0 |
| WATCH-02 | Single-instance guard blocks second watcher | unit | `pytest tests/unit/cli/test_jira_watch_loop.py::test_watch_single_instance_guard -x` | ❌ Wave 0 |
| WATCH-03 | Paused state skips dispatch | unit | `pytest tests/unit/cli/test_jira_watch_loop.py::test_watch_honors_global_pause -x` | ❌ Wave 0 |
| WATCH-03 | Unready readiness verdict skips dispatch | unit | `pytest tests/unit/cli/test_jira_watch_loop.py::test_watch_readiness_gate_blocks_dispatch -x` | ❌ Wave 0 |
| WATCH-01,02,03 | `whilly jira watch` ACTION wired in run_jira_command | unit | `pytest tests/unit/test_jira_cli.py::test_jira_watch_action_dispatches -x` | ❌ Wave 0 |

### Sampling Rate

- **Per task commit:** `pytest tests/unit/cli/test_jira_watch_loop.py -q`
- **Per wave merge:** `pytest tests/unit/ -q`
- **Phase gate:** `pytest -q` full suite green before `/gsd-verify-work`

### Wave 0 Gaps

- [ ] `tests/unit/cli/test_jira_watch_loop.py` — covers WATCH-01, WATCH-02, WATCH-03
- [ ] `whilly/cli/jira_watch_loop.py` — new module (implementation, not test gap)

*(Existing test infrastructure — conftest, pytest config — is complete. Only the new
test file and implementation file are missing.)*

---

## Security Domain

> `security_enforcement` not set to false in config.

### Applicable ASVS Categories

| ASVS Category | Applies | Standard Control |
|---------------|---------|-----------------|
| V2 Authentication | no | — (daemon uses existing Jira token from env) |
| V3 Session Management | no | — (stateless per-cycle; no HTTP sessions) |
| V4 Access Control | no | — (local daemon, no multi-user surface) |
| V5 Input Validation | yes | `parse_jira_key` (already used in poll/smoke) |
| V6 Cryptography | no | — (no key generation; Jira token is operator-managed) |

### Known Threat Patterns for Watch Daemon

| Pattern | STRIDE | Standard Mitigation |
|---------|--------|---------------------|
| Issue key injection via `--issue` | Tampering | `parse_jira_key()` is already called in `collect_jira_work_snapshot` → `parse_jira_key(jira_ref)` at line 61; validated before any HTTP call |
| PID file TOCTOU (stale PID overwrite) | Spoofing | `os.kill(pid, 0)` liveness check before overwrite; atomic rename prevents partial writes |
| Status file containing credentials | Information Disclosure | Status file must NOT log `JIRA_API_TOKEN` or DSN; only non-secret fields (pid, cycle count, timestamps) |
| Path traversal via `--issue` URL form | Tampering | `parse_jira_key` rejects non-key inputs; already verified in smoke tests |

---

## Sources

### Primary (HIGH confidence)

- [VERIFIED: whilly/cli/jira.py] — `_run_poll`, `_run_jira_smoke`, `_ensure_jira_config`,
  `build_jira_parser`, `run_jira_command`, `_read_jira_work_readiness`, `_persist_smoke_event`
- [VERIFIED: whilly/jira_watch.py] — `collect_jira_work_snapshot`, `persist_jira_work_snapshot`,
  `JiraWorkSnapshot`, `JiraWorkStateRepo` protocol
- [VERIFIED: whilly/pause_control.py] — `PauseControl.is_paused()`, `get_pause_info()`
- [VERIFIED: whilly/worker/main.py] — `_install_signal_handlers`, `_make_shutdown_handler`,
  `run_worker`, `_SHUTDOWN_SIGNALS`
- [VERIFIED: whilly/adapters/db/repository.py] — `append_jira_work_event`, `ControlState`,
  `get_control_state`, `_INSERT_JIRA_WORK_EVENT_SQL`
- [VERIFIED: whilly/state_store.py] — atomic write pattern, status file conventions
- [VERIFIED: whilly/llm_ops.py] — `_log_dir()`, `DEFAULT_LOG_DIR`, `LOG_DIR_ENV`
- [VERIFIED: whilly/cli/smoke.py] — `EXIT_CONFIG_MISSING`, `_smoke_report_dir`, `_log_dir`
- [VERIFIED: whilly/scheduler/worker.py] — `SchedulerWorker.stop()`, `asyncio.Event` stop pattern
- [VERIFIED: whilly/github_projects.py:700-734] — sync watch loop prior art (weaker pattern)
- [VERIFIED: tests/unit/test_worker_shutdown.py] — injectable stop event test pattern
- [VERIFIED: tests/unit/test_jira_watch.py] — `_FakeRepo` inject pattern
- [VERIFIED: tests/unit/cli/test_jira_smoke.py] — smoke CLI test pattern
- [VERIFIED: pyproject.toml `[tool.pytest.ini_options]`] — `asyncio_mode = "auto"`
- [VERIFIED: .planning/config.json `workflow.nyquist_validation: true`]

### Secondary (MEDIUM confidence)

- [VERIFIED: .planning/phases/20-jira-watcher-daemon/20-CONTEXT.md] — locked decisions

### Tertiary (LOW confidence)

- [ASSUMED] — PID-file liveness check via `os.kill(pid, 0)` as single-instance guard
  (no prior art in codebase; standard Unix pattern)

---

## Metadata

**Confidence breakdown:**
- Standard stack: HIGH — all components verified in codebase, no new packages
- Architecture: HIGH — loop structure follows existing patterns directly
- Pitfalls: HIGH — most derived from verified code + Phase 19 STATE.md decisions
- Test patterns: HIGH — injected-collector + injectable-stop pattern verified in
  `test_worker_shutdown.py` and `test_jira_watch.py`
- Signal handling: HIGH — verified in `whilly/worker/main.py`; sync equivalent is stdlib

**Research date:** 2026-06-12
**Valid until:** 2026-07-12 (stable domain — stdlib + internal code, no external API churn)
