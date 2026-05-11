---
phase: 12-governance-and-semantic-memory-decision
plan: 01
subsystem: governance-compliance-docs
tags: [governance, compliance, semantic-memory, docs, tdd]

requires:
  - phase: 11-ci-polling-and-bounded-repair
    provides: Scoped compliance wording for explicit configured CI polling, bounded repair attempts, and non-overclaiming repair boundaries.
provides:
  - Pure deterministic governance risk policy for migration, auth, infrastructure, dependencies, release, and external_pr work.
  - Compliance rows for Governance risk policy and explicit Semantic memory deferral.
  - Synchronized current-vs-target docs for governance, rollback, CI repair, sandbox limits, and semantic-memory scope.
affects: [compliance-reporting, governance-policy, current-vs-target-docs, semantic-memory-scope]

tech-stack:
  added: []
  patterns:
    - Pure core policy modules expose frozen value objects and deterministic stdlib-only scoring.
    - Compliance rows pass only from concrete code/test evidence and use scoped non-overclaiming wording.
    - Docs drift tests pin exact current-vs-target phrases shared with rendered compliance output.

key-files:
  created:
    - whilly/core/governance.py
    - tests/unit/core/test_governance_policy.py
    - .planning/phases/12-governance-and-semantic-memory-decision/12-01-SUMMARY.md
  modified:
    - whilly/compliance/__init__.py
    - tests/unit/test_compliance_report.py
    - docs/Current-vs-Target.md
    - README.md
    - README-RU.md
    - docs/index.md
    - docs/Project-Description.md
    - docs/target/04_Compliance_Validation_Guide.md
    - docs/target/06_Autonomous_Developer_Roadmap.md

key-decisions:
  - "Governance risk policy is deterministic, pure, inspectable, and evidence-reported; it does not claim autonomous production release or default auto-merge."
  - "Semantic memory is explicitly deferred from current scope; deterministic events, task history, PR evidence, and verification logs remain authoritative."
  - "Operator-triggered rollback, explicit configured CI polling, and bounded repair attempts are current scoped capabilities; continuous polling, auto-merge, production recovery, and unbounded repair are not claimed."

patterns-established:
  - "Governance policy findings carry category, score, reason, matched_signal, and approval_boundary in category-order output."
  - "Compliance report tests now act as docs drift tests for exact Phase 8-12 scope wording."

requirements-completed: [DOC-04, GOV-01, GOV-02]

duration: 10 min 44 sec
completed: 2026-05-08
---

# Phase 12 Plan 01: Governance and Semantic-Memory Decision Summary

**Deterministic governance risk scoring plus explicit semantic-memory deferral across compliance and current-vs-target docs**

## Performance

- **Duration:** 10 min 44 sec
- **Started:** 2026-05-08T19:35:21Z
- **Completed:** 2026-05-08T19:46:04Z
- **Tasks:** 3 TDD tasks
- **Files modified:** 11 plus this summary

## Accomplishments

- Added `whilly/core/governance.py`, a pure stdlib-only scorer for `migration`, `auth`, `infrastructure`, `dependencies`, `release`, and `external_pr` work.
- Added compliance evidence for `Governance risk policy` and changed `Semantic memory` to explicit current-scope deferral rather than an implemented claim.
- Updated current-vs-target docs, READMEs, docs home, project description, and target docs to share the same scoped wording.
- Added tests that prevent positive current claims for semantic long-term memory, full sandbox/VM isolation, default auto-merge, continuous autonomous repair, or autonomous production release.

## Task Commits

Each TDD task was committed atomically:

1. **Task 1 RED: governance policy tests** - `723dfa1` (test)
2. **Task 1 GREEN: deterministic governance policy** - `de501d8` (feat)
3. **Task 2 RED: compliance scope tests** - `daeac4f` (test)
4. **Task 2 GREEN: governance and semantic-memory compliance rows** - `58fa818` (feat)
5. **Task 3 RED: docs scope drift tests** - `f543f5b` (test)
6. **Task 3 GREEN: synchronized docs and compliance wording** - `ed63a20` (docs)
7. **Task 3 REFACTOR: compliance formatting** - `af623cb` (refactor)

**Plan metadata:** captured by the final GSD metadata commit.

## Files Created/Modified

- `whilly/core/governance.py` - Pure deterministic governance risk scoring and immutable output types.
- `tests/unit/core/test_governance_policy.py` - Category, approval-boundary, determinism, ordering, low-risk, and no-I/O tests.
- `whilly/compliance/__init__.py` - Evidence-scanned governance row, semantic-memory deferral row, and deferral-aware doc mismatch logic.
- `tests/unit/test_compliance_report.py` - Compliance row tests plus docs/compliance drift guards.
- `docs/Current-vs-Target.md` - Canonical Phase 8-12 current-vs-target wording.
- `README.md`, `README-RU.md`, `docs/index.md`, `docs/Project-Description.md` - User-facing scope wording aligned to compliance.
- `docs/target/04_Compliance_Validation_Guide.md` - Target compliance guide aligned to current scoped capabilities.
- `docs/target/06_Autonomous_Developer_Roadmap.md` - Future semantic-memory and autonomous-developer roadmap wording kept in target scope.

## Decisions Made

- Governance scoring remains pure and deterministic, based only on caller-supplied metadata, with no filesystem, network, subprocess, database, framework, git, GitHub, or LLM calls.
- Semantic memory is not implemented in this plan; compliance and docs explicitly defer it and keep deterministic audit/task/PR/verification evidence authoritative.
- Governance policy is reported as evidence and recommended gates only; it does not claim autonomous production release enforcement, default auto-merge, or production recovery.

## Deviations from Plan

None - plan executed exactly as written.

## Issues Encountered

- `make lint` initially reported Ruff formatting differences in `whilly/compliance/__init__.py` and `tests/unit/test_compliance_report.py`. Applied `python3 -m ruff format whilly/compliance/__init__.py tests/unit/test_compliance_report.py`, reran focused tests and lint, and committed the cleanup as `af623cb`.
- Pre-existing `.planning/config.json` changes and untracked `.serena/` were left untouched.

## Verification

- RED Task 1: `.venv/bin/python -m pytest -q tests/unit/core/test_governance_policy.py --maxfail=1` failed as expected with missing `whilly.core.governance`.
- GREEN Task 1: `.venv/bin/python -m pytest -q tests/unit/core/test_governance_policy.py --maxfail=1` - 10 passed.
- Core purity smoke: `.venv/bin/python -m pytest -q tests/integration/test_phase1_smoke.py::test_whilly_core_is_importable_without_io_dependencies tests/integration/test_phase1_smoke.py::test_whilly_core_subprocess_and_chdir_grep_clean --maxfail=1` - 2 passed.
- RED Task 2: `.venv/bin/python -m pytest -q tests/unit/test_compliance_report.py --maxfail=1` failed as expected with missing `Governance risk policy`.
- GREEN Task 2: `.venv/bin/python -m pytest -q tests/unit/test_compliance_report.py --maxfail=1` - 17 passed.
- RED Task 3: `.venv/bin/python -m pytest -q tests/unit/test_compliance_report.py --maxfail=1` failed as expected on stale current-vs-target docs.
- GREEN Task 3 and final focused gate: `.venv/bin/python -m pytest -q tests/unit/core/test_governance_policy.py tests/unit/test_compliance_report.py --maxfail=1` - 30 passed.
- Final core purity smoke: `.venv/bin/python -m pytest -q tests/integration/test_phase1_smoke.py::test_whilly_core_is_importable_without_io_dependencies tests/integration/test_phase1_smoke.py::test_whilly_core_subprocess_and_chdir_grep_clean --maxfail=1` - 2 passed.
- Compliance report: `.venv/bin/python -m whilly compliance report --format markdown --out out/compliance-report.md` - report written.
- Scope phrase scan: `rg -n "Governance risk policy|Semantic memory is explicitly deferred|deterministic governance risk policy|explicit configured CI polling|bounded repair attempts|operator-triggered rollback" out/compliance-report.md docs/Current-vs-Target.md README.md README-RU.md docs/index.md docs/Project-Description.md docs/target/04_Compliance_Validation_Guide.md docs/target/06_Autonomous_Developer_Roadmap.md` - matched all required surfaces.
- Lint gate: `make lint` - Ruff check passed and 449 files already formatted.
- Import-linter: `.venv/bin/lint-imports --config .importlinter` - 2 contracts kept, 0 broken.

## User Setup Required

None - no external service configuration required.

## Next Phase Readiness

Phase 12 plan 01 closes the current roadmap requirements DOC-04, GOV-01, and GOV-02. The milestone is ready for verification or milestone completion without claiming implemented semantic memory, autonomous production release, default auto-merge, continuous autonomous repair, or full sandbox/VM isolation.

---
*Phase: 12-governance-and-semantic-memory-decision*
*Completed: 2026-05-08*

## Self-Check: PASSED

- Confirmed `.planning/phases/12-governance-and-semantic-memory-decision/12-01-SUMMARY.md` exists.
- Confirmed key created files exist: `whilly/core/governance.py` and `tests/unit/core/test_governance_policy.py`.
- Confirmed task commits exist: `723dfa1`, `de501d8`, `daeac4f`, `58fa818`, `f543f5b`, `ed63a20`, and `af623cb`.
- Confirmed pre-existing `.planning/config.json` and untracked `.serena/` remain outside task commits.
