---
phase: 29-spec-drift-guard
type: context
requirements: [DRIFT-01, DRIFT-02, DRIFT-03]
source: orchestrator-authored (autonomous run — milestone start)
---

# Phase 29 Context — Spec Drift-Guard

## Goal

Operationalize the v1.3 OpenSpec baseline with an automated CI gate so that
spec↔code drift (the v3→v4 class that v1.3 had to fix by hand) can never silently re-accumulate.
Every PR/push must prove the 32 capability specs still validate `--strict` and the
`module → capability` coverage matrix is still complete (live module count == matrix rows, zero
unmapped, zero double-mapped, slugs ⊆ taxonomy, every capability ≥1 module).

## Current state (verified at phase start)

- **32/32** capability specs exist (`openspec/specs/*/spec.md`) and pass
  `openspec validate --all --strict` (32 passed, 0 failed)
- Coverage matrix: **275 rows = live `find whilly/ -name "*.py" -not -path "*/__pycache__/*" | wc -l`** (no drift; 0 UNMAPPED, all 32 capabilities covered)
- Existing CI infrastructure in `.github/workflows/ci.yml` with lint/test jobs
- `openspec` is a Node CLI that can be installed in CI
- Makefile with existing patterns like `migrate-chain`

## 3 requirements to satisfy

| Req | What | How |
|-----|------|-----|
| DRIFT-01 | CI job validates all OpenSpec capability specs | Add a job to `.github/workflows/ci.yml` that runs `openspec validate --all --strict` on every pull_request and push, and fails the build if any spec is invalid (non-zero exit / any "failed"). |
| DRIFT-02 | Committed, executable coverage-matrix audit | Create a committed, executable script that checks: live module count == matrix body-row count; zero `UNMAPPED`; zero double-mapped module paths; every capability slug used is one of the taxonomy slugs; every taxonomy capability has ≥1 module row. Integrate this check into the CI job. |
| DRIFT-03 | Local entry point reproduces the CI gate | Add a local entry point (e.g. `make spec-check`) that reproduces the CI gate and document it in CLAUDE.md/docs, so contributors can run the same checks before pushing. |

## Implementation approach

The implementation should follow existing patterns in the codebase:
- CI jobs in `.github/workflows/ci.yml`
- Makefile targets like `migrate-chain`
- Executable scripts in `scripts/` directory
- Documentation in `CLAUDE.md`

## Constraints

- Gate runs in the existing GitHub Actions CI alongside lint/test/etc.
- `openspec` is a Node CLI → the job installs Node + the openspec CLI before validating.
- The coverage-matrix audit is a committed, testable script (no inline shell-only logic) so it
  runs identically in CI and locally.
- A local entry point mirrors CI (Makefile target), matching the existing `migrate-chain` pattern.
- This milestone MAY change `whilly/`-adjacent infra (CI, Makefile, a `scripts/` checker) — it is
  NOT spec-capture-only. It must not change `whilly/` runtime behavior.

## Out of scope

| Item | Reason |
|------|--------|
| Changing any `whilly/` runtime behavior | This milestone is CI/tooling only. Behavior changes go through `opsx` proposals. |
| Rewriting or re-reviewing the 32 capability specs | v1.3 shipped them validated; v1.4 only guards them. |
| Auto-fixing drift | The gate fails loudly; fixing drift is a normal `opsx`/spec-update task, not automated here. |

## Success criteria (ROADMAP Phase 29)

1. CI job validates all OpenSpec capability specs on every pull_request and push — DRIFT-01.
2. Committed, executable coverage-matrix audit integrated into CI — DRIFT-02.
3. Local entry point reproduces the CI gate and is documented — DRIFT-03.

## Spec/doc format

This is an implementation phase, not a spec-writing phase. The deliverables are:
- Updated CI configuration
- Executable audit script
- Makefile target
- Updated documentation