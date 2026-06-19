---
phase: 18-migration-chain-validation
plan: "01"
subsystem: migrations/testing
tags: [alembic, integration-test, migration-chain, postgres, evidence]
dependency_graph:
  requires: []
  provides: [full-chain-test-coverage-001-028, migration-chain-evidence]
  affects: [tests/integration/test_alembic_full_chain.py, .gitignore]
tech_stack:
  added: []
  patterns: [information_schema-assertions, EXPECTED_CHAIN-single-source-of-truth, evidence-write]
key_files:
  created: []
  modified:
    - tests/integration/test_alembic_full_chain.py
    - .gitignore
decisions:
  - "Use EXPECTED_CHAIN[-1] as single source of truth for head revision; no second literal"
  - "Post-downgrade assertion remains empty-set; all 017-028 migrations have real drop_table/drop_column"
  - "Evidence file contains only revision string, count, booleans — no DSN/password (T-18-01)"
metrics:
  duration: "19 min"
  completed: "2026-06-11"
  tasks_completed: 3
  files_modified: 2
---

# Phase 18 Plan 01: Extend Alembic Full-Chain Test to 028 Summary

**One-liner:** Extended full-chain integration test from 016 to 028_webauthn_user_handles with structural assertions for new tables/columns, downgrade coverage, and machine-readable evidence write.

## Tasks Completed

| Task | Name | Commit | Files |
|------|------|--------|-------|
| 1 | Extend EXPECTED_CHAIN to 028 and fix stale head-revision assertions | fb8c483 | tests/integration/test_alembic_full_chain.py |
| 2 | Add structural assertions for 017 tables and 019a columns; extend post-downgrade set | 5c0f768 | tests/integration/test_alembic_full_chain.py |
| 3 | Write machine-readable evidence file and gitignore it | 16384ff | tests/integration/test_alembic_full_chain.py, .gitignore |

## What Was Built

- **EXPECTED_CHAIN** extended from 16 entries (ending at `016_jira_work_sessions`) to 28 entries (ending at `028_webauthn_user_handles`), including `019a_plans_archived_at` with intentional 'a' suffix.
- **Head-revision assertions** in `test_full_chain_upgrade_then_full_downgrade` and both assertions in `test_full_chain_then_re_upgrade_idempotent` now reference `EXPECTED_CHAIN[-1]` — single source of truth.
- **Structural assertions** added for migration 017 (`scheduler_rules`, `scheduler_poll_cycles` tables) and migration 019a (`archived_at`, `last_event_at` columns on `plans`), following the existing `information_schema` query style.
- **post_downgrade_tables** extended to include all 017–028 tables (`scheduler_rules`, `scheduler_poll_cycles`, `sessions`, `magic_links`, `users`, `user_totp_secrets`, `auth_audit`, `webauthn_credentials`, `webauthn_challenges`, `webauthn_user_handles`); assertion still checks `== set()`.
- **Evidence write** added at end of `test_full_chain_then_re_upgrade_idempotent`: writes `migration-chain-evidence.json` at repo root with `timestamp`, `head_revision` (`EXPECTED_CHAIN[-1]`), `migration_count` (`len(EXPECTED_CHAIN)`), and three boolean pass flags. No DSN written.
- **`.gitignore`** updated with `migration-chain-evidence.json` entry next to `.whilly_state.json`.

## Verification

All static acceptance criteria pass:
- `ast.parse` prints "parse ok"
- `028_webauthn_user_handles` appears in EXPECTED_CHAIN (1 occurrence)
- `grep -v '^#' ... | grep -c '016_jira_work_sessions'` returns 1 (only EXPECTED_CHAIN tuple member; all `== "016..."` comparisons replaced)
- `scheduler_poll_cycles` appears 4 times (no `scheduler_cycles` typo)
- `webauthn_user_handles` appears 2 times (EXPECTED_CHAIN + post_downgrade_tables)
- `migration-chain-evidence.json` in test file and in .gitignore
- Evidence dict uses `EXPECTED_CHAIN[-1]` and `len(EXPECTED_CHAIN)` — no hardcoded "028..." or 28 literal
- No DSN/secret in evidence dict

Full behavioral proof requires Docker:
```
pytest -q tests/integration/test_alembic_full_chain.py --tb=short
```
(Skips gracefully if Docker is absent; runs green in CI via Plan 02.)

## Deviations from Plan

None — plan executed exactly as written. Ruff auto-reformatted the file on the first commit (expected; pre-commit hook enforces formatting).

## Threat Surface

No new threat surface beyond what is already in the plan's threat model:
- T-18-01 (mitigated): evidence file contains only revision string, count, booleans — no DSN, password, or host
- T-18-02 (mitigated): no DSN printed to stdout; structural assertions query schema, not credentials
- T-18-03 (accepted): evidence file is git-ignored; cannot be committed

## Self-Check: PASSED

- tests/integration/test_alembic_full_chain.py: FOUND
- .gitignore (migration-chain-evidence.json entry): FOUND
- fb8c483: FOUND
- 5c0f768: FOUND
- 16384ff: FOUND
