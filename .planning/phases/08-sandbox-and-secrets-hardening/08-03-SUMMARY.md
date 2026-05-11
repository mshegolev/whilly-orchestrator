---
phase: 08-sandbox-and-secrets-hardening
plan: 03
subsystem: security
tags: [secret-lint, worker-guards, verification-redaction, compliance]

requires:
  - phase: 08-sandbox-and-secrets-hardening
    provides: Shared secret-lint contract from 08-01.
  - phase: 08-sandbox-and-secrets-hardening
    provides: Runner environment allowlist contract from 08-02.
provides:
  - Worker pre-run secret lint blocking on local and remote runner paths.
  - Remote transport prelude acceptance for secret_lint_blocked failures.
  - Verification stdout, stderr, and command redaction before audit persistence.
  - Compliance and docs residual-risk evidence for guards without claiming full VM/container isolation.
affects: [worker-security, transport-audit, verification, compliance-docs]

tech-stack:
  added: []
  patterns:
    - Reuse whilly.security.secret_lint as the audit-safe redaction boundary.
    - Mirror prompt/shell guard fail-before-runner branches for secret lint findings.

key-files:
  created:
    - .planning/phases/08-sandbox-and-secrets-hardening/08-03-SUMMARY.md
  modified:
    - whilly/core/agent_runner.py
    - whilly/worker/local.py
    - whilly/worker/remote.py
    - whilly/adapters/transport/server.py
    - whilly/pipeline/verification.py
    - whilly/compliance/__init__.py
    - docs/CODEX-MISSION.md
    - docs/Current-vs-Target.md
    - tests/unit/test_local_worker.py
    - tests/unit/test_remote_worker.py
    - tests/integration/test_transport_tasks.py
    - tests/unit/test_verification_runner.py
    - tests/unit/test_compliance_report.py

key-decisions:
  - "Secret lint blocks local and remote worker execution after prompt construction and before runner invocation."
  - "Remote fail detail with event_type=secret_lint_blocked is treated as a security prelude event."
  - "Sandbox/VM isolation remains PARTIAL; docs and compliance describe improved guards as residual-risk reduction only."

patterns-established:
  - "scan_task_secret_surface scans task fields and rendered runner prompt with stable field paths."
  - "Verification audit details are redacted both when command results are captured and when events are built."

requirements-completed: [SEC-01, SEC-02, SEC-03]

duration: 9min
completed: 2026-05-08T14:18:06Z
---

# Phase 08 Plan 03: Worker Guard and Audit Redaction Summary

**Secret-lint worker blocking, remote audit preludes, verification redaction, and residual-risk compliance evidence without overclaiming VM isolation**

## Performance

- **Duration:** 9 min
- **Started:** 2026-05-08T14:09:15Z
- **Completed:** 2026-05-08T14:18:06Z
- **Tasks:** 2
- **Files modified:** 13 plus this summary

## Accomplishments

- Added `scan_task_secret_surface()` to scan task description, PRD requirement, acceptance criteria, test steps, and rendered runner prompt text.
- Wired local and remote workers to fail with `secret_lint_blocked` before runner invocation, using deterministic audit-safe payload fields.
- Allowed remote `/tasks/{id}/fail` requests carrying `secret_lint_blocked` detail to persist a security prelude event before `FAIL`.
- Redacted verification command output and event detail fields before audit persistence.
- Updated compliance and docs to name prompt, shell, secret, and runner-env guards while keeping full per-task VM/container isolation as future work.

## Task Commits

Each task was committed atomically with TDD RED and GREEN commits:

1. **Task 1 RED: secret guard worker tests** - `8c4fc1e` (test)
2. **Task 1 GREEN: worker secret guard blocking** - `6599b18` (feat)
3. **Task 2 RED: verification redaction and compliance tests** - `31cf1b9` (test)
4. **Task 2 GREEN: verification redaction and residual-risk docs** - `2a37384` (feat)

## Files Created/Modified

- `whilly/core/agent_runner.py` - Adds `scan_task_secret_surface()` and exports the secret guard constants/finding type.
- `whilly/worker/local.py` - Blocks secret findings before shell scanning and runner invocation, with prelude payloads.
- `whilly/worker/remote.py` - Sends `client.fail(... reason="secret_lint_blocked", detail=payload)` before runner invocation.
- `whilly/adapters/transport/server.py` - Accepts `secret_lint_blocked` as a security prelude event type.
- `whilly/pipeline/verification.py` - Redacts stdout, stderr, and event command values at verification/audit boundaries.
- `whilly/compliance/__init__.py` - Keeps sandbox/VM isolation `PARTIAL` while naming concrete guards and residual risk.
- `docs/CODEX-MISSION.md` and `docs/Current-vs-Target.md` - Document completed guards and future VM/container isolation.
- Worker, transport, verification, and compliance tests now pin the new payload shapes and redaction behavior.

## Decisions Made

- Secret lint runs after prompt construction so both raw task fields and rendered prompt text are available, but still before shell scanning and runner calls.
- Local worker uses a repository prelude event directly; remote worker sends the same event payload through fail detail and lets the server persist the prelude.
- Full sandbox/VM isolation was not reframed as complete; the plan only documents guard improvements and residual risk.

## Deviations from Plan

None - plan executed within the requested ownership boundary.

## Issues Encountered

- `make test` is not clean due to out-of-scope failures:
  - `tests/integration/test_alembic_013_work_intents.py::test_013_is_head_revision` expects head `013_work_intents_repo_targets`, while the current script head is `014_control_state`.
  - `tests/unit/test_readme_quickstart_extractable.py::test_long_running_block_is_segregated_in_readme` reports `README.md` lacks the required long-running second-terminal bash block.
- Full `.venv/bin/python -m ruff format --check whilly/ tests/` is not clean due to out-of-scope formatting in `tests/unit/test_project_config.py` and `whilly/sources/github_issues.py`.
- Owned Python files passed format check; these out-of-scope files were not modified per the plan ownership rule.

## Verification

- `.venv/bin/python -m pytest -q tests/unit/test_secret_lint.py --maxfail=1` -> 39 passed.
- `.venv/bin/python -m pytest -q tests/unit/test_local_worker.py --maxfail=1` -> 28 passed.
- `.venv/bin/python -m pytest -q tests/unit/test_remote_worker.py --maxfail=1` -> 31 passed.
- `.venv/bin/python -m pytest -q tests/integration/test_transport_tasks.py --maxfail=1` -> 29 skipped.
- `.venv/bin/python -m pytest -q tests/unit/test_secret_lint.py tests/unit/test_local_worker.py tests/unit/test_remote_worker.py tests/integration/test_transport_tasks.py --maxfail=1` -> 98 passed, 29 skipped.
- `.venv/bin/python -m pytest -q tests/unit/test_verification_runner.py tests/unit/test_compliance_report.py --maxfail=1` -> 18 passed.
- Final focused Phase 8 pytest command from the plan -> 398 passed, 29 skipped.
- `.venv/bin/python -m ruff check whilly/ tests/` -> All checks passed.
- `.venv/bin/lint-imports --config .importlinter` -> 2 contracts kept, 0 broken.
- `.venv/bin/python -m ruff format --check` on all owned Python files -> 11 files already formatted.
- `make test` -> 2751 passed, 643 skipped, 2 failed due out-of-scope tests listed above.
- Full `.venv/bin/python -m ruff format --check whilly/ tests/` -> failed on two out-of-scope files listed above.

## User Setup Required

None - no external service configuration required.

## Next Phase Readiness

SEC-01, SEC-02, and SEC-03 have the worker, transport, verification, and compliance wiring needed for Phase 9 to build on profile-native verification without inheriting raw secrets or overclaiming sandbox isolation.

## Self-Check: PASSED

- Confirmed `.planning/phases/08-sandbox-and-secrets-hardening/08-03-SUMMARY.md` exists.
- Confirmed task commits exist: `8c4fc1e`, `6599b18`, `31cf1b9`, and `2a37384`.
- Confirmed `.planning/STATE.md` and `.planning/ROADMAP.md` were not updated by this executor.

---
*Phase: 08-sandbox-and-secrets-hardening*
*Completed: 2026-05-08*
