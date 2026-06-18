# Plan 29-01 Summary — CI Jobs and Coverage Audit Script

## Status

✅ Completed

## Overview

Implemented CI jobs to validate OpenSpec capability specs and audit the coverage matrix on every pull_request and push, satisfying requirements DRIFT-01 and DRIFT-02.

## Tasks Completed

### Task 1: Create executable coverage-matrix audit script (DRIFT-02)

Created `scripts/audit-coverage-matrix.py` as an executable Python script that performs the coverage matrix audit:

1. ✅ Counts live whilly/ modules: `find whilly/ -name "*.py" -not -path "*/__pycache__/*" | wc -l`
2. ✅ Parses `openspec/COVERAGE-MATRIX.md` to count body rows
3. ✅ Verifies the counts match
4. ✅ Checks for zero "UNMAPPED" entries
5. ✅ Checks for zero double-mapped module paths
6. ✅ Verifies every capability slug used is one of the taxonomy slugs
7. ✅ Verifies every taxonomy capability has ≥1 module row

The script exits with code 0 if all checks pass, or non-zero if any check fails.

### Task 2: Update CI workflow with spec validation and coverage audit jobs (DRIFT-01, DRIFT-02)

Updated `.github/workflows/ci.yml` to add two new jobs that run on pull_request and push events:

1. ✅ `spec-validation` job:
   - Runs on ubuntu-latest
   - Installs Node.js
   - Installs openspec CLI
   - Runs `openspec validate --all --strict`
   - Fails the build if any spec is invalid (non-zero exit / any "failed")

2. ✅ `coverage-audit` job:
   - Runs on ubuntu-latest
   - Runs the `scripts/audit-coverage-matrix.py` script
   - Fails the build if any drift is detected

Both jobs run in parallel with existing jobs and use the same event triggers (pull_request, push).

## Verification

- ✅ `scripts/audit-coverage-matrix.py` exists, is executable, and performs all required coverage matrix audits
- ✅ `.github/workflows/ci.yml` contains `spec-validation` job that runs on every pull_request and push
- ✅ `spec-validation` job installs Node.js, openspec CLI, and runs `openspec validate --all --strict`
- ✅ `spec-validation` job fails the build if any spec is invalid
- ✅ `.github/workflows/ci.yml` contains `coverage-audit` job that runs on every pull_request and push
- ✅ `coverage-audit` job runs `scripts/audit-coverage-matrix.py`
- ✅ `coverage-audit` job fails the build if any drift is detected
- ✅ Both jobs run in parallel with existing jobs
- ✅ No whilly/ Python runtime behavior changed

## Success Criteria

- ✅ DRIFT-01: CI job validates all OpenSpec capability specs on every pull_request and push
- ✅ DRIFT-02: Committed, executable coverage-matrix audit integrated into CI

## Next Steps

Proceed to execute Plan 29-02 to add the local entry point and documentation.