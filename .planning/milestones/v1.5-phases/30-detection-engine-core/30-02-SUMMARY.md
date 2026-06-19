---
phase: 30-detection-engine-core
plan: 02
subsystem: semantic-drift-engine
tags: [tooling, drift-detection, tdd, scripts, cli]
requires:
  - "scripts/semantic_drift_check.py: resolve_modules_for_slug, build_review_prompt, parse_findings (Plan 01)"
provides:
  - "scripts/semantic_drift_check.py: review_spec(slug, reviewer) single-spec pipeline"
  - "scripts/semantic_drift_check.py: claude_reviewer default CLI reviewer (CLAUDE_BIN, -p --output-format json, envelope unwrap)"
  - "scripts/semantic_drift_check.py: main(--slug) CLI entry, always exit 0"
affects: []
tech-stack:
  added: []
  patterns:
    - "dependency-injected reviewer Callable[[str], str] (offline-testable pipeline)"
    - "thin subprocess.run Claude CLI shell (no retry stack, per CONTEXT)"
    - "Claude --output-format json envelope unwrap to inner `result` (fallback to raw stdout)"
    - "live-CLI test skips via shutil.which('claude') is None (repo convention)"
key-files:
  created: []
  modified:
    - scripts/semantic_drift_check.py
    - tests/test_semantic_drift_check.py
decisions:
  - "Bad/missing slug returns [] (logged to stderr) and never calls the reviewer; main() always exits 0 — a bad slug never yields a non-zero exit (plan-checker nudge pinned by test)"
  - "All filesystem I/O (spec.md + module reads) lives in review_spec so build_review_prompt stays pure"
  - "A mapped module absent on disk is non-fatal: recorded as '(source file unreadable / not found)' so the path still appears in the prompt"
  - "claude_reviewer unwraps {result: ...}; falls back to raw stdout for a bare array envelope shape"
requirements: [DETECT-01]
metrics:
  duration: ~15m
  completed: 2026-06-19
---

# Phase 30 Plan 02: Detection Engine Core (Pipeline + CLI) Summary

Wired the Plan 01 model-free core into an end-to-end single-spec review and a `--slug` CLI entry (DETECT-01): `review_spec` loads spec.md + mapped module sources, builds the prompt, calls a dependency-injected reviewer, and parses findings; a thin `claude_reviewer` shells to the Claude CLI; `main(--slug)` prints findings JSON and always exits 0.

## What shipped

- `scripts/semantic_drift_check.py` (appended, no Plan 01 rewrite):
  - `review_spec(slug, reviewer, *, specs_root="openspec/specs", repo_root=".", matrix_path=...)` — the DETECT-01 pipeline. Reads `<specs_root>/<slug>/spec.md`, resolves modules via `resolve_modules_for_slug`, reads each module source (missing → recorded `(source file unreadable / not found)`, skipped not fatal), calls `build_review_prompt`, hands the prompt to the injected `reviewer`, returns `parse_findings(raw)`. A missing/invalid slug (no spec.md) logs to stderr and returns `[]` WITHOUT calling the reviewer — never raises. Reviewer typed `Callable[[str], str]`.
  - `claude_reviewer(prompt) -> str` — default reviewer. Resolves binary via `CLAUDE_BIN` (default `claude`), runs `claude --model <m> --disallowedTools Write,Edit,MultiEdit,NotebookEdit,Bash -p <prompt> --output-format json` (timeout via `WHILLY_CLAUDE_TIMEOUT`), unwraps the `{"result": ...}` envelope to its inner string, falls back to raw stdout for unexpected shapes. Kept thin (no retry stack, per CONTEXT).
  - `main(argv=None, *, reviewer=claude_reviewer) -> int` — argparse `--slug` (plus `--specs-root`, `--repo-root`, `--matrix-path`), runs `review_spec`, prints `json.dumps(findings, indent=2)`, returns `0` ALWAYS. `reviewer` is injectable so tests drive the full CLI path offline. Guarded `if __name__ == "__main__": sys.exit(main())`.
- `tests/test_semantic_drift_check.py` — appended 12 tests: review_spec valid/clean/junk/missing-slug/missing-module; main prints-JSON / clean-exit-0 / **bad-slug-exit-0-with-[]** (plan-checker nudge); claude_reviewer argv shape (`--output-format json`, `-p`, `CLAUDE_BIN`) + envelope unwrap + raw-stdout fallback; and a live-CLI test that skips when `shutil.which("claude") is None`.

## Plan-checker nudge resolution

A missing/invalid `--slug` does NOT raise. `review_spec` short-circuits to `[]` (stderr diagnostic) before touching the reviewer, and `main` unconditionally `return 0`. Pinned by `test_main_bad_slug_exits_zero_with_empty_array` (bad slug → rc==0, `[]` on stdout).

## TDD gates

- RED: `test(30-02)` commit `bacc31d` — 11 new tests fail (review_spec/main/claude_reviewer absent).
- GREEN: `feat(30-02)` commit `62bd3e8` — implementation makes all pass.
- REFACTOR: not needed (clean on first pass).

## Verification

```
$ python3 -m pytest tests/test_semantic_drift_check.py -q
............s..................                                          [100%]
30 passed, 1 skipped in 112.45s (0:01:52)

$ python3 -m ruff check scripts/ tests/
All checks passed!
```

Notes:
- The full run is slow (112s) because `claude` IS on PATH in this environment, so `test_live_cli_reviewer_runs_against_real_claude` actually invoked the live CLI and passed (asserts a `list` is returned). Offline-only subset (live test deselected) runs in 0.04s: `29 passed, 1 skipped, 1 deselected`.
- The 1 skip is the Plan 01 `test_parse_findings_json_repair_recovers_trailing_comma` — optional `json_repair` absent from this `python3` (3.10); `importorskip` per repo convention. Unchanged by this plan.
- `git diff --name-only -- whilly/` is empty — zero `whilly/` behavior change, no new coverage-matrix/opsx obligation (per CONTEXT location decision).

## Deviations from Plan

None — plan executed exactly as written. Both task commits passed the repo pre-commit hook (ruff check + format) with no reformatting required.

## Self-Check: PASSED

- FOUND: scripts/semantic_drift_check.py (review_spec, claude_reviewer, main present)
- FOUND: tests/test_semantic_drift_check.py (fake_reviewer pipeline + live-skip tests)
- FOUND commit bacc31d (RED), 62bd3e8 (GREEN)
- Confirmed: no file under `whilly/` modified
