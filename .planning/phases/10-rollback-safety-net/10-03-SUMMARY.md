---
phase: 10-rollback-safety-net
plan: 03
subsystem: rollback-safety
tags: [git, rollback, preflight, github-pr, compliance]

requires:
  - phase: 10-rollback-safety-net
    provides: Rollback core service, rollback CLI, backup tags, preflight reports, and confirmation-gated restore.
provides:
  - PR push preflight before `git push origin HEAD:<branch> --force-with-lease`.
  - Structured `rollback_preflight_failed` PRResult and `pr.open_failed` audit evidence.
  - Git rollback compliance row based on service, CLI, PR preflight, and test evidence.
affects: [pr-preflight, post-complete-pr-hook, compliance, phase-11-ci-repair]

tech-stack:
  added: []
  patterns:
    - Injectable preflight builder for PR sink tests while production defaults to rollback service evidence.
    - Compliance helper functions that classify capability status from concrete repository signals.

key-files:
  created:
    - .planning/phases/10-rollback-safety-net/10-03-SUMMARY.md
  modified:
    - whilly/sinks/github_pr.py
    - whilly/compliance/__init__.py
    - tests/test_github_pr_sink.py
    - tests/unit/test_pr_hook_failure_events.py
    - tests/integration/test_post_complete_pr_hook.py
    - tests/unit/test_compliance_report.py

key-decisions:
  - "PR push preflight uses the computed branch string passed to `git push origin HEAD:<branch>`."
  - "Preflight blockers return `PRResult(failure_mode=\"rollback_preflight_failed\")` and skip push/PR creation."
  - "Compliance describes Git rollback as operator-triggered only; no autonomous recovery."

patterns-established:
  - "PR sink mutation guard: compute branch, verify worktree exists, build rollback preflight, then construct and execute push."
  - "Rollback compliance status is signal-based instead of tied to legacy verifier helper behavior."

requirements-completed: [ROLL-02, ROLL-03]

duration: 7 min 27 sec
completed: 2026-05-08
---

# Phase 10 Plan 03: PR Push Preflight and Compliance Evidence Summary

**PR push mutation now runs rollback preflight before force-with-lease, and compliance recognizes scoped Git rollback safety-net support**

## Performance

- **Duration:** 7 min 27 sec
- **Started:** 2026-05-08T17:02:09Z
- **Completed:** 2026-05-08T17:09:36Z
- **Tasks:** 2
- **Files modified:** 6

## Accomplishments

- Added rollback preflight to `open_pr_for_task()` before `git push`, passing the computed PR branch as `target_ref`.
- Added structured preflight blocker handling with `PRResult(ok=False, failure_mode="rollback_preflight_failed")` and no push or `gh pr create` call.
- Extended post-complete PR hook evidence coverage so rollback preflight failures become one `pr.open_failed` payload with reason, branch, and failure mode.
- Upgraded compliance so `Git rollback` passes only with service, CLI, PR preflight, and restore-test evidence, while preserving scoped wording: `operator-triggered only; no autonomous recovery`.

## Task Commits

Each TDD task was committed atomically:

1. **Task 1 RED: PR push preflight coverage** - `6b06839` (test)
2. **Task 1 GREEN: PR push preflight guard** - `925b7cb` (feat)
3. **Task 2 RED: Git rollback compliance evidence test** - `aeda382` (test)
4. **Task 2 GREEN: Git rollback compliance helpers** - `a8cc7f3` (feat)

## Files Created/Modified

- `whilly/sinks/github_pr.py` - Adds `preflight_builder`, default rollback preflight, and `rollback_preflight_failed` handling before push.
- `whilly/compliance/__init__.py` - Adds `_git_rollback_*` helpers and scoped PASS evidence for Git rollback.
- `tests/test_github_pr_sink.py` - Covers preflight ordering, target ref, blocker behavior, and preserved push/PR behavior.
- `tests/unit/test_pr_hook_failure_events.py` - Covers rollback preflight failure event payloads and clean-preflight wrappers for existing PR failure tests.
- `tests/integration/test_post_complete_pr_hook.py` - Uses a tiny clean Git repository fixture so default preflight can inspect real Git state.
- `tests/unit/test_compliance_report.py` - Verifies Git rollback PASS evidence and guards against recovery overclaims.

## Decisions Made

- The PR sink passes `target_ref=branch`, matching the branch used by `git push origin HEAD:<branch>`.
- Missing backup points and unknown branch protection remain warnings through `report.ok`; blockers are taken directly from the rollback preflight report.
- Compliance keeps rollback scope explicit and does not claim autonomous recovery, CI repair, or broader Phase 11 behavior.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 3 - Blocking] Formatted compliance implementation after lint failure**
- **Found during:** Final verification
- **Issue:** `make lint` reported `Would reformat: whilly/compliance/__init__.py` after the compliance helper implementation.
- **Fix:** Ran Ruff format on the plan-owned compliance file and amended the Task 2 GREEN commit.
- **Files modified:** `whilly/compliance/__init__.py`
- **Verification:** `.venv/bin/python -m pytest -q tests/unit/test_compliance_report.py --maxfail=1` and `python3 -m ruff format --check whilly/compliance/__init__.py whilly/sinks/github_pr.py tests/test_github_pr_sink.py tests/unit/test_pr_hook_failure_events.py tests/integration/test_post_complete_pr_hook.py tests/unit/test_compliance_report.py`
- **Committed in:** `a8cc7f3`

---

**Total deviations:** 1 auto-fixed (1 blocking issue)
**Impact on plan:** Formatting fix only; no behavior or scope expansion.

## Issues Encountered

- Pre-existing untracked `.serena/` remained untouched.
- Post-plan orchestration cleanup fixed Phase 10 fallout outside Plan 10-03's original write set:
  `tests/unit/test_rollback.py` was Ruff-formatted, and
  `tests/unit/test_pr_title_argv_sanitization.py` now injects clean rollback preflight evidence so the tests remain focused on PR title/body argv sanitization.
- `make test` still has one unrelated README quickstart failure:
  `tests/unit/test_readme_quickstart_extractable.py::test_long_running_block_is_segregated_in_readme`.
  Final full-suite line after cleanup: `1 failed, 2796 passed, 648 skipped, 10 warnings in 65.61s`.

## Verification

- `.venv/bin/python -m pytest -q tests/test_github_pr_sink.py tests/unit/test_pr_hook_failure_events.py tests/integration/test_post_complete_pr_hook.py --maxfail=1` - `36 passed, 3 skipped`
- `.venv/bin/python -m pytest -q tests/unit/test_compliance_report.py --maxfail=1` - `10 passed`
- `.venv/bin/python -m pytest -q tests/unit/test_rollback.py tests/integration/test_rollback_cli.py tests/test_github_pr_sink.py tests/unit/test_pr_hook_failure_events.py tests/integration/test_post_complete_pr_hook.py tests/unit/test_compliance_report.py --maxfail=1` - `65 passed, 3 skipped`
- `.venv/bin/python -m whilly compliance report --format markdown --out out/compliance-report.md` - succeeded
- `rg -n "Git rollback|backup tags|PR push preflight" out/compliance-report.md` - found upgraded PASS row
- `.venv/bin/lint-imports --config .importlinter` - `2 kept, 0 broken`
- `python3 -m ruff format --check whilly/compliance/__init__.py whilly/sinks/github_pr.py tests/test_github_pr_sink.py tests/unit/test_pr_hook_failure_events.py tests/integration/test_post_complete_pr_hook.py tests/unit/test_compliance_report.py` - `6 files already formatted`
- `make lint` - `All checks passed`; `435 files already formatted`
- `.venv/bin/python -m pytest -q tests/unit/test_pr_title_argv_sanitization.py --maxfail=1` - `6 passed`
- `.venv/bin/python -m pytest -q tests/unit/test_rollback.py tests/integration/test_rollback_cli.py tests/test_github_pr_sink.py tests/unit/test_pr_hook_failure_events.py tests/integration/test_post_complete_pr_hook.py tests/unit/test_compliance_report.py --maxfail=1` - `65 passed, 3 skipped`
- `make test` - failed only on the unrelated README quickstart test, exact summary documented above

## User Setup Required

None - no external service configuration required.

## Next Phase Readiness

Phase 10 rollback safety-net behavior is complete for the targeted gate: rollback core, CLI, PR preflight, hook evidence, compliance evidence, lint, and Phase 10 targeted tests are green. The remaining full-suite failure is the unrelated README quickstart extraction test documented above.

---
*Phase: 10-rollback-safety-net*
*Completed: 2026-05-08*

## Self-Check: PASSED

- Verified key files exist.
- Verified task commits exist: `6b06839`, `925b7cb`, `aeda382`, `a8cc7f3`.
