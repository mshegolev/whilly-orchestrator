---
phase: 18-migration-chain-validation
plan: "02"
subsystem: ci/makefile
tags: [makefile, github-actions, migration-chain, mig-02, ci]
dependency_graph:
  requires: [18-01]
  provides: [migrate-chain-make-target, migration-chain-ci-job]
  affects: [Makefile, .github/workflows/ci.yml]
tech_stack:
  added: []
  patterns: [make-help-docstring-convention, needs-lint-ci-pattern, upload-artifact-v4]
key_files:
  created: []
  modified:
    - Makefile
    - .github/workflows/ci.yml
decisions:
  - "migrate-chain target uses -s flag so Docker skip reason is visible to operator"
  - "Single-process pytest invocation (no -n auto) because the chain test owns its own ephemeral container"
  - "CI job mirrors all existing post-lint jobs: needs:lint + ref:head_ref||ref_name checkout pattern"
metrics:
  duration: "9 min"
  completed: "2026-06-11"
  tasks_completed: 2
  files_modified: 2
---

# Phase 18 Plan 02: Makefile Target and CI Job for Migration Chain Validation Summary

**One-liner:** Added `make migrate-chain` as the canonical local entry point and a `migration-chain` CI job that runs the same target and uploads evidence on every pipeline run.

## Tasks Completed

| Task | Name | Commit | Files |
|------|------|--------|-------|
| 1 | Add the migrate-chain Makefile target | c77e8f6 | Makefile |
| 2 | Add the migration-chain CI job with artifact upload | 8b4c7eb | .github/workflows/ci.yml |

## What Was Built

- **`make migrate-chain`** — new `.PHONY` Makefile target added after the `test:` block. Invokes `$(PYTHON) -m pytest -q -s tests/integration/test_alembic_full_chain.py -v --tb=short`. Uses `-s` so the Docker skip reason is visible when Docker is absent. No `-n auto` / `--maxprocesses` flags — single-process run as the chain test owns its own container. Carries a `## Run full Alembic migration chain validation (requires Docker)` docstring so `make help` auto-lists it via the existing awk scraper.
- **`migration-chain` CI job** — appended at the end of `jobs:` in `.github/workflows/ci.yml`. Mirrors the exact structure of all existing post-lint jobs: `runs-on: ubuntu-latest`, `needs: lint` (picks up the lint auto-fix commit per RESEARCH Pitfall 4), `actions/checkout@v4` with `ref: ${{ github.head_ref || github.ref_name }}`, `actions/setup-python@v5` pinned to `python-version: "3.12"`, `pip install -e '.[dev]'`, then `make migrate-chain`. Final step is `actions/upload-artifact@v4` with `if: always()`, artifact name `migration-chain-evidence`, path `migration-chain-evidence.json`, `if-no-files-found: ignore`, `retention-days: 30`. No `secrets.*` references.

## Verification

- `make help | grep migrate-chain` outputs: `migrate-chain     Run full Alembic migration chain validation (requires Docker)`
- `python -c "import yaml; yaml.safe_load(...)"` confirms ci.yml parses as valid YAML
- Automated acceptance criterion from plan passes for both tasks
- Live CI proof requires pushing branch and observing `migration-chain` job run green on ubuntu-latest with uploaded evidence artifact

## Deviations from Plan

None — plan executed exactly as written.

## Threat Surface

No new threat surface beyond what is in the plan's threat model:
- T-18-04 (mitigated): job references no `secrets.*`; testcontainers generates ephemeral DB credentials
- T-18-05 (mitigated): evidence artifact is the JSON written by Plan 01 — only revision string, count, booleans; no DSN
- T-18-06 (mitigated): `needs: lint` + `ref: ${{ github.head_ref || github.ref_name }}` ensures job runs against lint auto-fix commit
- Security plugin flagged `github.head_ref` in `ref:` parameter — this is the pre-existing pattern used by all 4 other non-lint jobs in this workflow (arch-guard, type-check, test, agent-backends). The risk is bounded: an attacker controlling a fork branch can only cause checkout of their own code, which they already have access to as a PR contributor. No shell injection vector exists since the value is used only as a git ref, not in a `run:` command.

## Known Stubs

None.

## Self-Check: PASSED

- Makefile (migrate-chain: target): FOUND
- Makefile (.PHONY migrate-chain): FOUND
- .github/workflows/ci.yml (migration-chain job): FOUND
- c77e8f6: FOUND
- 8b4c7eb: FOUND
