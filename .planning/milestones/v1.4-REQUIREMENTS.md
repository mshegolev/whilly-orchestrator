# Requirements: Whilly Orchestrator — v1.4 Spec Drift-Guard CI

**Defined:** 2026-06-16
**Core Value:** Operators can safely coordinate AI-assisted engineering work with auditable state,
human control, and verification before claiming success.

**Milestone goal:** Operationalize the v1.3 OpenSpec baseline with an automated CI gate so that
spec↔code drift (the v3→v4 class that v1.3 had to fix by hand) can never silently re-accumulate.
Every PR/push must prove the 32 capability specs still validate `--strict` and the
`module → capability` coverage matrix is still complete (live module count == matrix rows, zero
unmapped, zero double-mapped, slugs ⊆ taxonomy, every capability ≥1 module).

**Decisions locked (2026-06-16):**
- Gate runs in the existing GitHub Actions CI (`.github/workflows/ci.yml`) alongside lint/test/etc.
- `openspec` is a Node CLI → the job installs Node + the openspec CLI before validating.
- The coverage-matrix audit is a committed, testable script (no inline shell-only logic) so it
  runs identically in CI and locally.
- A local entry point mirrors CI (Makefile target), matching the existing `migrate-chain` pattern.
- This milestone MAY change `whilly/`-adjacent infra (CI, Makefile, a `scripts/` checker) — it is
  NOT spec-capture-only. It must not change `whilly/` runtime behavior.

## v1.4 Requirements

### Spec Drift-Guard (Phase 29)

- [ ] **DRIFT-01**: A CI job validates all OpenSpec capability specs — runs
  `openspec validate --all --strict` on every pull_request and push, and fails the build if any
  spec is invalid (non-zero exit / any "failed").
- [ ] **DRIFT-02**: A committed, executable coverage-matrix audit checks, and the CI job enforces:
  live module count (`find whilly/ -name "*.py" -not -path "*/__pycache__/*" | wc -l`) == matrix
  body-row count; zero `UNMAPPED`; zero double-mapped module paths; every capability slug used is
  one of the taxonomy slugs; every taxonomy capability has ≥1 module row. Any drift fails the build.
- [ ] **DRIFT-03**: A local entry point reproduces the CI gate (e.g. `make spec-check`) and is
  documented (CLAUDE.md / docs), so contributors can run the same checks before pushing.

## Out of Scope

| Item | Reason |
|------|--------|
| Changing any `whilly/` runtime behavior | This milestone is CI/tooling only. Behavior changes go through `opsx` proposals. |
| Rewriting or re-reviewing the 32 capability specs | v1.3 shipped them validated; v1.4 only guards them. |
| Auto-fixing drift | The gate fails loudly; fixing drift is a normal `opsx`/spec-update task, not automated here. |

## Traceability

| Requirement | Phase | Status |
|-------------|-------|--------|
| DRIFT-01..03 | Phase 29 | Pending |
