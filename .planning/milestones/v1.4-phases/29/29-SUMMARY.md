# Phase 29 Summary — Spec Drift-Guard

## Status

✅ Phase COMPLETE

## Overview

Completed milestone v1.4 "Spec Drift-Guard CI" Phase 29 "Spec Drift-Guard". This phase operationalizes the v1.3 OpenSpec baseline with automated CI gates to prevent spec↔code drift from silently accumulating.

## Requirements Addressed

### DRIFT-01: CI job validates all OpenSpec capability specs
- **Status**: ✅ Completed (29-01-PLAN.md)
- **Description**: A CI job validates all OpenSpec capability specs — runs `openspec validate --all --strict` on every pull_request and push, and fails the build if any spec is invalid (non-zero exit / any "failed").

### DRIFT-02: Committed, executable coverage-matrix audit
- **Status**: ✅ Completed (29-01-PLAN.md)
- **Description**: A committed, executable coverage-matrix audit checks, and the CI job enforces: live module count (`find whilly/ -name "*.py" -not -path "*/__pycache__/*" | wc -l`) == matrix body-row count; zero `UNMAPPED`; zero double-mapped module paths; every capability slug used is one of the taxonomy slugs; every taxonomy capability has ≥1 module row. Any drift fails the build.

### DRIFT-03: Local entry point reproduces the CI gate
- **Status**: ✅ Completed (29-02-PLAN.md)
- **Description**: A local entry point reproduces the CI gate (e.g. `make spec-check`) and is documented (CLAUDE.md / docs), so contributors can run the same checks before pushing.

## Plans Executed

1. **29-01-PLAN.md**: Implemented CI jobs for spec validation and coverage matrix audit (DRIFT-01, DRIFT-02)
2. **29-02-PLAN.md**: Added local entry point and documentation (DRIFT-03)

## Deliverables

### New Files Created
- `scripts/audit-coverage-matrix.py` - Executable coverage matrix audit script
- `.planning/phases/29/CONTEXT.md` - Phase context and requirements
- `.planning/phases/29/29-01-PLAN.md` - Plan for CI jobs and coverage audit
- `.planning/phases/29/29-01-SUMMARY.md` - Summary of plan execution
- `.planning/phases/29/29-02-PLAN.md` - Plan for local entry point and documentation
- `.planning/phases/29/29-02-SUMMARY.md` - Summary of plan execution
- `.planning/phases/29/29-SUMMARY.md` - This summary file

### Modified Files
- `.github/workflows/ci.yml` - Added `spec-validation` and `coverage-audit` jobs
- `Makefile` - Added `spec-check` target
- `CLAUDE.md` - Added documentation for `make spec-check` command

## Verification Results

All requirements have been successfully implemented and verified:

- ✅ CI job validates all OpenSpec capability specs on every pull_request and push
- ✅ Committed, executable coverage-matrix audit integrated into CI
- ✅ Local entry point reproduces the CI gate and is documented

The implementation successfully prevents spec↔code drift from silently accumulating by enforcing validation on every code change.