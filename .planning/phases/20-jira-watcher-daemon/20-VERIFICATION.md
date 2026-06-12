---
phase: 20-jira-watcher-daemon
verified: 2026-06-12T00:00:00Z
status: passed
score: 14/14
overrides_applied: 0
human_verification:
  - test: "Run `whilly jira watch --issue ABC-123 --interval 30` for 2 cycles (60s) against real Jira and inspect output"
    expected: "Daemon starts, polls immediately, logs cycle results, writes whilly_logs/watch/jira-watch-status.json; SIGINT causes graceful stop with state=stopped"
    why_human: "Live Jira HTTP credentials required; status file contents and signal behavior cannot be verified programmatically without a running server"
---

# Phase 20: Jira Watcher Daemon Verification Report

**Phase Goal:** Operators can run a continuous Jira intake daemon that wraps the validated one-shot poll cycle, with full lifecycle controls and the existing global-pause and readiness gates honored before any autonomous work is dispatched.

**Verified:** 2026-06-12T00:00:00Z
**Status:** human_needed
**Re-verification:** No — initial verification

---

## Goal Achievement

### Observable Truths

| # | Truth | Status | Evidence |
|---|-------|--------|----------|
| 1 | `whilly jira watch` is a registered ACTION that dispatches to `_run_jira_watch` through `run_jira_command` | VERIFIED | `jira.py:505-629` — lazy import branch for `action == "watch"`, confirmed by `test_watch_dispatch_invokes_run_jira_watch` |
| 2 | `whilly jira watch-status` reads `jira-watch-status.json` and prints it (human-readable default, `--json` structured) | VERIFIED | `jira.py:630-633` lazy-imports `_run_watch_status`; `jira_watch_loop.py:636-705` implements human + JSON output; 3 CLI tests pass |
| 3 | The watch subparser declares `--interval`, `--issue` (repeatable), `--timeout`, `--dispatch`, `--readiness-repo-path`, `--allow-unready-run`, `--interactive-config`, `--no-interactive-config` | VERIFIED | `jira.py:361-418` — all 8 flags present; parser round-trip confirms `parse_args(['watch','--issue','ABC-123'])` returns all expected defaults |
| 4 | Configurable-interval polling without manual intervention (WATCH-01) | VERIFIED | `_resolve_interval` (lines 98-120): `--interval` > `WHILLY_JIRA_WATCH_INTERVAL` > 300s; `test_interval_resolution` + `test_interval_recorded_in_status_file` pass |
| 5 | Graceful stop + status inspection (WATCH-02) | VERIFIED | `stop_event.set()` triggers exit with `state=stopped`; status JSON atomically written at `_log_dir()/watch/jira-watch-status.json`; `_run_watch_status` reads + displays it; `test_pre_set_stop_event_exits_zero` passes |
| 6 | Exponential backoff 5/10/20/40/60s with audit events (WATCH-02) | VERIFIED | `_BACKOFF_SEQUENCE = (5,10,20,40,60)` at line 60; per-issue accounting at lines 498-532 (WR-01 fix); `test_backoff_increases_on_consecutive_failures_and_resets_on_success` passes |
| 7 | Pause active → read-only polling continues, no dispatch, `watch.paused` event recorded (WATCH-03) | VERIFIED | Pause gate at lines 541-574: collector called before gate, `continue` prevents dispatch, `EVENT_PAUSED` emitted; `test_pause_gate_collector_called_dispatch_not` + `test_pause_gate_emits_watch_paused_event` pass |
| 8 | Readiness unsatisfied → `watch.block` recorded + wait (WATCH-03) | VERIFIED | `_run_dispatch_if_ready` lines 731-767: gate fails CLOSED (None readiness → blocks); `test_readiness_gate_blocks_dispatch_with_block_event` + 5 additional fail-closed tests pass |
| 9 | Production dispatch is wired through the Phase-17-gated `jira run` path, default-off | VERIFIED | `jira.py:540-629` — `dispatch_runner=None` without `--dispatch`; closure builds complete `argparse.Namespace` for `_run_argv` (CR-01 fix); `test_watch_dispatch_default_off` passes |
| 10 | `docs/Whilly-Usage.md` has a watch section and docs regression tests pin it | VERIFIED | `## Jira watcher daemon` section at line 103; 6 occurrences of `whilly jira watch`; 5 docs regression tests pass |
| 11 | Credential gate (`_ensure_jira_config`) runs before the daemon loop | VERIFIED | `jira.py:510-536` — config gate runs before `_run_jira_watch`; exits `EXIT_CONFIG_MISSING` on failure (WR-03 fix) |
| 12 | `watch-status` detects stale/crashed watcher (not falsely reporting `running`) | VERIFIED | `jira_watch_loop.py:672-681` — `os.kill(pid,0)` probe when `state=running`; `ProcessLookupError` → `stale (pid N not running)` (WR-05 fix) |
| 13 | Honest dispatch audit: `watch.dispatch` only emitted on `rc==0` | VERIFIED | `_run_dispatch_if_ready` lines 801-824: `EVENT_DISPATCH` only when `dispatch_ok`; exception → `EVENT_FAILURE`; `test_dispatch_event_only_on_success_rc` passes (CR-03 fix) |
| 14 | No circular import from `whilly.cli.jira` in `jira_watch_loop.py` | VERIFIED | `grep -c "from whilly.cli.jira" jira_watch_loop.py` → 1 (comment only, line 43) |

**Score:** 14/14 truths verified

---

### Required Artifacts

| Artifact | Expected | Status | Details |
|----------|----------|--------|---------|
| `whilly/cli/jira_watch_loop.py` | Core loop, status writer, PID guard, backoff, signal seam, DB persist helper | VERIFIED | 832 lines; all required functions present: `_run_jira_watch`, `_resolve_interval`, `_write_status`, `_interruptible_sleep`, `_install_watch_signal_handlers`, `_acquire_pid_lock`, `_release_pid_lock`, `_persist_watch_event`, `_read_watch_readiness`, `_evaluate_watch_readiness`, `_run_dispatch_if_ready`, `_run_watch_status` |
| `tests/unit/cli/test_jira_watch_loop.py` | Deterministic loop, interval, stop, status-file, backoff, single-instance, pause, dispatch tests | VERIFIED | 31 tests, all passing; contains `install_signal_handlers`, `whilly_pause`, `is_paused`, `PauseControl`, `ready_for_testing` |
| `whilly/cli/jira.py` | `watch` + `watch-status` subparsers, `run_jira_command` dispatch branches, lazy import | VERIFIED | watch subparser lines 361-418; dispatch at lines 505-634 |
| `docs/Whilly-Usage.md` | Jira watcher daemon section: command, interval, status file, exit codes, gates | VERIFIED | `## Jira watcher daemon` at line 103; 6 occurrences of `whilly jira watch` |
| `tests/unit/test_docs_live_smoke.py` | Regression tests pinning the watch docs section | VERIFIED | 5 watcher tests pass: heading, commands, log dir, status file name, dispatch default-off |
| `docs/Whilly-Interfaces-and-Tasks.md` | Module contract section for `jira_watch_loop` | VERIFIED | `## 7. Module contract: whilly/cli/jira_watch_loop.py` at line 359; event types listed |

---

### Key Link Verification

| From | To | Via | Status | Details |
|------|----|-----|--------|---------|
| `jira.py run_jira_command` | `whilly.cli.jira_watch_loop._run_jira_watch` | lazy import in `action == "watch"` branch | VERIFIED | `jira.py:506` — `from whilly.cli.jira_watch_loop import _run_jira_watch` |
| `jira.py run_jira_command` | `whilly.cli.jira_watch_loop._run_watch_status` | lazy import in `action == "watch-status"` branch | VERIFIED | `jira.py:631` — `from whilly.cli.jira_watch_loop import _run_watch_status` |
| `jira_watch_loop.py` | `whilly.jira_watch.collect_jira_work_snapshot` | injectable default in `_run_jira_watch` | VERIFIED | `jira.py:625` passes `collect_jira_work_snapshot` as default `snapshot_collector` |
| `jira_watch_loop.py` | `whilly.llm_ops._log_dir` | status/pid path resolution under `watch/` dir | VERIFIED | `jira_watch_loop.py:37,80` — imported and used in `_watch_dir()` |
| `jira_watch_loop.py` | `threading.Event` | interruptible sleep / stop signal | VERIFIED | `jira_watch_loop.py:149-158` — `_interruptible_sleep` uses `stop.wait(timeout=seconds)` |
| `jira_watch_loop.py` | `whilly.pause_control.PauseControl` | `is_paused()` gate before dispatch | VERIFIED | `jira_watch_loop.py:38,422,541` — imported, instantiated, checked |
| `jira_watch_loop.py readiness gate` | `_evaluate_watch_readiness` → `probe_code_readiness` | `--readiness-repo-path` accepted as directory or plan JSON | VERIFIED | `jira_watch_loop.py:342-367` — fail-closed when `None`; `verdict="unknown"` blocks |
| `jira.py dispatch closure` | `_run_plan_worker(_run_argv(plan_id, run_namespace))` | complete namespace built for CR-01 fix | VERIFIED | `jira.py:614-621` — `argparse.Namespace` with explicit defaults for all `_run_argv` fields |

---

### Data-Flow Trace (Level 4)

| Artifact | Data Variable | Source | Produces Real Data | Status |
|----------|---------------|--------|-------------------|--------|
| `_run_jira_watch` (status dict) | `status["state"]`, `cycle_count`, `backoff_seconds` | `snapshot_collector` result + internal counters | Yes — updates each cycle from collector outcome | FLOWING |
| `_run_watch_status` | `data` (JSON) | `_status_path().read_text()` | Yes — reads the atomically-written status file | FLOWING |
| `_run_dispatch_if_ready` | `readiness` | `_evaluate_watch_readiness(readiness_repo_path)` → `probe_code_readiness` or `_read_watch_readiness` | Yes — calls Phase-17 readiness probe or reads plan JSON | FLOWING |

---

### Behavioral Spot-Checks

| Behavior | Command | Result | Status |
|----------|---------|--------|--------|
| Watch subparser round-trip | `python -c "from whilly.cli.jira import build_jira_parser; p=build_jira_parser(); ns=p.parse_args(['watch','--issue','ABC-123']); print(vars(ns))"` | All 9 expected fields present with correct defaults | PASS |
| `watch-status` subparser | `python -c "build_jira_parser().parse_args(['watch-status'])"` | `{'action': 'watch-status', 'json': False}` | PASS |
| 31 unit tests pass | `.venv/bin/python -m pytest tests/unit/cli/test_jira_watch_loop.py -q` | `31 passed` | PASS |
| 9 CLI watch tests pass | `.venv/bin/python -m pytest tests/unit/test_jira_cli.py -k watch -q` | `9 passed` | PASS |
| 5 docs regression tests | `.venv/bin/python -m pytest tests/unit/test_docs_live_smoke.py -k watch -q` | `5 passed` | PASS |
| Full unit suite | `.venv/bin/python -m pytest tests/unit -q` | `2494 passed, 2 skipped` | PASS |
| Ruff lint clean | `ruff check whilly/cli/jira_watch_loop.py whilly/cli/jira.py` | `All checks passed!` | PASS |
| Security: no secret leaks | `grep -n 'JIRA_API_TOKEN\|DATABASE_URL.*payload\|token.*status' jira_watch_loop.py` | 0 matches | PASS |
| No circular import | `grep -c "from whilly.cli.jira" jira_watch_loop.py` | `1` (comment only) | PASS |
| Dispatch call site count | `grep -c "dispatch_runner(" jira_watch_loop.py` | `1` (inside `_run_dispatch_if_ready`, under `if wants_dispatch`) | PASS |
| No debt markers (TBD/FIXME/XXX) | `grep -n "TBD\|FIXME\|XXX" jira_watch_loop.py jira.py` | 0 matches | PASS |

---

### Probe Execution

Step 7c: SKIPPED — no `scripts/*/tests/probe-*.sh` files found; phase produces a library module, not a standalone runnable pipeline.

---

### Requirements Coverage

| Requirement | Source Plan | Description | Status | Evidence |
|-------------|-------------|-------------|--------|----------|
| WATCH-01 | Plans 01, 03 | Operator can run a long-running `whilly jira watch` daemon that wraps the one-shot poll cycle on a configurable interval | SATISFIED | `_run_jira_watch` loop (plans 01/02), `whilly jira watch` CLI surface (plan 03), configurable interval resolved 3-way |
| WATCH-02 | Plans 01, 03 | Operator can start, stop, and inspect watcher status; transient failures retried with backoff and audit events | SATISFIED | Stop via `stop_event`/SIGTERM/SIGINT; status file with `state=running/stopped`; `watch-status` reader; 5/10/20/40/60s backoff; `EVENT_CYCLE`/`EVENT_FAILURE` audit events |
| WATCH-03 | Plans 02, 03 | Watcher honors global worker pause and code/test readiness gates before dispatching any autonomous work | SATISFIED | PauseControl gate (lines 541-574) — polls, no dispatch; readiness gate fails CLOSED (unknown = block); dispatch structurally gated behind `wants_dispatch` bool; `--dispatch` default False |

All 3 requirements from REQUIREMENTS.md Phase 20 mapping are SATISFIED.

---

### Anti-Patterns Found

| File | Line | Pattern | Severity | Impact |
|------|------|---------|----------|--------|
| None found | — | No TBD/FIXME/XXX debt markers, no stale TODOs, no hardcoded empty data, no placeholder returns | — | — |

All findings from the REVIEW.md (3 Critical + 7 Warning + 6 Info) were fixed in post-summary commits (`fix(20):` prefix). The Fix Status table in REVIEW.md documents each finding's resolution commit. Verification confirms all fixes are present in current code:

- CR-01 (production closure AttributeError): Complete `argparse.Namespace` built at `jira.py:614-621`
- CR-02 (readiness fail-open): `_evaluate_watch_readiness` fails CLOSED; `None` → `verdict="unknown"` → block at `jira_watch_loop.py:731-767`
- CR-03 (fabricated dispatch audit): `EVENT_DISPATCH` only when `dispatch_rc == EXIT_OK` at line 823
- WR-01 (multi-issue backoff): Per-issue `issue_results`/`failed_issues` tracking at lines 498-533
- WR-02 (TOCTOU + EPERM): `O_CREAT|O_EXCL` creation, `PermissionError` → refuse at lines 220-235
- WR-03 (credential gate missing): `_ensure_jira_config` at `jira.py:522-536` before loop entry
- WR-04 (first poll delayed): `first_cycle=True` guard at lines 479-489; polls immediately on start
- WR-05 (stale running state): `os.kill(pid,0)` probe in `_run_watch_status` at lines 674-681
- WR-06 (dispatch dedup): `dispatched[issue_ref] = current_hash` at line 804; unchanged snapshots skipped
- WR-07 (raw ref in closure): `parse_jira_key(issue_ref)` at `jira.py:578`

---

### Human Verification Required

### 1. Live Jira Daemon Run

**Test:** On this machine (real credentials available), run:
```
whilly jira watch --issue <real_jira_key> --interval 30 --no-interactive-config
```
Let it run for at least 2 cycles (60s), then SIGINT (Ctrl-C).

**Expected:**
- Daemon starts without error and logs the first poll result immediately (no 30s wait)
- After 30s, a second cycle runs
- `whilly_logs/watch/jira-watch-status.json` is written with `state=running`, non-null `last_poll_at`, `cycle_count=2`
- After SIGINT, status file shows `state=stopped` and `stopped_at` timestamp
- Exit code is 0

**Why human:** Requires live Jira HTTP credentials; the watcher process is a foreground daemon; verifying the status file on disk and the graceful-stop behavior on a real signal requires a terminal session.

---

### Gaps Summary

No gaps. All 14 observable truths are VERIFIED. All 3 requirements (WATCH-01, WATCH-02, WATCH-03) are SATISFIED. All REVIEW.md findings are confirmed fixed in the current codebase. One human verification item remains: a live end-to-end daemon run against real Jira to confirm the production credential gate, real HTTP polling, signal handling, and status file writing operate as documented.

---

_Verified: 2026-06-12T00:00:00Z_
_Verifier: Claude (gsd-verifier)_


## Human Verification Result (2026-06-12)

Live daemon run executed and PASSED — see 20-HUMAN-UAT.md (status: complete).
2 cycles against real jira.mts.ru, graceful SIGTERM stop, accurate status file, pidfile cleanup.
