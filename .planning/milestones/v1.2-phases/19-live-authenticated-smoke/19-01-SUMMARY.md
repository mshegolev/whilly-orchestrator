---
phase: 19-live-authenticated-smoke
plan: "01"
subsystem: testing
tags: [smoke, report, redaction, cli, json, pytest]

requires:
  - phase: 17-jira-work-routing
    provides: "whilly/llm_ops.py DEFAULT_LOG_DIR, LOG_DIR_ENV, _log_dir() used by smoke.py"

provides:
  - "SmokeReport dataclass accumulator with add_check/all_passed/to_payload"
  - "write_smoke_report() — secret-free, timestamped JSON report under whilly_logs/smoke/"
  - "_redact_url() — strips user:pass@ from URL authority, idempotent on clean URLs"
  - "EXIT_OK=0, EXIT_CHECK_FAILED=1, EXIT_CONFIG_MISSING=2 shared exit constants"
  - "13 unit tests covering write, redaction, accumulation, no-secret-leak"

affects:
  - 19-02-jira-smoke-command
  - 19-03-gitlab-smoke-command

tech-stack:
  added: []
  patterns:
    - "SmokeReport accumulator: never stops on first failure (per-check try/except model)"
    - "Timestamp format: datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')"
    - "write_smoke_report mirrors llm_ops._write_json: mkdir(parents=True, exist_ok=True) then write_text"
    - "_redact_url: urllib.parse.urlsplit + SplitResult rebuild; returns input on parse failure"
    - "Report dir: _log_dir() / 'smoke' so WHILLY_LOG_DIR is honored"

key-files:
  created:
    - whilly/cli/smoke.py
    - tests/unit/cli/test_smoke.py
  modified: []

key-decisions:
  - "Import DEFAULT_LOG_DIR, LOG_DIR_ENV, _log_dir from whilly.llm_ops rather than re-hardcoding literals"
  - "SmokeReport.add_check never raises on False — per-check accumulation is a hard contract"
  - "Report payload schema: kind, timestamp, checks[], summary{total,passed,failed,all_passed} — no tokens/DSNs"

patterns-established:
  - "SmokeReport: add_check for direct calls, add_timed_check for measured durations"
  - "write_smoke_report(report_dir, kind, payload) -> Path — takes explicit dir for testability with tmp_path"

requirements-completed: [LIVE-03]

duration: 16min
completed: "2026-06-12"
---

# Phase 19 Plan 01: Smoke Report Foundation Summary

**SmokeReport dataclass + write_smoke_report + _redact_url foundation for whilly jira/gitlab smoke, with URL credential scrubbing and 13 unit tests asserting no-secret-leak**

## Performance

- **Duration:** 16 min
- **Started:** 2026-06-11T20:32:19Z
- **Completed:** 2026-06-11T20:48:00Z
- **Tasks:** 2
- **Files modified:** 2

## Accomplishments

- Created `whilly/cli/smoke.py` with SmokeReport accumulator (never stops on first failure), write_smoke_report() writing secret-free JSON with parents=True mkdir, and _redact_url() stripping user:pass@ via urllib.parse.urlsplit
- Established EXIT_OK/EXIT_CHECK_FAILED/EXIT_CONFIG_MISSING (0/1/2) as shared constants consumed by Wave 2 commands
- Created `tests/unit/cli/test_smoke.py` with 13 tests: file naming, JSON round-trip, nested dir creation, accumulation order, timestamp Z-suffix, summary counts, redaction with port, no-secret-leak assertion

## Task Commits

1. **Task 1: SmokeReport accumulator + write_smoke_report + redaction** - `0e871d6` (feat)
2. **Task 2: Unit tests for smoke report** - `448eda1` (feat)

**Plan metadata:** (docs commit will follow)

## Files Created/Modified

- `whilly/cli/smoke.py` — SmokeReport dataclass, write_smoke_report, _redact_url, exit constants, _smoke_report_dir
- `tests/unit/cli/test_smoke.py` — 13 unit tests (write, accumulation, redaction, no-secret-leak)

## Decisions Made

- Import `_log_dir` from `whilly.llm_ops` rather than reimplementing, so WHILLY_LOG_DIR is honored through the existing env-resolution chain
- `add_timed_check()` added alongside `add_check()` to give Wave 2 commands the option of recording measured durations without breaking the simple API
- `write_smoke_report` takes an explicit `report_dir: Path` parameter (not the internal default) so unit tests can use `tmp_path`; callers that want the production default call `_smoke_report_dir()` themselves

## Deviations from Plan

None - plan executed exactly as written.

## Issues Encountered

- Unused `import time` and unused `import pytest` caught by ruff pre-commit check; removed before commit.
- ruff format required one reformat pass on each file; applied before commit.

## User Setup Required

None - no external service configuration required.

## Next Phase Readiness

- `whilly/cli/smoke.py` interface is stable: Wave 2 plans (19-02, 19-03) can import SmokeReport, write_smoke_report, _redact_url, and the exit constants immediately
- `_smoke_report_dir()` and `write_smoke_report()` handle directory creation; no caller setup needed
- 13 unit tests establish the contract; any regression in the foundation will be caught before Wave 2 proceeds

## Threat Flags

No new network endpoints, auth paths, file access patterns, or schema changes introduced. `write_smoke_report` writes only to `whilly_logs/smoke/` (user's local filesystem, no external surface). Redaction coverage verified by test `test_smoke_report_payload_contains_no_tokens_or_dsn`.

## Self-Check: PASSED

- `whilly/cli/smoke.py` exists: FOUND
- `tests/unit/cli/test_smoke.py` exists: FOUND
- Commit `0e871d6` exists: FOUND
- Commit `448eda1` exists: FOUND

---
*Phase: 19-live-authenticated-smoke*
*Completed: 2026-06-12*
