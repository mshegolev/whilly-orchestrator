---
phase: 20-jira-watcher-daemon
plan: "02"
subsystem: cli/jira-watch
tags: [jira, daemon, watch-loop, pause-gate, readiness-gate, dispatch-seam, tdd]
dependency_graph:
  requires:
    - whilly.cli.jira_watch_loop._run_jira_watch  # Plan 01 core
    - whilly.pause_control.PauseControl
    - whilly.cli.jira_watch_loop._persist_watch_event
    - whilly.cli.jira_watch_loop._write_status
  provides:
    - whilly.cli.jira_watch_loop.EVENT_PAUSED
    - whilly.cli.jira_watch_loop.EVENT_BLOCK
    - whilly.cli.jira_watch_loop.EVENT_DISPATCH
    - whilly.cli.jira_watch_loop._read_watch_readiness
    - whilly.cli.jira_watch_loop._run_dispatch_if_ready
    - pause_control injectable on _run_jira_watch
    - dispatch_runner injectable on _run_jira_watch
  affects:
    - whilly/cli/jira_watch_loop.py (extended with gates)
tech_stack:
  added:
    - PauseControl gate in jira_watch_loop.py
    - _read_watch_readiness (local re-implementation of jira.py:1138-1144, no circular import)
    - _run_dispatch_if_ready extracted helper
    - 7 new unit tests in test_jira_watch_loop.py (18 total)
  patterns:
    - injectable pause_control seam (default PauseControl() in cwd)
    - injectable dispatch_runner seam (None = no dispatch wired yet)
    - default-off dispatch (structurally unreachable unless --dispatch set)
    - best-effort asyncio.run persist for watch.paused and watch.block events
    - secret-free audit payloads (reason/verdict/missing_context/issue_key only)
key_files:
  created: []
  modified:
    - whilly/cli/jira_watch_loop.py
    - tests/unit/cli/test_jira_watch_loop.py
decisions:
  - _read_watch_readiness re-implemented locally in jira_watch_loop.py to avoid
    circular import from whilly.cli.jira (mirrors jira.py:1138-1144 exactly).
  - _run_dispatch_if_ready extracted to a named helper so the dispatch call site
    is isolated and grep-auditable (plan <done> grep gate: dispatch_runner( is 1
    occurrence, all under the `if wants_dispatch:` conditional).
  - dispatch_runner=None means "no production dispatch wired yet" — plan 03 wires
    the Phase-17-gated path; tests inject a spy callable.
  - Pause gate fires AFTER collect (read-only polling already happened) and BEFORE
    dispatch — consistent with CONTEXT.md locked decision "keep read-only polling,
    dispatch nothing".
  - watch.block payload carries verdict + missing_context + issue_key only (T-20-07).
  - Task 2 RED tests passed immediately because Task 1 GREEN implementation was
    holistic (both gates implemented in same commit); per TDD discipline, RED commit
    still created for completeness.
metrics:
  duration: "28 min"
  completed_date: "2026-06-12"
  tasks: 2
  files: 2
---

# Phase 20 Plan 02: Pause Gate and Readiness Gate Summary

Layered pause and readiness gating onto the watch loop from plan 01, plus a
default-off dispatch seam routed through an injectable runner with full TDD
coverage.

## One-liner

PauseControl gate suppresses dispatch while polling continues; readiness gate
blocks dispatch with watch.block event when verdict != ready_for_testing;
dispatch is structurally off by default, enabled only via --dispatch through
the injected runner seam.

## What Was Built

### `whilly/cli/jira_watch_loop.py` (extended)

New symbols added to the existing module:

- **`EVENT_PAUSED = "watch.paused"`, `EVENT_BLOCK = "watch.block"`, `EVENT_DISPATCH = "watch.dispatch"`**
  Audit event type constants.

- **`_read_watch_readiness(plan_path) -> dict | None`**
  Local re-implementation of `_read_jira_work_readiness` from `jira.py:1138-1144`.
  Reads `data["jira_work"]["readiness"]` from the plan JSON.  No import from
  `whilly.cli.jira` (Pitfall 5 / T-20-08).

- **`pause_control: PauseControl | None` parameter on `_run_jira_watch`**
  Injectable; defaults to `PauseControl()` (reads `.whilly_pause` in cwd).  In
  tests, a `PauseControl(pause_file=str(tmp_pause))` pointing at a tmp file is
  injected.

- **`dispatch_runner: Callable[..., int] | None` parameter on `_run_jira_watch`**
  Injectable dispatch hook.  `None` = no dispatch wired (plan 03 wires the
  Phase-17-gated path).  Tests inject a spy callable.

- **Pause gate** (in `_run_jira_watch` per-cycle body):
  After collector call, before dispatch: `if effective_pause_ctrl.is_paused()`:
  set `last_poll_result="paused"`, log, emit best-effort `watch.paused` event
  with `{reason, issue_key}` payload (secret-free, T-20-07), `continue` — never
  dispatches.

- **`_run_dispatch_if_ready(...)` helper**:
  Only called when `wants_dispatch=True` (i.e., `--dispatch` is set).  Reads
  readiness from plan JSON; if verdict != `ready_for_testing` and not
  `allow_unready`: sets `last_poll_result="blocked"`, emits best-effort
  `watch.block` event with `{verdict, missing_context, issue_key}`, returns.
  Otherwise calls `dispatch_runner(args)` and emits `watch.dispatch` event.

### `tests/unit/cli/test_jira_watch_loop.py` (7 new tests, 18 total)

| Test | Behavior Verified |
|------|-------------------|
| `test_pause_gate_collector_called_dispatch_not` | Collector runs; dispatch_runner not called when paused; status="paused" |
| `test_pause_gate_emits_watch_paused_event` | watch.paused event emitted with reason; no token/DSN in payload |
| `test_no_pause_gate_not_taken_when_unpaused` | Without pause file, last_poll_result != "paused" |
| `test_dispatch_default_off_runner_never_called` | Without --dispatch, dispatch_runner never called even when ready |
| `test_readiness_gate_blocks_dispatch_with_block_event` | Unready verdict blocks dispatch; watch.block event with verdict + missing_context |
| `test_readiness_gate_passes_calls_dispatch_runner` | Ready verdict routes through dispatch_runner once |
| `test_allow_unready_run_overrides_readiness_gate` | --allow-unready-run overrides gate; dispatch_runner called |

## TDD Gate Compliance

| Gate | Commit | Status |
|------|--------|--------|
| RED (Task 1) | `8c06c2a` test(20-02): add failing tests for pause gate (Task 1 RED) | Passed — 3 tests failed with TypeError (unexpected keyword argument) |
| GREEN (Task 1) | `38f14f2` feat(20-02): implement pause gate in jira_watch_loop (Task 1 GREEN) | All pause tests + prior 11 green |
| RED (Task 2) | `d5e251e` test(20-02): add readiness-gate and dispatch default-off tests (Task 2 RED) | Passed immediately — Task 1 GREEN implementation was holistic |
| GREEN (Task 2) | `38f14f2` (same) | All 18 tests green |

Note: Task 2 RED tests passed immediately because Task 1 GREEN implemented both
gates in the same module. Per TDD discipline a separate RED commit was still
created. This matches the Plan 01 precedent documented in 20-01-SUMMARY.md.

## Verification

```
.venv/bin/python -m pytest tests/unit/cli/test_jira_watch_loop.py -q   → 18 passed
.venv/bin/python -m pytest tests/unit/cli -q                            → 67 passed (no regressions)
ruff check jira_watch_loop.py test_jira_watch_loop.py                   → clean
grep -n "^from whilly.cli.jira" jira_watch_loop.py                      → 0 real imports (1 comment only)
dispatch_runner( call sites: 1, all inside _run_dispatch_if_ready (called only under wants_dispatch=True)
```

## Deviations from Plan

### Test design: holistic GREEN (not a deviation)

Task 1 GREEN commit implemented both the pause gate AND the dispatch/readiness
seam in `_run_jira_watch` (they coexist in the same loop body).  Task 2 RED tests
passed immediately as a consequence.  This mirrors the Plan 01 pattern and is
expected behavior.

### Intermediate `_write_status` call (Rule 2 - correctness)

The plan specifies status is written after the cycle.  The implementation writes
status at two points: once after the cycle (before the pause check), and a second
time inside the pause gate before `continue`.  This ensures the `last_poll_result`
value is visible on disk when the paused branch is taken mid-cycle.

## Threat Mitigations Applied

| Threat | Mitigation |
|--------|-----------|
| T-20-06 (EoP: dispatch without gate) | dispatch is structurally unreachable unless `wants_dispatch=True` AND unpaused AND readiness satisfied (or `allow_unready`); routed through injected runner only |
| T-20-07 (Info Disclosure: pause/readiness payload) | watch.paused payload: {reason, issue_key}; watch.block payload: {verdict, missing_context, issue_key}; no token, no DSN value in any payload |
| T-20-08 (Spoofing: circular import bypass) | No `from whilly.cli.jira` import in module; `_read_watch_readiness` re-implemented locally; grep-verified |

## Known Stubs

None — no placeholders. TODO comment in module:
- `TODO(plan-03)`: credential gate (`_ensure_jira_config`) wiring + `build_jira_parser` subparser registration

## Threat Flags

None — no new network endpoints, auth paths, or trust boundaries. The pause gate
reads a local file; the readiness gate reads a local JSON file. Both already existed
in the threat model registered for plan 02.

## Self-Check: PASSED

- [x] `whilly/cli/jira_watch_loop.py` contains `EVENT_PAUSED`, `EVENT_BLOCK`, `EVENT_DISPATCH`
- [x] `whilly/cli/jira_watch_loop.py` contains `_read_watch_readiness`
- [x] `whilly/cli/jira_watch_loop.py` contains `pause_control` parameter
- [x] `whilly/cli/jira_watch_loop.py` contains `dispatch_runner` parameter
- [x] No `from whilly.cli.jira` real import (only comment)
- [x] Commit `8c06c2a` (RED Task 1) exists
- [x] Commit `38f14f2` (GREEN Task 1 + holistic) exists
- [x] Commit `d5e251e` (RED Task 2) exists
- [x] All 18 tests pass under `.venv/bin/python`
- [x] No regressions in `tests/unit/cli` (67 passed)
- [x] Ruff clean
