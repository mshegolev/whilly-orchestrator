---
phase: 11-ci-polling-and-bounded-repair
plan: 06
subsystem: compliance-reporting
tags: [ci, repair, compliance, scoped-wording, audit-evidence]

requires:
  - phase: 11-ci-polling-and-bounded-repair
    provides: Local and remote CI poll evidence, bounded repair requests, escalation, terminal repair completion, and transport diagnostics from Plans 11-04 and 11-05.
provides:
  - Scoped compliance capability named exactly `Bounded CI polling and repair`.
  - PASS detection requiring CI/repair primitives, local and remote runtime wiring, transport prefixes, and focused runtime tests.
  - Report wording that includes `explicit configured CI polling`, `bounded repair attempts`, `repair.escalated`, and the exact negative scope sentence.
affects: [phase-11-compliance, ci-runtime, repair-runtime, documentation-boundaries]

tech-stack:
  added: []
  patterns:
    - Compliance capabilities use deterministic file-content evidence before reporting PASS.
    - Overclaim terms are restricted to a single negative scope sentence in the bounded CI/repair row.

key-files:
  created:
    - .planning/phases/11-ci-polling-and-bounded-repair/11-06-SUMMARY.md
  modified:
    - whilly/compliance/__init__.py
    - tests/unit/test_compliance_report.py

key-decisions:
  - "Compliance reports `Bounded CI polling and repair` as PASS only when CI primitives, repair primitives, local and remote runtime wiring, transport diagnostic prefixes, and focused local/remote runtime tests are present."
  - "PASS evidence uses scoped wording: `explicit configured CI polling`, `bounded repair attempts`, and `repair.escalated`."
  - "The only allowed overclaiming terms in the capability row are inside the exact negative sentence: `No continuous polling, auto-merge, production recovery, or unbounded repair is claimed.`"

patterns-established:
  - "Compliance file scans can require both runtime code signals and focused tests before moving a capability to PASS."
  - "Compliance wording tests remove the allowed negative-scope sentence before checking for forbidden implemented-claim terms."

requirements-completed: [CI-01, CI-02]

duration: 6 min 23 sec
completed: 2026-05-08T19:05:10Z
---

# Phase 11 Plan 06: Scoped Compliance Wording And Report Tests Summary

**Scoped compliance evidence for explicit configured CI polling and bounded repair without autonomous or continuous repair claims**

## Performance

- **Duration:** 6 min 23 sec
- **Started:** 2026-05-08T18:58:47Z
- **Completed:** 2026-05-08T19:05:10Z
- **Tasks:** 1 TDD task
- **Files modified:** 2 plus this summary

## Accomplishments

- Added compliance capability `Bounded CI polling and repair`.
- Required concrete evidence from `whilly/ci/verification.py`, `whilly/repair/policy.py`, local worker wiring, remote worker wiring, transport `ci.` and `repair.` diagnostics, and focused local/remote runtime tests before PASS.
- Pinned report wording to `explicit configured CI polling`, `bounded repair attempts`, `repair.escalated`, and `No continuous polling, auto-merge, production recovery, or unbounded repair is claimed.`
- Added compliance report regression tests for scoped PASS wording, missing runtime evidence, and forbidden overclaim terms.

## Task Commits

The TDD task was committed atomically with RED, GREEN, and REFACTOR commits:

1. **Task 1 RED: scoped bounded CI compliance tests** - `ef2b015` (test)
2. **Task 1 GREEN: bounded CI compliance capability** - `f0c9d9e` (feat)
3. **Task 1 REFACTOR: Ruff formatting** - `6598bf7` (refactor)

**Plan metadata:** captured by the final GSD metadata commit.

## Files Created/Modified

- `whilly/compliance/__init__.py` - Adds scoped bounded CI/repair capability detection, PASS evidence, gap wording, and missing-signal reporting.
- `tests/unit/test_compliance_report.py` - Adds regression tests for scoped wording, runtime evidence requirements, and non-overclaiming language.

## Decisions Made

- Compliance detection accepts existing runtime builder-call wiring such as `make_ci_poll_result_event`, `make_repair_escalated_event`, and `make_repair_attempt_completed_event` as concrete local/remote evidence for the corresponding audit events.
- The report row keeps the forbidden terms out of positive evidence and action text; they appear only in the exact required negative scope sentence.
- Missing evidence returns PARTIAL when any CI/repair signal exists, with the missing labels listed in the gap.

## Deviations from Plan

None - plan executed exactly as written.

## Issues Encountered

- `make lint` initially found Ruff format differences in `whilly/compliance/__init__.py` and `tests/unit/test_compliance_report.py`. Applied `python3 -m ruff format whilly/compliance/__init__.py tests/unit/test_compliance_report.py`, reran focused compliance tests, and committed the cleanup as `6598bf7`.
- Pre-existing `.planning/config.json` changes and untracked `.serena/` were left untouched.

## Verification

- RED: `.venv/bin/python -m pytest -q tests/unit/test_compliance_report.py::test_bounded_ci_polling_and_repair_compliance_is_scoped tests/unit/test_compliance_report.py::test_bounded_ci_polling_and_repair_compliance_requires_runtime_evidence tests/unit/test_compliance_report.py::test_bounded_ci_polling_and_repair_compliance_does_not_overclaim_autonomy --maxfail=1` failed as expected with `KeyError: 'Bounded CI polling and repair'`.
- GREEN subset: same command - 3 passed.
- Focused compliance gate: `.venv/bin/python -m pytest -q tests/unit/test_compliance_report.py --maxfail=1` - 13 passed.
- Compliance report: `.venv/bin/python -m whilly compliance report --format markdown --out out/compliance-report.md` - report written.
- Compliance report grep: `rg -n "Bounded CI polling and repair|explicit configured CI polling|bounded repair attempts|repair\\.escalated|No continuous polling" out/compliance-report.md` - matched the PASS row and negative-scope gap.
- Final Phase 11 integration set: `.venv/bin/python -m pytest -q tests/unit/test_ci_polling.py tests/unit/test_repair_loop.py tests/unit/test_project_config.py tests/unit/test_plan_io.py tests/integration/test_plan_io.py tests/unit/test_transport_schemas.py tests/unit/test_remote_client.py tests/unit/test_cli_run.py tests/unit/test_cli_worker.py tests/unit/test_local_worker.py tests/unit/test_remote_worker.py tests/integration/test_transport_tasks.py tests/unit/test_configured_sinks.py tests/unit/test_compliance_report.py --maxfail=1` - 314 passed, 43 skipped.
- Lint gate: `make lint` - Ruff check passed and 447 files already formatted.
- Import-linter: `.venv/bin/lint-imports --config .importlinter` - 2 contracts kept, 0 broken.

## User Setup Required

None - no external service configuration required.

## Next Phase Readiness

Phase 11 now has compliance evidence that matches the local and remote runtime work without claiming continuous polling, auto-merge, production recovery, or unbounded repair. Phase 12 can build on this bounded wording when synchronizing governance and semantic-memory scope.

---
*Phase: 11-ci-polling-and-bounded-repair*
*Completed: 2026-05-08*

## Self-Check: PASSED

- Confirmed `.planning/phases/11-ci-polling-and-bounded-repair/11-06-SUMMARY.md` exists.
- Confirmed task commits exist: `ef2b015`, `f0c9d9e`, and `6598bf7`.
- Confirmed pre-existing `.planning/config.json` and untracked `.serena/` remain outside task commits.
