---
phase: 27-safety-quality-cluster
plan: 01
subsystem: openspec-capability-specs
tags: [safety, budget, resource-guards, recovery, self-healing, reverse-spec]
requires: [task-model-fsm, state-persistence, cli-surface]
provides: [budget-resource-guards, recovery-self-healing]
affects: []
tech-stack:
  added: []
  patterns: [reverse-spec-from-source, legacy-status-annotation]
key-files:
  created:
    - openspec/specs/budget-resource-guards/spec.md
    - openspec/specs/recovery-self-healing/spec.md
  modified:
    - .planning/REQUIREMENTS.md
    - .planning/STATE.md
decisions:
  - "Budget contract spec'd as the Postgres plan.budget_exceeded sentinel, NOT the v3 kill-tmux/exit-2 lore"
  - "ResourceMonitor spec'd as standalone library with an explicit unwired-status requirement"
  - "recovery.py and self_healing.py marked legacy/unwired; live recovery references release_stale_tasks"
metrics:
  duration: ~10m
  completed: 2026-06-16
---

# Phase 27 Plan 01: Budget-Resource-Guards & Recovery-Self-Healing Specs Summary

Two normative OpenSpec capability specs reverse-spec'd from real v4 code: SAFE-01
`budget-resource-guards` (ResourceMonitor thresholds + the Postgres `plan.budget_exceeded`
sentinel + secret-free smoke-report exit codes) and SAFE-02 `recovery-self-healing`
(file-based recovery + self_healing excepthook, both marked legacy against the live
`release_stale_tasks` visibility-timeout sweep). Both pass `openspec validate --strict`.

## What was built

### Task 1 — SAFE-01 budget-resource-guards (commit 02025ed)
`openspec/specs/budget-resource-guards/spec.md` — 6 requirements, all SHALL/MUST,
each with WHEN/THEN scenarios:
1. ResourceLimits default thresholds (cpu 80 / mem 75 / disk 5GB / 5 procs / 2GB log
   dir / 30min) + check_limits severity rules (verified against resource_monitor.py
   lines 20-41, 158-202).
2. should_throttle decision rule — any high OR ≥2 medium (lines 215-229).
3. wait_for_resources polling to default 300s, 60s warning cooldown, and
   create_monitor_from_env env overrides (lines 231-250, 332-348).
4. Budget contract = single `plan.budget_exceeded` event (reason `budget_threshold`,
   threshold_pct 100) on the crossing call; budget_usd 0/NULL = unlimited; no
   process kill / budget-specific exit (verified repository.py 155-164, 641-665;
   config.py BUDGET_USD default 0.0).
5. Smoke-report exit codes EXIT_OK 0 / EXIT_CHECK_FAILED 1 / EXIT_CONFIG_MISSING 2,
   all_passed semantics, `_redact_url` credential stripping (smoke.py 25-27, 45-77,
   127-130). References cli-surface for the general exit-code contract.
6. Explicit ResourceMonitor wiring-status requirement: standalone, NOT in the v4
   worker-claim path; budget enforcement is the sentinel not the monitor.

### Task 2 — SAFE-02 recovery-self-healing (commit 83f887c)
`openspec/specs/recovery-self-healing/spec.md` — 6 requirements, all SHALL/MUST,
each with WHEN/THEN scenarios:
1. recover_task_statuses unions `[id] DONE` progress lines + log COMPLETION_MARKER
   (valid `{"type":"result"}`, is_error False), flips non-done legacy tasks, saves
   only on change, returns change dict (recovery.py 13-101; COMPLETION_MARKER from
   agents/base.py:99).
2. validate_task_consistency dual mismatch warnings (lines 104-131).
3. SelfHealingHandler.analyze_error classification of NameError / missing-positional
   TypeError / Import|ModuleNotFound / AttributeError → CodeError (self_healing.py
   32-152).
4. apply_fix scope — NameError (log, True) + ImportError (pip install); all else
   False (lines 164-210).
5. global_exception_handler analyze→apply→print-traceback; enable_self_healing sets
   sys.excepthook (lines 213-256).
6. Explicit legacy-status requirement: both modules unwired (0 callers); live
   recovery = `release_stale_tasks` visibility-timeout sweep per state-persistence.

## Verification

- `openspec validate budget-resource-guards --strict` → "is valid", exit 0.
- `openspec validate recovery-self-healing --strict` → "is valid", exit 0.
- All grounding-caution items honored: budget = sentinel (not v3 exit-2 lore);
  recovery/self_healing marked legacy pointing to release_stale_tasks; ResourceMonitor
  unwired status stated truthfully.
- Documentation-only — zero `whilly/` changes.

## Deviations from Plan

None — plan executed exactly as written.

## Self-Check: PASSED

- FOUND: openspec/specs/budget-resource-guards/spec.md
- FOUND: openspec/specs/recovery-self-healing/spec.md
- FOUND commit 02025ed (SAFE-01)
- FOUND commit 83f887c (SAFE-02)
