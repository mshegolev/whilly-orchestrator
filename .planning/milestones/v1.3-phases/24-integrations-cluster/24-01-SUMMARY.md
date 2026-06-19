---
phase: 24-integrations-cluster
plan: "01"
subsystem: integrations
tags: [jira, openspec, capability-spec, watch-daemon, auth, reverse-spec]

requires:
  - phase: 20-jira-watcher-daemon
    provides: shipped watch-loop daemon (lifecycle, pause/readiness gates, fail-closed PID guard)
  - phase: 21-spec-baseline
    provides: openspec/AUTHORING.md conventions + task-model-fsm exemplar
provides:
  - openspec/specs/jira-integration/spec.md (INT-01) — Jira read/work-snapshot + auth + read/mutating boundary
  - openspec/specs/jira-watcher-daemon/spec.md (INT-04) — watch-loop lifecycle + pause/readiness gates + fail-closed
affects: [24-integrations-cluster, gitlab-integration, github-integration, verifier]

tech-stack:
  added: []
  patterns:
    - "Reverse-spec capability specs from observed v4 code (no invented behavior)"
    - "Explicit read-only vs single-mutating-path boundary requirement per integration"

key-files:
  created:
    - openspec/specs/jira-integration/spec.md
    - openspec/specs/jira-watcher-daemon/spec.md
  modified:
    - .planning/REQUIREMENTS.md
    - .planning/STATE.md

key-decisions:
  - "set_issue_status named as the SOLE mutating Jira path; soft-fail-never-raise stated normatively"
  - "Readiness None and EPERM/unprobeable PID both specced as fail-closed per Phase 20 shipped behavior"
  - "CLI credential gate placed BEFORE fetch/loop as an explicit requirement in both specs"

patterns-established:
  - "Pattern 1: each integration spec carries a dedicated read-only-vs-mutating boundary requirement"
  - "Pattern 2: auth expectations (layered toml/env/company-settings, basic vs bearer) specced explicitly"

requirements-completed: [INT-01, INT-04]

duration: 18min
completed: 2026-06-16
---

# Phase 24 Plan 01: Jira Integration & Watcher-Daemon Specs Summary

**Two reverse-spec'd OpenSpec capability specs — jira-integration (INT-01) and jira-watcher-daemon (INT-04) — locking Jira auth resolution, the read-only-vs-single-mutating-path boundary, and the Phase 20 watch-loop lifecycle with fail-closed pause/readiness/PID gates, both passing `openspec validate --strict` clean.**

## Performance

- **Duration:** ~18 min
- **Started:** 2026-06-16
- **Completed:** 2026-06-16
- **Tasks:** 2
- **Files modified:** 4 (2 specs created, REQUIREMENTS.md + STATE.md updated)

## Accomplishments
- INT-01 `jira-integration` spec: layered credential resolution (toml → env → company-settings YAML), basic vs bearer schemes, TLS/CA honoring, RuntimeError-naming-missing-fields when unconfigured, read-only fetch/snapshot/classify path, and `set_issue_status` as the sole mutating transition with case-insensitive `to.name` match and soft-fail-never-raise.
- INT-04 `jira-watcher-daemon` spec: interval resolution (`--interval` > env > 300s), `threading.Event.wait` interruptible sleep, SIGTERM/SIGINT → EXIT_OK, atomic secret-free status file, 5/10/20/40/60 backoff reset-on-success, PID single-instance guard with EPERM/unprobeable = fail-closed, pause gate suppressing dispatch only, readiness gate with `None` = not-ready fail-closed, and default-off `--dispatch`.
- Both specs explicitly state the read-only-vs-mutating boundary and the CLI credential gate running before any fetch/loop.

## Task Commits

Each task was committed atomically:

1. **Task 1: Spec jira read/source/board-sync (INT-01)** - `87e4f18` (docs)
2. **Task 2: Spec jira watch-loop daemon (INT-04)** - `ad06dd0` (docs)

**Plan metadata:** committed with this SUMMARY (docs: complete plan)

## Files Created/Modified
- `openspec/specs/jira-integration/spec.md` - INT-01 normative capability spec reverse-spec'd from `whilly/sources/jira.py`, `whilly/jira_board.py`, `whilly/jira_work.py`, `whilly/cli/jira.py`
- `openspec/specs/jira-watcher-daemon/spec.md` - INT-04 normative capability spec reverse-spec'd from `whilly/cli/jira_watch_loop.py`, `whilly/jira_watch.py`, `whilly/pause_control.py`
- `.planning/REQUIREMENTS.md` - INT-01 + INT-04 checked off; traceability row updated
- `.planning/STATE.md` - Current Position advanced to Phase 24 / Plan 24-01

## Decisions Made
- Named `JiraBoardClient.set_issue_status` as the single mutating path and specced its case-insensitive `to.name` match plus soft-fail (return False, never raise) directly from the observed code, rather than a generic "transitions are mutating" statement.
- Treated both undeterminable readiness (`None`) and EPERM/unprobeable PID as fail-closed, matching the Phase 20 shipped guards (`_acquire_pid_lock`, `_run_dispatch_if_ready`).
- Specced the credential gate as living in the CLI layer (`_ensure_jira_config` / watch branch in `whilly/cli/jira.py`) so the loop itself is read-only — consistent with both modules.

## Deviations from Plan

None - plan executed exactly as written. Both specs passed `openspec validate <slug> --strict --json` (0 errors, 0 warnings) on first attempt.

## Issues Encountered
None.

## Known Stubs
None — both files are complete normative specs grounded in real v4 code.

## User Setup Required
None - documentation only; zero `whilly/` changes.

## Next Phase Readiness
- INT-01 and INT-04 specced and validated. Remaining Phase 24 work: INT-02 (gitlab-integration), INT-03 (github-integration, subsystem altitude), INT-05 (notifications), INT-06 (mcp-integration).
- No blockers. The read-only-vs-mutating + auth-expectations pattern established here applies to the remaining four integration specs.

## Self-Check: PASSED

---
*Phase: 24-integrations-cluster*
*Completed: 2026-06-16*
