---
phase: 08-sandbox-and-secrets-hardening
plan: 01
subsystem: security
tags: [secret-lint, prompt-sanitizer, project-config, github-issues]

requires:
  - phase: 07-review-action-affordances
    provides: Current operator review baseline before Phase 8 hardening.
provides:
  - Shared pure secret-lint registry with stable pattern ids and audit-safe findings.
  - Sanitizer and GitHub issue warnings backed by the shared secret-lint contract.
  - Project-config validation that blocks plaintext token-like persisted config values.
affects: [sandbox-and-secrets-hardening, project-config, prompt-sanitizer, source-adapters]

tech-stack:
  added: []
  patterns:
    - Pure stdlib security helper module under whilly.security.
    - Pre-construction validation of persisted project-config dictionaries.

key-files:
  created:
    - whilly/security/secret_lint.py
    - tests/unit/test_secret_lint.py
  modified:
    - whilly/security/prompt_sanitizer.py
    - whilly/project_config/loader.py
    - whilly/sources/github_issues.py
    - tests/unit/test_prompt_sanitizer.py
    - tests/unit/test_prompt_sanitizer_wiring.py
    - tests/unit/test_project_config.py
    - tests/test_github_issues_source.py

key-decisions:
  - "Secret pattern ids and redaction placeholders are centralized in whilly.security.secret_lint."
  - "Project-config secret lint runs on the raw persisted dictionary before dataclass construction."
  - "env:, keyring:, and file: values are accepted as references by prefix only; no resolver runs during validation."

patterns-established:
  - "SecretFinding.event_payload returns deterministic audit-safe fields without raw secret values."
  - "GitHub issue secret warnings expose shared pattern ids instead of raw regex strings."
  - "Project-config paths use project_config.sinks[0].config.token style field paths."

requirements-completed: [SEC-01]

duration: 6min
completed: 2026-05-08T14:05:30Z
---

# Phase 08 Plan 01: Shared Secret-Lint Contract Summary

**Shared SEC-01 secret linting now covers sanitizer text, GitHub issue warnings, and persisted project-config values with stable ids and redacted excerpts.**

## Performance

- **Duration:** 6 min
- **Started:** 2026-05-08T13:59:08Z
- **Completed:** 2026-05-08T14:05:30Z
- **Tasks:** 3
- **Files modified:** 9 code/test files plus this summary

## Accomplishments

- Added `whilly/security/secret_lint.py` with `SecretPattern`, `SecretFinding`, shared pattern ids, redaction, text scans, mapping scans, and audit-safe event payloads.
- Migrated `prompt_sanitizer.py` and `sources/github_issues.py` away from private secret regex registries.
- Added project-config validation that rejects plaintext token-like persisted config values while allowing `env:`, `keyring:`, and `file:` references unchanged.

## Task Commits

Each task was committed atomically with TDD RED and GREEN commits:

1. **Task 1 RED: shared secret-lint tests** - `b1f5abb` (test)
2. **Task 1 GREEN: shared secret-lint contract** - `42555a0` (feat)
3. **Task 2 RED: sanitizer/source migration tests** - `fa5b742` (test)
4. **Task 2 GREEN: sanitizer/source migration** - `c7867ee` (feat)
5. **Task 3 RED: project-config secret-lint tests** - `7c6162d` (test)
6. **Task 3 GREEN: project-config validation wiring** - `5213a77` (feat)

## Files Created/Modified

- `whilly/security/secret_lint.py` - Pure shared registry and helpers for secret redaction, scans, config mapping checks, and audit payloads.
- `tests/unit/test_secret_lint.py` - Coverage for required ids, redaction, mapping scans, references, and audit payload safety.
- `whilly/security/prompt_sanitizer.py` - Delegates secret checks/redaction to `secret_lint`.
- `whilly/sources/github_issues.py` - Uses shared pattern metadata and returns pattern ids for issue-body warnings.
- `whilly/project_config/loader.py` - Blocks plaintext secret-like persisted config values before constructing `ProjectConfig`.
- `tests/unit/test_prompt_sanitizer.py` - Extended sanitizer pattern coverage.
- `tests/unit/test_prompt_sanitizer_wiring.py` - Extended external feedback wiring coverage.
- `tests/unit/test_project_config.py` - API and CLI coverage for config secret lint.
- `tests/test_github_issues_source.py` - Stable pattern-id assertions for GitHub issue warnings.

## Decisions Made

- Centralized runtime secret-pattern ownership in `whilly/security/secret_lint.py` to avoid sanitizer/source drift.
- Kept project-config validation static: references are allowed by prefix and no environment variables, files, or keyring entries are resolved.
- Prioritized concrete secret pattern matches over config-key heuristics, so a plaintext GitHub token under `token` reports `github-token`.

## Deviations from Plan

None - plan executed exactly as written.

## Issues Encountered

- The worktree contained unrelated `.planning/STATE.md` and 08-02-owned file changes from parallel execution. They were left untouched per the ownership instructions.

## User Setup Required

None - no external service configuration required.

## Verification

- `.venv/bin/python -m pytest -q tests/unit/test_secret_lint.py tests/unit/test_prompt_sanitizer.py tests/unit/test_prompt_sanitizer_wiring.py tests/unit/test_project_config.py tests/test_github_issues_source.py --maxfail=1` -> 195 passed.
- `.venv/bin/python -m ruff check whilly/security/secret_lint.py whilly/security/prompt_sanitizer.py whilly/project_config/loader.py whilly/sources/github_issues.py tests/unit/test_secret_lint.py tests/unit/test_prompt_sanitizer.py tests/unit/test_prompt_sanitizer_wiring.py tests/unit/test_project_config.py tests/test_github_issues_source.py` -> All checks passed.

## Next Phase Readiness

SEC-01 now has a shared contract for downstream runner-prompt, task-field, worker, and audit integrations in later Phase 8 plans.

## Self-Check: PASSED

- Confirmed `08-01-SUMMARY.md`, `whilly/security/secret_lint.py`, and `tests/unit/test_secret_lint.py` exist.
- Confirmed task commits exist: `b1f5abb`, `42555a0`, `fa5b742`, `c7867ee`, `7c6162d`, `5213a77`.

---
*Phase: 08-sandbox-and-secrets-hardening*
*Completed: 2026-05-08*
