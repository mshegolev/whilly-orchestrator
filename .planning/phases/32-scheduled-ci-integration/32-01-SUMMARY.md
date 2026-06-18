---
phase: 32-scheduled-ci-integration
plan: 01
subsystem: ci-semantic-drift
tags: [ci, semantic-drift, github-actions, gating]
requires:
  - scripts/semantic_drift_check.py (Phase 31 run_fleet/build_artifact/format_summary)
provides:
  - "--fail-on {none,high} severity gate on semantic_drift_check.py --all"
  - "scheduled-only semantic-drift.yml CI workflow with artifact + step-summary"
affects:
  - .github/workflows (new standalone scheduled workflow; ci.yml untouched)
tech-stack:
  added: []
  patterns:
    - "argparse choices allowlist for gating posture"
    - "GitHub Actions posture via quoted env var (no inline interpolation)"
key-files:
  created:
    - .github/workflows/semantic-drift.yml
    - tests/test_semantic_drift_workflow.py
  modified:
    - scripts/semantic_drift_check.py
    - tests/test_semantic_drift_check.py
    - docs/Whilly-Usage.md
decisions:
  - "Gating lives in the script (--fail-on); workflow maps posture input to it via env"
  - "Cron + workflow_dispatch only; no PR/push so the per-PR mechanical gate is untouched"
  - "Errors never gate — only HIGH findings under --fail-on high red the job"
metrics:
  duration: ~25m
  completed: 2026-06-19
---

# Phase 32 Plan 01: Scheduled CI Integration Summary

Wired the Phase 31 fleet runner into a scheduled-only semantic-drift CI job with an
operator-selectable HIGH-severity gate, leaving the v1.4 per-PR mechanical gate byte-identical.

## What was built

- **CI-02 (Task 1, TDD):** Added `--fail-on {none,high}` (argparse `choices`, default `none`)
  to `main()` in `scripts/semantic_drift_check.py`. The gate is inserted in the `--all` branch
  AFTER the artifact write + summary print, BEFORE the return: `--fail-on high` returns 1 iff
  any finding has severity `SEVERITIES[0]` ("HIGH"); per-unit `errors` never gate; `--fail-on none`
  (and the `--slug` path) always returns 0. 7 new offline tests pin every case.
- **CI-01 (Task 2):** Created `.github/workflows/semantic-drift.yml` — triggered ONLY by a weekly
  `schedule` (Mon 06:00 UTC) + `workflow_dispatch` (no `pull_request`/`push`). Installs the Claude
  CLI (`npm install -g @anthropic-ai/claude-code`), fails fast (exit 1, no value echoed) if the
  `ANTHROPIC_API_KEY` secret is empty, maps an allowlisted `posture` input (`report-only|fail-on-high`)
  to `--fail-on` via a quoted `POSTURE` env var → `GITHUB_ENV` `FAIL_ON` (never inline interpolation),
  runs `--all`, tees the summary into `$GITHUB_STEP_SUMMARY` under `set -o pipefail` (exit code
  preserved), and uploads the JSON artifact via `actions/upload-artifact@v4` with `if: always()`.
  Added `tests/test_semantic_drift_workflow.py` (9 structural assertions via `yaml.safe_load`, reading
  triggers under the PyYAML `True` key, asserting no `pull_request`/`push`, no inline
  `github.event.inputs.posture` in any `run:` line, and a `POSTURE` env binding). Documented the
  required secret + posture mapping in `docs/Whilly-Usage.md`.

## Deviations from Plan

None — plan executed exactly as written.

## Threat mitigations applied

- **T-32-01** (posture injection): allowlist via argparse `choices` + quoted `POSTURE` env mapped
  through a `case` to `FAIL_ON`; structural test asserts no inline interpolation in any run line.
- **T-32-02 / T-32-04** (key disclosure / silent pass): key injected only via step `env:` from
  `secrets.ANTHROPIC_API_KEY`, never echoed; dedicated fail-fast step exits 1 on empty key.

## Verification (real output)

```
=== pytest ===
61 passed, 1 skipped, 6 deselected in 0.37s
=== ruff ===
All checks passed!
=== yaml triggers ===
triggers: ['schedule', 'workflow_dispatch']
=== LOCKED ===
whilly/ diff: (empty)
ci.yml: CI_YML_BYTE_IDENTICAL_OK
```

## Commits

- a822085 test(32-01): add failing --fail-on {none,high} gating tests (RED)
- 06cdb5d feat(32-01): add --fail-on {none,high} severity gating to --all (CI-02) (GREEN)
- 2d18c3c feat(32-01): add scheduled semantic-drift workflow + structural test + docs (CI-01)

## Self-Check: PASSED
- scripts/semantic_drift_check.py — FOUND
- .github/workflows/semantic-drift.yml — FOUND
- tests/test_semantic_drift_workflow.py — FOUND
- commits a822085, 06cdb5d, 2d18c3c — FOUND
