---
phase: 30-detection-engine-core
plan: 01
subsystem: semantic-drift-engine
tags: [tooling, drift-detection, tdd, scripts]
requires: []
provides:
  - "scripts/semantic_drift_check.py: resolve_modules_for_slug, build_review_prompt, parse_findings, validate_finding"
  - "shared schema constants FINDING_KEYS / SEVERITIES / TRIAGE_VALUES"
affects: []
tech-stack:
  added: []
  patterns:
    - "fence-strip + lazy json_repair fallback (mirrors whilly/prd_generator)"
    - "live coverage-matrix parse (ports scripts/audit-coverage-matrix.py table parser)"
    - "pure prompt builder (no I/O / subprocess) for offline testability"
key-files:
  created:
    - scripts/semantic_drift_check.py
    - tests/test_semantic_drift_check.py
  modified: []
decisions:
  - "Engine lives under scripts/ (not whilly/) so it carries no coverage-matrix/opsx obligation"
  - "Schema enums are module-level constants shared by prompt builder and validator (single source of truth)"
  - "parse_findings never raises (Phase 30 is report-only, exit 0); unrecoverable input -> []"
  - "json_repair is an optional lazy import inside _try_load; tests use importorskip"
requirements: [DETECT-02, DETECT-03, DETECT-04]
metrics:
  duration: ~20m
  completed: 2026-06-19
---

# Phase 30 Plan 01: Detection Engine Core Summary

Pure, model-free, fully-offline core of the single-spec semantic drift engine: live matrix-driven module resolution, a deterministic review-prompt builder, and robust findings parse/validate against a single shared 7-key schema.

## What shipped

- `scripts/semantic_drift_check.py` (249 lines, standalone tooling — nothing under `whilly/`):
  - `resolve_modules_for_slug(slug, matrix_path=...)` — derives a capability's reviewed module set live from `openspec/COVERAGE-MATRIX.md` by EXACT slug match, in matrix order, via the ported `_parse_matrix_rows` helper (same table-locate-and-split approach as `scripts/audit-coverage-matrix.py::parse_coverage_matrix`). Unknown slug → `[]`. Injectable `matrix_path` (DETECT-04).
  - `build_review_prompt(slug, spec_text, module_sources)` — PURE function (no `open()`, no subprocess); byte-identical output for identical inputs. Embeds slug, full spec text, and each `### FILE: <path>` + source; encodes the full 7-key schema, severity/triage enums, the `file:line` evidence requirement, and the clean-spec `[]` rule (DETECT-02/03).
  - `parse_findings(text)` — fence-strip → `json.loads` → first-`[...]`-array extraction → lazy `json_repair` fallback; never raises (returns `[]` on unrecoverable input). Filters through `validate_finding` so callers only see schema-valid findings.
  - `validate_finding(finding)` — enforces exactly `FINDING_KEYS`, `severity in SEVERITIES`, `triage in TRIAGE_VALUES`, and a non-empty `file:line`-shaped `evidence`.
  - `FINDING_KEYS`, `SEVERITIES`, `TRIAGE_VALUES` — single source of truth shared by prompt builder and validator.
- `tests/test_semantic_drift_check.py` — 20 offline unit tests (loads the script by file path since `scripts/` is not a package). No subprocess/httpx/network imports.

## TDD gates

- RED: `test(30-01)` commit `0457471` — tests fail (module absent).
- GREEN: `feat(30-01)` commit `2db59d1` — implementation makes all tests pass.
- REFACTOR: not needed (implementation clean on first pass).

## Verification

```
$ python3 -m pytest tests/test_semantic_drift_check.py -q
............s.......                                                     [100%]
19 passed, 1 skipped in 0.03s

$ python3 -m ruff check scripts/ tests/test_semantic_drift_check.py
All checks passed!
```

The 1 skip is `test_parse_findings_json_repair_recovers_trailing_comma` — `json_repair` is an optional dependency absent from the default `python3` (3.10) here, so the test `importorskip`s it per repo convention. The lazy import is confirmed inside `_try_load` (line 224), not at module top.

## Deviations from Plan

None — plan executed exactly as written. (Pre-commit hook required `ruff format`; both files were formatted to width 120 before each commit, which is a project convention, not a plan deviation.)

## Self-Check: PASSED

- FOUND: scripts/semantic_drift_check.py
- FOUND: tests/test_semantic_drift_check.py
- FOUND commit 0457471 (RED), 2db59d1 (GREEN)
- Confirmed: no file created under `whilly/` (`git diff --name-only HEAD~2 HEAD -- whilly/` empty)
