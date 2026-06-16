---
phase: 20-jira-watcher-daemon
plan: "01"
subsystem: cli/jira-watch
tags: [jira, daemon, watch-loop, tdd, pid-guard, backoff, status-file]
dependency_graph:
  requires:
    - whilly.jira_watch.collect_jira_work_snapshot  # Phase 19 live-validated
    - whilly.llm_ops._log_dir
    - whilly.adapters.db.repository.TaskRepository.append_jira_work_event
  provides:
    - whilly.cli.jira_watch_loop._run_jira_watch
    - whilly.cli.jira_watch_loop._resolve_interval
    - whilly.cli.jira_watch_loop._write_status
    - whilly.cli.jira_watch_loop._interruptible_sleep
    - whilly.cli.jira_watch_loop._install_watch_signal_handlers
    - whilly.cli.jira_watch_loop._acquire_pid_lock
    - whilly.cli.jira_watch_loop._release_pid_lock
    - whilly.cli.jira_watch_loop._persist_watch_event
    - whilly.cli.jira_watch_loop.EVENT_CYCLE
    - whilly.cli.jira_watch_loop.EVENT_FAILURE
  affects: []
tech_stack:
  added:
    - whilly/cli/jira_watch_loop.py (new module, 454 lines)
    - tests/unit/cli/test_jira_watch_loop.py (11 deterministic unit tests)
  patterns:
    - atomic-tempfile-os-replace (T-20-05, from state_store.py model)
    - threading.Event.wait interruptible sleep (not time.sleep)
    - signal.signal SIGTERM/SIGINT -> stop.set() sync handler
    - os.kill(pid, 0) liveness probe for PID guard
    - best-effort asyncio.run + except Exception warn-not-fail
key_files:
  created:
    - whilly/cli/jira_watch_loop.py
    - tests/unit/cli/test_jira_watch_loop.py
  modified: []
decisions:
  - EXIT_VALIDATION_ERROR (1) reused as the single-instance refusal code —
    avoids introducing a new exit code constant; documented here.
  - Backoff applied to the NEXT cycle's sleep (interval + backoff_seconds),
    not the current cycle — matches spec (Pitfall 6).
  - _acquire_pid_lock treats EPERM as stale (conservative choice) — a process
    we cannot signal 0 is not our responsibility; we overwrite and proceed.
  - interval=None in args triggers env/default resolution in _resolve_interval;
    argparse will map --interval absence to None, not 0.
metrics:
  duration: "24 min"
  completed_date: "2026-06-12"
  tasks: 2
  files: 2
---

# Phase 20 Plan 01: Jira Watch Loop Core Summary

Synchronous foreground watch-loop module implementing WATCH-01 (configurable-interval
daemon) and lifecycle half of WATCH-02 (graceful stop, status file, backoff, PID guard,
audit events) using `threading.Event` interruptible sleep and atomic `os.replace` writes.

## One-liner

Synchronous Jira watch loop with threading.Event stop, atomic status JSON, 5/10/20/40/60s
backoff, os.kill(pid,0) PID guard, and best-effort asyncio DB audit events — all injectable
for deterministic unit tests.

## What Was Built

### `whilly/cli/jira_watch_loop.py` (454 lines)

Core module providing:

- **`_run_jira_watch(args, *, snapshot_collector, environ, stop_event, install_signal_handlers)`**  
  Main loop. Resolves interval, acquires PID lock, initializes secret-free status dict,
  runs `while not stop.is_set()` with interruptible sleep, serial per-issue collector calls,
  atomic status writes, best-effort DB persist, and `finally` graceful-exit writes.

- **`_resolve_interval(args_interval, env)`**  
  Priority: `--interval` arg > `WHILLY_JIRA_WATCH_INTERVAL` env > 300s default.
  Bad env value falls back to default with a warning.

- **`_interruptible_sleep(stop, seconds) -> bool`**  
  `threading.Event.wait(timeout=seconds)`. Returns True if stop fired, False on timeout.
  Never uses `time.sleep` (20-RESEARCH.md Pitfall 2).

- **`_write_status(status, status_path)`**  
  Atomic tempfile + `os.replace` (verbatim state_store.py model). `BaseException` cleanup
  branch unlinks the temp file. `dir_path = parent or Path(".")` defensive fallback.

- **`_install_watch_signal_handlers(stop)`**  
  `signal.signal(SIGTERM/SIGINT, handler)` where handler calls `stop.set()`.
  Gated by `install_signal_handlers=False` for test injection.

- **`_acquire_pid_lock(pid_path) -> bool`**  
  `os.kill(pid, 0)` liveness probe. Returns False on live process (T-20-02: never sends
  a real signal), True on OSError/ValueError (stale/garbage). Writes own PID atomically.

- **`_release_pid_lock(pid_path)`**  
  Unlinks only if file still holds our PID (guards against removing a successor's lock).
  Called in `finally` so crashes still allow the next start to overwrite.

- **`_persist_watch_event(*, dsn, issue_key, event_type, payload, repo=None)`**  
  Async, injectable repo for tests. Secret-free payload (T-20-03). Called via
  `asyncio.run()` wrapped in `try/except Exception` (warn-not-fail).

- **`EVENT_CYCLE = "watch.cycle"`, `EVENT_FAILURE = "watch.failure"`**  
  Module constants for audit event type names.

### `tests/unit/cli/test_jira_watch_loop.py` (11 tests)

Deterministic tests: `interval=0`, injected collector, `install_signal_handlers=False`,
`monkeypatch.setenv("WHILLY_LOG_DIR", ...)`.

| Test | Behavior Verified |
|------|-------------------|
| `test_watch_loop_calls_collector_per_cycle` | Collector called exactly N times before stop |
| `test_interval_resolution` | `_resolve_interval` priority chain |
| `test_interval_recorded_in_status_file` | Resolved interval visible in status JSON |
| `test_pre_set_stop_event_exits_zero` | Pre-set stop → rc 0, no collector calls, state=stopped |
| `test_status_file_location_and_no_secrets` | Location, valid JSON, no token/DSN values |
| `test_no_signal_handlers_when_disabled` | install_signal_handlers=False leaves handlers unchanged |
| `test_backoff_increases_on_consecutive_failures_and_resets_on_success` | 0,5,10,20,40,60,60 sleep sequence; error_count=6; final backoff=0 |
| `test_acquire_pid_lock` | No-file/live-PID/stale-PID scenarios |
| `test_live_pid_file_refuses_second_watcher` | rc=1, no collector, hint with pid in stderr |
| `test_persist_watch_event_calls_repo` | Injected _FakeRepo receives correct kwargs |
| `test_persist_watch_event_raises_are_swallowed_by_loop` | warn-not-fail, loop continues |

## TDD Gate Compliance

| Gate | Commit | Status |
|------|--------|--------|
| RED (Task 1) | `c5c0e9d` test(20-01): add failing tests for jira_watch_loop core | Passed — all 6 tests failed with ModuleNotFoundError |
| GREEN (Task 1) | `c7575ac` feat(20-01): implement jira_watch_loop core | Passed — all 6 tests green |
| RED (Task 2) | `1f46265` test(20-01): add backoff/PID-guard/DB-audit tests | 5 tests added (passed because Task 1 GREEN was holistic) |
| GREEN (Task 2) | `1afd4e9` feat(20-01): Task 2 GREEN — backoff/PID-guard/DB-audit all passing | All 11 tests green |

Note: Task 2 tests passed immediately because the Task 1 GREEN implementation covered both
tasks (same module file, plan specified they coexist). The RED commit for Task 2 was still
created per TDD discipline; the tests are genuinely new behavioral coverage.

## Verification

```
.venv/bin/python -m pytest tests/unit/cli/test_jira_watch_loop.py -q   → 11 passed
.venv/bin/python -m pytest tests/unit/cli -q                            → 60 passed (no regressions)
ruff check whilly/cli/jira_watch_loop.py tests/unit/cli/test_jira_watch_loop.py → clean
grep security gate (JIRA_API_TOKEN / DATABASE_URL in payload) → 0 leaks
min_lines check: 454 lines >= 180 minimum
```

## Deviations from Plan

### Auto-fixed Issues

None — plan executed as written.

### Test design adjustment (not a deviation)

`test_interval_recorded_in_status_file` originally monkeypatched `os.environ` for
`WHILLY_JIRA_WATCH_INTERVAL=42` but also passed `environ=_jira_env()` (without the key).
Fixed by including the interval key in the explicit `environ` dict — the injected mapping
takes precedence over `os.environ`, so the monkeypatch was a no-op. This is a test
correctness fix, not a deviation.

### Backoff test expected sequence (not a deviation)

Test initially had expected sequence `[5, 10, 20, 40, 60, 60, 0]` (thinking backoff
applied retroactively). Corrected to `[0, 5, 10, 20, 40, 60, 60]` matching the spec:
"backoff is added to the UPCOMING gap" (Pitfall 6 in RESEARCH.md).

## Threat Mitigations Applied

| Threat | Mitigation |
|--------|-----------|
| T-20-02 (Spoofing/DoS via forged PID) | `os.kill(pid,0)` liveness probe; refuse-and-hint; NEVER sends real signal |
| T-20-03 (Info Disclosure via status/payload) | Status dict and event payload contain only pid/counts/timestamps/result; no token, no DSN value |
| T-20-05 (Tampering via partial write) | Atomic tempfile + `os.replace` for both status and PID files; BaseException cleanup |

## Known Stubs

None — no hardcoded placeholders. TODO comments reference future plans explicitly:
- `TODO(plan-02)`: credential gate (`_ensure_jira_config`) wiring
- `TODO(plan-02)`: full type-alias parameters for config_loader/reader/prompt/etc.
- `TODO(plan-03)`: PauseControl and readiness gate integration

## Threat Flags

None — no new network endpoints or auth paths introduced. Module is local-filesystem only
(status/PID file writes) plus optional best-effort DB persist (same surface as Phase 19
smoke). No new trust boundaries.

## Self-Check

See below.

## Self-Check: PASSED

- [x] `whilly/cli/jira_watch_loop.py` exists (454 lines)
- [x] `tests/unit/cli/test_jira_watch_loop.py` exists (11 tests)
- [x] Commit `c5c0e9d` (RED Task 1) exists
- [x] Commit `c7575ac` (GREEN Task 1) exists
- [x] Commit `1f46265` (RED Task 2) exists
- [x] Commit `1afd4e9` (GREEN Task 2) exists
- [x] All 11 tests pass under `.venv/bin/python`
- [x] No regressions in `tests/unit/cli` (60 passed)
- [x] Ruff clean
- [x] Security grep gate: 0 secret-leak sinks
