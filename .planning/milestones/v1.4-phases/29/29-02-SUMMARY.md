# Plan 29-02 Summary — Local Entry Point and Documentation

## Status

✅ Completed

## Overview

Added a local entry point that reproduces the CI gate and documented it so contributors can run the same checks before pushing, satisfying requirement DRIFT-03.

## Tasks Completed

### Task 1: Add spec-check target to Makefile (DRIFT-03)

Updated `Makefile` to add a `spec-check` target that reproduces the CI gate:

1. ✅ Added `spec-check` target that runs `openspec validate --all --strict`
2. ✅ Added `spec-check` target that runs `scripts/audit-coverage-matrix.py`
3. ✅ Added Node.js and openspec CLI installation steps
4. ✅ Followed existing Makefile patterns and formatting
5. ✅ Added `spec-check` to `.PHONY` list

### Task 2: Document local entry point in CLAUDE.md (DRIFT-03)

Updated `CLAUDE.md` to document the local entry point:

1. ✅ Added `make spec-check` to the "Common commands" section
2. ✅ Included explanatory comment that it reproduces the CI gate locally
3. ✅ Maintained existing CLAUDE.md style and structure

## Verification

- ✅ `Makefile` contains `spec-check` target that reproduces CI gate
- ✅ `spec-check` target runs both `openspec validate --all --strict` and `scripts/audit-coverage-matrix.py`
- ✅ `CLAUDE.md` documents the `make spec-check` command
- ✅ Documentation explains that it reproduces the CI gate locally
- ✅ Documentation mentions both spec validation and coverage audit
- ✅ Documentation advises running before pushing to avoid CI failures
- ✅ Documentation follows existing CLAUDE.md style and structure

## Success Criteria

- ✅ DRIFT-03: Local entry point reproduces the CI gate and is documented

## Phase Completion

All requirements for Phase 29 have been satisfied:
- ✅ DRIFT-01: CI job validates all OpenSpec capability specs on every pull_request and push
- ✅ DRIFT-02: Committed, executable coverage-matrix audit integrated into CI
- ✅ DRIFT-03: Local entry point reproduces the CI gate and is documented