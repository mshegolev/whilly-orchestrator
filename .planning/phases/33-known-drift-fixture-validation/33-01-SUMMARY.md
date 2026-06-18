---
phase: 33-known-drift-fixture-validation
plan: 01
subsystem: semantic-drift-validation
tags: [validation, fixtures, tdd, canary, VALID-01]
requires:
  - "scripts/semantic_drift_check.py::review_spec (existing injectable seam)"
  - "scripts/semantic_drift_check.py::claude_reviewer (existing live reviewer)"
provides:
  - "tests/fixtures/semantic_drift/{drifted,clean}/ self-contained drift fixtures"
  - "tests/test_semantic_drift_fixture_validation.py two-layer validation"
  - "count_high() severity classifier (test-module helper)"
affects: []
tech-stack:
  added: []
  patterns:
    - "Two-layer validation: deterministic offline plumbing + skipif-guarded live canary"
    - "Severity-level-only assertions for non-deterministic LLM outputs"
    - "Self-contained fixtures pointed at via review_spec specs_root/repo_root/matrix_path"
key-files:
  created:
    - tests/fixtures/semantic_drift/drifted/spec.md
    - tests/fixtures/semantic_drift/drifted/module.py
    - tests/fixtures/semantic_drift/drifted/matrix.md
    - tests/fixtures/semantic_drift/clean/spec.md
    - tests/fixtures/semantic_drift/clean/module.py
    - tests/fixtures/semantic_drift/clean/matrix.md
    - tests/fixtures/semantic_drift/README.md
    - tests/test_semantic_drift_fixture_validation.py
  modified: []
decisions:
  - "Live canary + offline plumbing share one test module (reuse count_high + _FIXTURES)"
  - "count_high() lives in the test module — zero new scripts/ production surface (LOCKED)"
  - "Live canary asserts severity-level outcomes only — never LLM wording/counts"
metrics:
  duration: "~12m (incl. 3m43s live canary + 1m51s engine-suite regression)"
  completed: "2026-06-19"
  tasks: 3
  files: 8
requirements: [VALID-01]
---

# Phase 33 Plan 01: Known-Drift Fixture Validation Summary

Two-layer validation proving the semantic drift-detection engine is trustworthy:
a deliberately drifted spec/code fixture (spec SHALL return a JSON object; module
returns a bare string) plus a clean control, validated offline by a deterministic
plumbing test and live by the real `claude_reviewer` — which flagged the planted
drift HIGH and cleared the control with zero false positives.

## What was built

- **Self-contained drifted fixture** (`tests/fixtures/semantic_drift/drifted/`):
  `spec.md` with a concrete SHALL ("`summarize` SHALL return a JSON object … SHALL
  NOT return a bare string"), `module.py` that plainly violates it
  (`return f"{field}={value}"` on its own labeled line), and `matrix.md` mapping
  `module.py -> "drifted"`.
- **Self-contained clean control** (`clean/`): same SHALL, `module.py` returns a
  `dict` (matches exactly), matrix maps `module.py -> "clean"`.
- **Two-layer test module** (`tests/test_semantic_drift_fixture_validation.py`):
  - `count_high()` shared classifier on `sdc.SEVERITIES[0]`.
  - `test_plumbing_detects_high_on_drifted_fixture` — scripted HIGH reviewer →
    `count_high == 1`; asserts the prompt embedded the spec SHALL + module path
    (proves the real `review_spec` pipeline ran, not an assertion shortcut).
  - `test_plumbing_reports_clean_on_control_fixture` — scripted `[]` → `count_high == 0`.
  - `test_live_real_claude_flags_drift_and_clears_control` — `@pytest.mark.skipif(shutil.which("claude") is None)`, real `claude_reviewer`, asserts `count_high(drifted) >= 1 AND count_high(clean) == 0` (severity-level only).
- **Fixture README**: planted contradiction, clean rationale, expected-verdict
  table (drifted→HIGH, clean→clean), reproduction steps + `review_spec` params.

## Verification results

- Offline plumbing gate: `pytest ... -k "not live"` → **2 passed, 1 deselected**.
- `ruff check tests/` → **All checks passed!**
- Live canary (real model): **1 passed** in 3m43s — confirmed `>=1` HIGH on
  drifted, `0` HIGH on clean. Genuine trustworthiness proof for VALID-01.
- Engine-suite regression `tests/test_semantic_drift_check.py` → **58 passed, 1
  skipped** (no regression; existing live test ran since claude is on PATH).
- LOCKED scope: `git diff --name-only -- whilly/ scripts/` across all phase
  commits → **EMPTY**. Zero `whilly/` change, zero `scripts/` production change.

## Deviations from Plan

None — plan executed exactly as written. The live canary was implemented in the
same test module as the plumbing tests (per Task 3's explicit allowance to reuse
`count_high` and `_FIXTURES`), so it was committed alongside Task 2's tests; only
the README remained for the Task 3 commit.

## Notes

- `shutil.which("claude")` resolves a real binary on this host, so the live canary
  actually ran rather than skipping — output captured above.
- No package installs; no dependency change. tests/fixtures/docs only.

## Self-Check: PASSED
