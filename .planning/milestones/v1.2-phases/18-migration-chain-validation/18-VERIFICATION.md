---
phase: 18-migration-chain-validation
verified: 2026-06-11T10:05:00Z
status: passed
score: 8/8 must-haves verified
overrides_applied: 0
human_verification:
  - test: "Push the fix/dashboard-log-path-read-sink branch and observe the migration-chain CI job on ubuntu-latest"
    expected: "migration-chain job runs green with a migration-chain-evidence artifact uploaded containing upgrade_ok/downgrade_ok/idempotent_ok all true"
    why_human: "GitHub Actions ubuntu-latest runner is a remote environment — cannot be verified from the local workstation without an actual push and pipeline run"
---

# Phase 18: Migration Chain Validation Verification Report

**Phase Goal:** The full Alembic migration chain is verified repeatable from a clean state, giving operators and CI confidence in the data layer before live integration work begins.
**Verified:** 2026-06-11T10:05:00Z
**Status:** human_needed
**Re-verification:** No — initial verification

## Goal Achievement

### Observable Truths

| # | Truth | Status | Evidence |
|---|-------|--------|----------|
| 1 | Single command applies all migrations against empty Docker Postgres | VERIFIED | `make migrate-chain PYTHON=.venv/bin/python` ran 3 tests, all passed, exit 0 in 4.53s |
| 2 | Re-run from reset container is identically green (idempotency) | VERIFIED | `test_full_chain_then_re_upgrade_idempotent` passes; second `upgrade head` still reports `EXPECTED_CHAIN[-1]`; `_RESULTS["idempotent_ok"]=True` only set after assertions pass |
| 3 | CI entry point exists with no manual steps | VERIFIED | `Makefile:62` `migrate-chain:` target is `.PHONY`, listed by `make help`, invokes pytest on the test file; `.github/workflows/ci.yml:312` `migration-chain` job mirrors all post-lint jobs with `needs: lint`, no manual steps |
| 4 | Inspectable evidence records exit code, count, and revision | VERIFIED | Evidence file written to `REPO_ROOT/migration-chain-evidence.json` by session fixture; post-run content: `{"timestamp":"2026-06-11T10:03:31.161114Z","head_revision":"028_webauthn_user_handles","migration_count":28,"upgrade_ok":true,"downgrade_ok":true,"idempotent_ok":true}` |
| 5 | EXPECTED_CHAIN covers all 28 revisions; head assertions reference EXPECTED_CHAIN[-1] | VERIFIED | 28-entry tuple ending at `028_webauthn_user_handles`; all three head assertions (`lines 213, 582, 586`) use `EXPECTED_CHAIN[-1]`; only 1 occurrence of `016_jira_work_sessions` (EXPECTED_CHAIN member only) |
| 6 | Evidence flags are honest (not fabricated constants) | VERIFIED | `_RESULTS` dict with `.get(..., False)` defaults; `upgrade_ok` set at line 536 only after all upgrade assertions pass; `downgrade_ok` at line 567 only after `post_downgrade_tables == []` passes; `idempotent_ok` at line 591 only after both version checks pass |
| 7 | On-disk chain guard enforces set equality (cannot silently accumulate new migrations) | VERIFIED | `test_expected_chain_files_exist_on_disk` uses `assert on_disk == set(EXPECTED_CHAIN)` with explicit diff message; 28 files on disk exactly match EXPECTED_CHAIN (verified: `ls versions/` shows 28 .py files excluding `__init__`) |
| 8 | No DSN/secret leaks into evidence file or CI logs | VERIFIED | Evidence dict keys: `timestamp`, `head_revision`, `migration_count`, `upgrade_ok`, `downgrade_ok`, `idempotent_ok` — no `dsn`, `password`, `database_url`; CI job has no `secrets.*` references beyond the pre-existing `secrets.GITHUB_TOKEN` in the lint job |

**Score:** 8/8 truths verified

### Required Artifacts

| Artifact | Expected | Status | Details |
|----------|----------|--------|---------|
| `tests/integration/test_alembic_full_chain.py` | Full-chain upgrade/downgrade/idempotency test covering all 28 revisions + evidence write | VERIFIED | 592 lines; parses as valid Python; EXPECTED_CHAIN has 28 entries; `_write_evidence` session fixture; EVIDENCE_PATH anchored to REPO_ROOT |
| `.gitignore` | Ignore rule for migration-chain-evidence.json | VERIFIED | Line 45: `migration-chain-evidence.json` |
| `Makefile` | migrate-chain target running the full-chain integration test | VERIFIED | Lines 62-65; `.PHONY` includes `migrate-chain`; `## Run full Alembic migration chain validation (requires Docker)` docstring; listed by `make help` |
| `.github/workflows/ci.yml` | migration-chain CI job invoking make migrate-chain | VERIFIED | Lines 312-345; `runs-on: ubuntu-latest`; `needs: lint`; `ref: ${{ github.head_ref \|\| github.ref_name }}`; hard evidence gate at line 334; upload with `if: always()` |

### Key Link Verification

| From | To | Via | Status | Details |
|------|----|-----|--------|---------|
| `tests/integration/test_alembic_full_chain.py` | `EXPECTED_CHAIN[-1]` | head-revision assertions | VERIFIED | Lines 213, 582, 586 all use `EXPECTED_CHAIN[-1]`; no `== "028..."` or `== "016..."` literal comparisons remain |
| `tests/integration/test_alembic_full_chain.py` | `migration-chain-evidence.json` | `EVIDENCE_PATH.write_text(json.dumps(evidence, indent=2))` | VERIFIED | Line 83; `EVIDENCE_PATH = REPO_ROOT / "migration-chain-evidence.json"` anchored at line 51 |
| `Makefile migrate-chain target` | `tests/integration/test_alembic_full_chain.py` | `$(PYTHON) -m pytest` invocation | VERIFIED | Makefile line 64: `tests/integration/test_alembic_full_chain.py` |
| `.github/workflows/ci.yml migration-chain job` | `make migrate-chain` | `run: make migrate-chain` step | VERIFIED | CI line 326 |
| `.github/workflows/ci.yml migration-chain job` | `migration-chain-evidence.json` | `actions/upload-artifact@v4` | VERIFIED | CI lines 335-345; `if: always()`; artifact name `migration-chain-evidence`; `if-no-files-found: warn` (acceptable — hard existence gate is dedicated `test -f` step at line 334) |

### Data-Flow Trace (Level 4)

Not applicable — this phase produces test infrastructure and CI configuration, not components that render dynamic data from a database query.

### Behavioral Spot-Checks

| Behavior | Command | Result | Status |
|----------|---------|--------|--------|
| Full chain upgrades + downgrades + idempotency green from Docker Postgres | `make migrate-chain PYTHON=.venv/bin/python` | 3 passed in 4.53s, exit 0 | PASS |
| Evidence file written with correct structure after test run | `cat migration-chain-evidence.json` | `{"timestamp":..., "head_revision":"028_webauthn_user_handles", "migration_count":28, "upgrade_ok":true, "downgrade_ok":true, "idempotent_ok":true}` | PASS |
| `make help` lists migrate-chain | `make help PYTHON=.venv/bin/python \| grep migrate-chain` | `migrate-chain     Run full Alembic migration chain validation (requires Docker)` | PASS |
| CI YAML parses and job structure is correct | `.venv/bin/python -c "import yaml; ..."` | `ubuntu-latest`, `needs: lint`, `ref: ${{ github.head_ref \|\| github.ref_name }}`, upload `if: always()` | PASS |
| Test file is valid Python | `.venv/bin/python -c "import ast; ast.parse(...)"` | `parse ok` | PASS |

### Probe Execution

No `scripts/*/tests/probe-*.sh` declared or conventional for this phase type. Step 7c SKIPPED — behavioral spot-checks above subsume the runnable validation.

### Requirements Coverage

| Requirement | Source Plan | Description | Status | Evidence |
|-------------|-------------|-------------|--------|----------|
| MIG-01 | 18-01-PLAN.md | The full Alembic migration chain runs green from an empty Postgres in Docker | SATISFIED | `make migrate-chain` ran all 3 tests green; evidence file confirms `upgrade_ok`, `downgrade_ok`, `idempotent_ok` all true |
| MIG-02 | 18-02-PLAN.md | Chain validation is repeatable via a scripted/CI entry point, not a one-off manual run | SATISFIED | `make migrate-chain` Makefile target + `migration-chain` CI job both exist and are wired; job runs without manual steps |

### Anti-Patterns Found

Scanned: `tests/integration/test_alembic_full_chain.py`, `Makefile`, `.github/workflows/ci.yml`, `.gitignore`

| File | Line | Pattern | Severity | Impact |
|------|------|---------|----------|--------|
| `Makefile` | 63-65 | `-q -s ... -v --tb=short` contradictory verbosity flags | Info (IN-01 from REVIEW, deferred) | Cosmetic; `-q` and `-v` cancel to default verbosity; does not affect correctness |
| `.gitignore` | 56 | `!tasks.json  # Keep...` trailing inline comment parsed as part of pattern | Info (IN-02 from REVIEW, deferred) | Pre-existing; not introduced by this phase; negation is functionally inert but harmless |

No `TBD`, `FIXME`, or `XXX` markers found in any phase-modified file.

### Human Verification Required

### 1. CI Job on ubuntu-latest

**Test:** Push the current branch and observe the `migration-chain` CI job in the GitHub Actions run
**Expected:** Job runs green on ubuntu-latest; a `migration-chain-evidence` artifact is uploaded containing all three pass flags true; the `Assert evidence was produced` step (line 334) passes confirming tests were not skipped
**Why human:** Requires a live push to a GitHub remote and a running Actions runner; cannot be verified programmatically from the local workstation

### Gaps Summary

No gaps. All 8 must-have truths are VERIFIED against the actual codebase. Both MIG-01 and MIG-02 requirements are satisfied. The only outstanding item is the CI job on ubuntu-latest, which the environment notes explicitly flag as a legitimate human/deferred item ("CI job green on ubuntu-latest can only be confirmed after push"). All REVIEW findings (CR-01 through WR-07 that were marked fixed) have been confirmed fixed in the current code:

- **CR-01** (fabricated evidence flags): Fixed — `_RESULTS` dict with `.get(..., False)` defaults; each flag set only after its test's assertions pass.
- **WR-01** (subset-only on-disk guard): Fixed — `assert on_disk == set(EXPECTED_CHAIN)` with set-equality.
- **WR-02** (hand-curated post-downgrade list): Fixed — `assert post_downgrade_tables == []` with a query for all public tables except `alembic_version`.
- **WR-03** (missing upgrade-side assertions for 018/020-028): Fixed — `expected_auth_tables` block at lines 475-503; `users_policy_column_count` at lines 507-520; `tags_column_count` at lines 522-533.
- **WR-04** (cwd-relative evidence path): Fixed — `EVIDENCE_PATH = REPO_ROOT / "migration-chain-evidence.json"` anchored at file level.
- **WR-05** (silent-pass when Docker absent): Fixed — `test -f migration-chain-evidence.json` hard gate at CI line 334.
- **WR-06** (deprecated `datetime.utcnow()`): Fixed — `datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")`.
- **WR-07** (stale docstrings): Fixed — module docstring updated to reference `EXPECTED_CHAIN` and `028_webauthn_user_handles`.

---

_Verified: 2026-06-11T10:05:00Z_
_Verifier: Claude (gsd-verifier)_


## Human Verification Result (2026-06-12)

CI item validated: run 27416536290 — migration-chain job green on ubuntu-latest, evidence
artifact uploaded. See 18-HUMAN-UAT.md (status: complete).
