---
phase: 19-live-authenticated-smoke
plan: "02"
subsystem: cli
tags: [smoke, jira, cli, security, redaction, report, unit-tests]

requires:
  - phase: 19-live-authenticated-smoke
    plan: "01"
    provides: "SmokeReport, write_smoke_report, _redact_url, EXIT_CONFIG_MISSING, EXIT_CHECK_FAILED from whilly/cli/smoke.py"

provides:
  - "_run_jira_smoke() — six read-only Jira checks with per-check accumulation, credential gate, and redacted report"
  - "'smoke' subparser in whilly jira with --issue, --timeout, --persist, --json flags"
  - "Dispatch branch in run_jira_command() for action=='smoke'"
  - "5 unit tests covering pass/fail/missing-config/classify-readonly/no-secret-leak"

affects:
  - "LIVE-01 — authenticated Jira smoke exercising auth/issue/comments/changelog/links/classify"
  - "LIVE-03 — persisted redacted JSON report under whilly_logs/smoke/"

tech-stack:
  added: []
  patterns:
    - "Credential gate (parse_jira_key + _ensure_jira_config) runs before snapshot_collector — T-19-03 mitigation"
    - "Per-check SmokeReport accumulation: never stops on first failure (Pitfall 2)"
    - "EXIT_CONFIG_MISSING (2) mapped from _ensure_jira_config non-zero return"
    - "Redacted report: only target_host (hostname), project_key, booleans, counts — no token/DSN"
    - "Optional --persist gate identical to _run_poll pattern"

key-files:
  created:
    - tests/unit/cli/test_jira_smoke.py
  modified:
    - whilly/cli/jira.py

key-decisions:
  - "Import EXIT_CHECK_FAILED as _SMOKE_EXIT_CHECK_FAILED to avoid shadowing EXIT_VALIDATION_ERROR in jira.py"
  - "Add --interactive-config / --no-interactive-config to smoke subparser so _ensure_jira_config can check args.interactive_config without AttributeError"
  - "classify check reads snapshot.classification directly (pure field access, no extra Jira call) — read-only guarantee"
  - "_persist_smoke_event is a best-effort async helper; absence of DB does not block smoke report writing"

requirements-completed: [LIVE-01, LIVE-03]

duration: 20min
completed: "2026-06-12"
---

# Phase 19 Plan 02: Jira Smoke Command Summary

**`whilly jira smoke --issue KEY` with six read-only checks (auth/issue/comments/changelog/links/classify), credential gate before any network call, per-check SmokeReport accumulation, and redacted JSON report — exit 0/1/2**

## Performance

- **Duration:** ~20 min
- **Completed:** 2026-06-12
- **Tasks:** 2
- **Files modified:** 2 (1 created, 1 modified)

## Accomplishments

- Added `smoke` subparser to `build_jira_parser()` with `--issue` (required), `--timeout`, `--persist`, `--json`, `--interactive-config`, `--no-interactive-config` arguments
- Implemented `_run_jira_smoke()` in `whilly/cli/jira.py`:
  - V5 input validation via `parse_jira_key()` before any config or network call (T-19-03 mitigation)
  - Credential gate via `_ensure_jira_config()` returning `EXIT_CONFIG_MISSING (2)` — never leaks RuntimeError traceback (T-19-05 mitigation)
  - Six read-only checks: `auth`, `issue_fetch`, `comments`, `changelog`, `remote_links`, `classify`
  - `SmokeReport` accumulation — failing checks never abort remaining checks
  - `write_smoke_report()` called on every completed run (pass or fail)
  - Redacted payload: `target_host` (hostname only), `project_key`, `issue_key`, booleans, counts — token never serialized (T-19-04 mitigation)
  - Optional `--persist` gate identical to `_run_poll` pattern
  - Human-readable per-check summary or `--json` full payload
- Created `tests/unit/cli/test_jira_smoke.py` with 5 unit tests (all pass):
  1. `test_jira_smoke_all_checks_pass_returns_zero_and_writes_report` — exit 0, report file written
  2. `test_jira_smoke_missing_config_returns_2_and_never_calls_collector` — exit 2, collector not called
  3. `test_jira_smoke_raising_collector_returns_1_with_actionable_hint_no_traceback` — exit 1, no Traceback
  4. `test_jira_smoke_classify_uses_snapshot_classification_field` — classify reads field directly
  5. `test_jira_smoke_report_contains_no_token_or_dsn` — no token/DSN in report

## Task Commits

1. **Task 1: smoke subparser + _run_jira_smoke** — `5e6d662` (feat)
2. **Task 2: unit tests + interactive-config fix** — `7cdfb8c` (feat)

## Files Created/Modified

- `whilly/cli/jira.py` — smoke subparser, dispatch branch, `_run_jira_smoke`, `_persist_smoke_event`
- `tests/unit/cli/test_jira_smoke.py` — 5 unit tests (new file)

## Decisions Made

- Import `EXIT_CHECK_FAILED as _SMOKE_EXIT_CHECK_FAILED` to keep jira.py's own `EXIT_VALIDATION_ERROR` constant intact while still returning the correct code on persist failure
- Added `--interactive-config` / `--no-interactive-config` to the smoke subparser — `_ensure_jira_config` accesses `args.interactive_config` via `Namespace` attribute; smoke subparser must declare these flags or the call raises `AttributeError`
- `classify` check reads `snapshot.classification` as a pure field access (no extra Jira call); this satisfies the read-only guarantee documented in `whilly/jira_work.py`

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] Missing --interactive-config on smoke subparser**
- **Found during:** Task 2 (test run)
- **Issue:** `_ensure_jira_config()` accesses `args.interactive_config` and `args.no_interactive_config` via Namespace; the smoke subparser did not declare these flags, causing `AttributeError` on every smoke call with missing config
- **Fix:** Added `--interactive-config` and `--no-interactive-config` to `p_smoke` in `build_jira_parser()`
- **Files modified:** `whilly/cli/jira.py`
- **Commit:** `7cdfb8c`

## Threat Surface Scan

| Flag | File | Description |
|------|------|-------------|
| T-19-03 mitigated | whilly/cli/jira.py | `parse_jira_key()` validates `[A-Z][A-Z0-9]+-\d+` before URL/path construction |
| T-19-04 mitigated | whilly/cli/jira.py | Report payload contains only hostname, project_key, booleans, counts; token never serialized |
| T-19-05 mitigated | whilly/cli/jira.py | Snapshot collector exceptions caught and converted to per-check hints; no raw traceback surfaced |

## Known Stubs

None — all six checks are wired to real snapshot fields. The `_persist_smoke_event` helper is a best-effort async no-op when `--persist` is not passed, which is the intended behavior.

## Self-Check: PASSED

- `whilly/cli/jira.py` defines `_run_jira_smoke`: FOUND
- `tests/unit/cli/test_jira_smoke.py` exists: FOUND
- `.planning/phases/19-live-authenticated-smoke/19-02-SUMMARY.md` exists: FOUND
- Commit `5e6d662` exists: FOUND
- Commit `7cdfb8c` exists: FOUND
- Commit `62dc858` (metadata) exists: FOUND
- `pytest tests/unit/cli/test_jira_smoke.py`: 5 passed
- `pytest tests/unit -q`: 2413 passed, 2 skipped
- `ruff check whilly/cli/jira.py tests/unit/cli/test_jira_smoke.py`: clean

---
*Phase: 19-live-authenticated-smoke*
*Completed: 2026-06-12*
