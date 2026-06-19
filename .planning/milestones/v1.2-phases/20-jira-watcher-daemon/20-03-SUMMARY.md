---
phase: 20-jira-watcher-daemon
plan: "03"
subsystem: cli/jira-watch
tags: [jira, daemon, watch-loop, cli-wiring, docs, tdd]
dependency_graph:
  requires:
    - whilly.cli.jira_watch_loop._run_jira_watch  # Plan 01/02 core + gates
    - whilly.cli.jira_watch_loop._run_dispatch_if_ready
    - whilly.cli.jira.build_jira_parser
    - whilly.cli.jira.run_jira_command
  provides:
    - whilly.cli.jira.build_jira_parser (watch + watch-status subparsers)
    - whilly.cli.jira.run_jira_command (watch + watch-status dispatch branches)
    - whilly.cli.jira_watch_loop._run_watch_status
  affects:
    - whilly/cli/jira.py
    - whilly/cli/jira_watch_loop.py
    - docs/Whilly-Usage.md
    - docs/Whilly-Interfaces-and-Tasks.md
    - tests/unit/test_jira_cli.py
    - tests/unit/test_docs_live_smoke.py
tech_stack:
  added:
    - watch + watch-status subparsers in build_jira_parser() (jira.py)
    - lazy import dispatch branches in run_jira_command() (jira.py)
    - _run_watch_status() function in jira_watch_loop.py
    - 6 new CLI dispatch tests (test_jira_cli.py)
    - 5 new docs regression tests (test_docs_live_smoke.py)
    - Jira watcher daemon section in docs/Whilly-Usage.md
    - Module contract section in docs/Whilly-Interfaces-and-Tasks.md
  patterns:
    - lazy import branch (mirrors tui branch; avoids circular import)
    - injectable dispatch_runner wired only when --dispatch set (T-20-10)
    - EXIT_OK for missing status file (status absence is not an error)
    - env-var references in bash blocks (bash -n clean; Phase 19 lesson)
key_files:
  created: []
  modified:
    - whilly/cli/jira.py
    - whilly/cli/jira_watch_loop.py
    - tests/unit/test_jira_cli.py
    - tests/unit/test_docs_live_smoke.py
    - docs/Whilly-Usage.md
    - docs/Whilly-Interfaces-and-Tasks.md
decisions:
  - dispatch_runner=None wired into _run_jira_watch without --dispatch (T-20-10);
    the production closure wires through _run_plan_command + readiness gate only
    when --dispatch is explicitly passed.
  - _run_watch_status lives in jira_watch_loop.py (not jira.py); jira.py imports
    it lazily — keeps the no-circular-import invariant intact.
  - watch-status missing file returns EXIT_OK with a stderr hint (not an error exit)
    — operators inspect status in any state including before first run.
metrics:
  duration: "22 min"
  completed_date: "2026-06-12"
  tasks: 2
  files: 6
---

# Phase 20 Plan 03: CLI Wiring and Docs Summary

`whilly jira watch` and `whilly jira watch-status` wired into the CLI surface,
documented in Whilly-Usage.md, and pinned by regression tests.

## One-liner

CLI dispatch for watch/watch-status via lazy import (no circular import), default-off
production dispatch_runner wired through Phase-17 readiness gate, and full docs
coverage with bash -n-clean code blocks.

## What Was Built

### Task 1: watch + watch-status subparsers and dispatch wiring

**`whilly/cli/jira.py`**

- `build_jira_parser()`: added `watch` subparser with all required flags —
  `--issue` (repeatable, required), `--interval`, `--timeout`, `--dispatch`
  (default False), `--readiness-repo-path`, `--allow-unready-run`, and the
  `--interactive-config` / `--no-interactive-config` pair copied verbatim from
  the smoke subparser (Phase 19 Pitfall 1 — `_ensure_jira_config` accesses
  `args.interactive_config` directly).
- `build_jira_parser()`: added `watch-status` subparser with `--json`.
- `run_jira_command()`: dispatch branch for `action == "watch"` — lazy import of
  `_run_jira_watch` from `whilly.cli.jira_watch_loop` (mirrors `tui` branch at
  jira.py:434-436). Production `dispatch_runner` closure built only when
  `args.dispatch` is True; routes through `_run_plan_command` + readiness gate.
  Without `--dispatch`, `dispatch_runner=None` is passed (T-20-10).
- `run_jira_command()`: dispatch branch for `action == "watch-status"` — lazy
  import of `_run_watch_status`.

**`whilly/cli/jira_watch_loop.py`**

- Added `_run_watch_status(args, *, environ)` — reads `_status_path()`, returns
  `EXIT_OK` and prints stderr hint when file is missing; prints human-readable key
  fields (state, pid, cycle_count, error_count, last_poll_at, backoff_seconds) or
  raw JSON when `args.json` (T-20-11: no secrets in output).

**`tests/unit/test_jira_cli.py`** (6 new tests)

| Test | Behavior Verified |
|------|-------------------|
| `test_watch_subparser_has_required_flags` | All flags including --interactive-config, --no-interactive-config |
| `test_watch_dispatch_invokes_run_jira_watch` | Lazy import dispatch: spy receives call, rc=0 |
| `test_watch_dispatch_default_off` | dispatch_runner=None without --dispatch |
| `test_watch_status_missing_file_returns_ok` | rc=0, stderr hint, no crash |
| `test_watch_status_prints_human_readable` | state/pid/cycle_count in stdout |
| `test_watch_status_json_flag_prints_valid_json` | Valid JSON output |

### Task 2: Watcher docs section + regression tests + module contract

**`docs/Whilly-Usage.md`** — new `## Jira watcher daemon` section covering:

- Quick start tmux command (env-var refs, bash -n clean)
- Interval via `--interval` and `WHILLY_JIRA_WATCH_INTERVAL` (default 300 s)
- Graceful stop (SIGINT/SIGTERM), single-instance guard, exponential backoff
- Status inspection: `whilly jira watch-status` / `--json`, file path
  `whilly_logs/watch/jira-watch-status.json`
- Global pause gate (read-only polling continues, dispatch suppressed)
- `--dispatch` OFF by default; Phase-17 readiness gate with `--allow-unready-run`
- DB audit events (best-effort when `WHILLY_DATABASE_URL` set)
- Exit code table

**`docs/Whilly-Interfaces-and-Tasks.md`** — new `## 7. Module contract: whilly/cli/jira_watch_loop.py` section:

- Full exported symbols table (all public functions, async helpers, constants)
- Status-file JSON schema (field names, types, secret-free guarantee)
- Key constraints (no circular import, dispatch call site count = 1, EXIT_OK for
  missing status)

**`tests/unit/test_docs_live_smoke.py`** (5 new regression tests)

| Test | Behavior Verified |
|------|-------------------|
| `test_watcher_section_heading_present` | `## Jira watcher daemon` in docs |
| `test_watcher_commands_documented` | `whilly jira watch` + `whilly jira watch-status` in section |
| `test_watcher_log_dir_documented` | `whilly_logs/watch/` path in section |
| `test_watcher_status_file_name_documented` | `jira-watch-status.json` in section |
| `test_watcher_dispatch_default_off_documented` | `--dispatch` + off-by-default text in section |

## Verification

```
.venv/bin/python -m pytest tests/unit/test_jira_cli.py -k watch -q
  → 6 passed

.venv/bin/python -m pytest tests/unit/test_docs_live_smoke.py test_docs_bash_blocks_parse.py -q
  → 136 passed

.venv/bin/python -m pytest tests/unit -q
  → 2475 passed, 2 skipped

ruff check whilly/cli/jira.py whilly/cli/jira_watch_loop.py tests/unit/test_jira_cli.py
  → clean

grep -c "from whilly.cli.jira" whilly/cli/jira_watch_loop.py
  → 1 (comment only, no real import)

whilly jira watch --help | grep --interval,--issue,--dispatch,--interactive-config
  → all four flags listed
```

## Deviations from Plan

### Auto-fixed Issues

None — plan executed as written.

### Design note: production dispatch_runner closure (not a deviation)

The plan says "Claude's Discretion on the exact seam, consistent with plan 01/02
injectables." The production dispatch_runner is a closure that captures the injected
collaborators from `run_jira_command`'s parameters (plan_runner, config_loader, etc.)
and routes through `_run_plan_command` + `_read_jira_work_readiness` + `_run_plan_worker`.
The readiness gate is checked both in the runner closure (for early exit) and in
`_run_dispatch_if_ready` (for the block audit event). This double-gate is consistent
with the test for `test_watch_dispatch_default_off` — the inner gate inside the loop
never fires unless `--dispatch` is passed and the runner is non-None.

## Threat Mitigations Applied

| Threat | Mitigation |
|--------|-----------|
| T-20-10 (EoP: --dispatch) | `dispatch_runner=None` without --dispatch; production runner wires Phase-17 gate; default False on subparser |
| T-20-11 (Info Disclosure: watch-status) | `_run_watch_status` prints only status JSON fields (pid/counts/timestamps); no token, no DSN value |
| T-20-12 (Tampering: missing --interactive-config) | watch subparser declares `--interactive-config` / `--no-interactive-config` pair verbatim from smoke (verified by test_watch_subparser_has_required_flags) |

## Known Stubs

None — no hardcoded placeholders. The TODO comment in `_run_jira_watch` for
`TODO(plan-03)` credential gate wiring is now resolved: plan 03 wires the watch
subparser with `--interactive-config` and passes credential collaborators into the
production dispatch_runner. The main loop's credential check (`_ensure_jira_config`)
is invoked inside the dispatch_runner closure when `--dispatch` is set — this is the
correct seam because non-dispatch runs only poll (no credentials needed beyond what
the snapshot_collector already uses).

## Threat Flags

None — no new network endpoints, auth paths, or trust boundaries beyond those already
registered in the plan threat model.

## Self-Check: PASSED

- [x] `whilly/cli/jira.py` contains `watch` subparser with `--interactive-config`
- [x] `whilly/cli/jira.py` contains `watch-status` subparser
- [x] `whilly/cli/jira_watch_loop.py` contains `_run_watch_status`
- [x] No `from whilly.cli.jira` real import in jira_watch_loop.py (grep = 1 comment)
- [x] `docs/Whilly-Usage.md` contains `## Jira watcher daemon` section
- [x] `docs/Whilly-Interfaces-and-Tasks.md` contains `## 7. Module contract: whilly/cli/jira_watch_loop.py`
- [x] Commit `270ff18` (RED Task 1) exists
- [x] Commit `616cdd7` (GREEN Task 1) exists
- [x] Commit `2ac21f4` (Task 2 docs + regression tests) exists
- [x] All 6 watch CLI tests pass
- [x] All 5 watcher docs regression tests pass
- [x] All bash blocks pass `bash -n` (136 tests pass)
- [x] Full unit suite: 2475 passed (no regressions)
- [x] Ruff clean
