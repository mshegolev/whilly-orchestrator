---
phase: 27-safety-quality-cluster
plan: 02
subsystem: testing
tags: [openspec, quality-gates, compliance, audit, jsonl, verification, human-review, ci, reverse-spec]

# Dependency graph
requires:
  - phase: 21-spec-baseline-taxonomy
    provides: AUTHORING.md spec format + capability taxonomy + exemplar specs
  - phase: 27-safety-quality-cluster (27-01)
    provides: SAFE-01/SAFE-02 safety specs + grounding discipline pattern
provides:
  - SAFE-03 quality-compliance-audit normative spec (quality gates, compliance report, JSONL audit sink, qa-release)
  - SAFE-04 verification-gates normative spec (live pipeline + human-review + CI gates; legacy verify_task marked unwired)
affects: [phase-28-coverage-audit, openspec-validate-strict, adversarial-verifier]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "Subsystem-altitude reverse-spec: contract of the subsystem, not every helper"
    - "Truthful live/legacy annotation backed by grep-confirmed call-site evidence"

key-files:
  created:
    - openspec/specs/quality-compliance-audit/spec.md
    - openspec/specs/verification-gates/spec.md
  modified:
    - .planning/REQUIREMENTS.md
    - .planning/STATE.md

key-decisions:
  - "Spec the live v4 pipeline/CI verification gates as primary; mark verifier.verify_task legacy/unwired (no worker-path callers confirmed by grep)"
  - "Compliance capability framed as deterministic report generation with PASS/PARTIAL/FAIL/UNKNOWN and present-but-unwired = PARTIAL"
  - "JSONL audit sink specced as best-effort mirror of the Postgres events table (OSError swallowed, never raises)"

patterns-established:
  - "Required-vs-warning verification semantics gate the DONE transition (required_failed only)"
  - "Human-review gate: detect cue -> build checkpoint -> hold -> proceed only on matching approval"
metrics:
  duration: ~25m
  completed: 2026-06-16
  tasks: 2
  files: 4
---

# Phase 27 Plan 02: Safety & Quality Cluster (SAFE-03 + SAFE-04) Summary

Two subsystem-altitude OpenSpec capability specs reverse-spec'd from the real v4.7.0
code — quality/compliance/audit/qa-release (SAFE-03) and the live verification +
human-review gates (SAFE-04) — both passing `openspec validate <slug> --strict`
with 0 errors and 0 warnings. Documentation-only; zero `whilly/` changes.

## What was built

### Task 1 — SAFE-03 `quality-compliance-audit` (commit f75c684)
`openspec/specs/quality-compliance-audit/spec.md` with 5 requirements:
1. Per-language QualityGate Protocol — `detect(cwd)`/`run(cwd)` → `GateResult`,
   `passed` iff every `StageResult` passed, never raises on lint/test failure,
   missing binary, or timeout (grounded in `quality/base.py`, `_runner.py`,
   `python.py`).
2. Multi-language detection/aggregation — `detect_gates` + `run_all` →
   `gate_kind="multi"`, logical-AND pass, no-gates = `passed=True`
   (`quality/multi.py`, `__init__.py`).
3. Deterministic target-doc compliance report — `whilly compliance report`
   (`run_compliance_command` → `ComplianceReport`), PASS/PARTIAL/FAIL/UNKNOWN,
   present-but-unwired = PARTIAL (`compliance/__init__.py`, `cli/compliance.py`).
4. Append-only JSONL audit sink — `JsonlEventSink.record` one JSON object per line
   with `ts/event/event_type/task_id/plan_id/payload`, `OSError` swallowed
   (`audit/jsonl_sink.py`).
5. QA-release artifact generation — `whilly qa-release` collect/plan/scaffold-tests
   (`run_qa_release_command`), refuses to clobber non-generated tests without
   `--force` (`qa_release/*`, `cli/qa_release.py`).

### Task 2 — SAFE-04 `verification-gates` (commit f5cf383)
`openspec/specs/verification-gates/spec.md` with 6 requirements:
1. Required-vs-warning outcome gates DONE — `run_verification_commands` →
   `VerificationRunOutcome.required_failed` only when a required command fails;
   non-required failure = warning, leaves `succeeded` True
   (`pipeline/verification.py`).
2. Started + per-command result events with secret redaction (`event_names`,
   `make_verification_started_event`, `make_verification_result_event`).
3. Env allowlist + non-hanging timeout/blocked execution (`_allowed_env`,
   `_timeout_result`, `_blocked_result`, process-group kill).
4. Human-review checkpoint gate — `requires_human_review` /
   `build_human_review_checkpoint` / `is_human_review_approved` +
   required/approved/rejected/changes_requested events (`pipeline/human_review.py`).
5. CI verification contract — `run_ci_verification` maps CI poll → verification
   result/evidence; unconfigured runner → unavailable (not raise)
   (`ci/verification.py`, `ci/models.py`).
6. Legacy commit-revert verifier — `verifier.verify_task` marked legacy and NOT
   wired into the v4 worker-claim DONE path (grep-confirmed: no worker callers;
   only its own module, a compliance docstring mention, and a unit test).

## Verification

- `openspec validate quality-compliance-audit --strict` → "is valid", exit 0.
- `openspec validate verification-gates --strict` → "is valid", exit 0.
- Wiring confirmed via grep: `run_verification_commands` in `cli/run.py` +
  `cli/worker.py`; human-review helpers in `worker/local.py` + `worker/remote.py`;
  `verify_task` has no worker-path callers.

## Deviations from Plan

None - plan executed exactly as written.

## Self-Check: PASSED

- FOUND: openspec/specs/quality-compliance-audit/spec.md
- FOUND: openspec/specs/verification-gates/spec.md
- FOUND commit: f75c684 (SAFE-03)
- FOUND commit: f5cf383 (SAFE-04)
