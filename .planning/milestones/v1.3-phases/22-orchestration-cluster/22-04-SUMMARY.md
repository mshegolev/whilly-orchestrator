---
phase: 22-orchestration-cluster
plan: 04
subsystem: openspec-capability-specs
tags: [openspec, orchestration, batch-planning, worktree-isolation, documentation]
requires:
  - openspec/AUTHORING.md (format authority)
  - openspec/specs/task-model-fsm/spec.md (worked exemplar)
provides:
  - openspec/specs/batch-planning/spec.md (ORCH-04)
  - openspec/specs/worktree-isolation/spec.md (ORCH-06)
affects:
  - .planning/REQUIREMENTS.md (ORCH-04 + ORCH-06 marked complete)
  - .planning/STATE.md (Current Position advanced)
tech-stack:
  added: []
  patterns: [reverse-spec'd-from-v4-code, normative-testable-openspec]
key-files:
  created:
    - openspec/specs/batch-planning/spec.md
    - openspec/specs/worktree-isolation/spec.md
  modified:
    - .planning/REQUIREMENTS.md
    - .planning/STATE.md
decisions:
  - "batch-planning spec'd plan_batches as a PURE grouping helper (non-overlapping key_files, empty-key-files always batchable, max_parallel<=1 single-task batches, per-batch cap) — did NOT couple it to a v3 first-batch-then-re-evaluate dispatch loop; batch consumption is referenced to orchestration-loop, not re-specified here."
  - "plan_batches_llm spec'd as the LLM variant that falls back to plan_batches on any error (non-zero agent exit, non-list response, unparseable output, empty valid batches) and validates returned ids against the ready set — so it is never worse than deterministic; <=1 ready task bypasses the LLM."
  - "worktree-isolation grounded the v4 plan workspace in workspaces.RepoTargetWorkspaceResolver (.whilly_workspaces/repos/<repo>/<plan>/<task>, branch whilly/<plan>/<task>, repo-target-less tasks reuse the process cwd with reused_current_cwd=True, workspace.prepared event, unregistered target raises) — wired into cli/run.py:434."
  - "WorktreeManager (.whilly_worktrees/{task_id} create -> merge_back cherry-pick HEAD..branch -> cleanup) spec'd from the class; its REAL activation asserted as direct construction by a caller, NOT wired into the live whilly run path and NOT gated by WHILLY_WORKTREE / WHILLY_USE_WORKSPACE (removed no-ops, config.py:93-94)."
  - "Per CONTEXT.md v3->v4 note, the spec asserts NEITHER a .whilly_workspaces/{slug}/ plan-level worktree as the v4 model NOR an env-flag activation gate — the legacy create_plan_workspace/PlanWorkspace helpers still in worktree_runner.py were deliberately NOT spec'd as the v4 contract."
metrics:
  duration: ~12m
  completed: 2026-06-16
  tasks: 2
  files: 4
---

# Phase 22 Plan 04: Batch Planning + Worktree Isolation Specs Summary

Two independent normative OpenSpec capability specs, `batch-planning` (ORCH-04)
and `worktree-isolation` (ORCH-06), each reverse-spec'd from the real v4.7.0
code. `batch-planning` pins the contract of the two pure grouping helpers in
`whilly/orchestrator.py`; `worktree-isolation` pins the v4 per-task workspace
resolver and the per-task git-worktree lifecycle, explicitly negating the stale
v3 plan-level-worktree and env-flag-gate claims.

## What Was Built

### Task 1 — batch-planning/spec.md (ORCH-04) — commit 4ba9713

Six requirements grounded in `whilly/orchestrator.py` (`plan_batches`,
`plan_batches_llm`):

- **Non-overlapping key_files grouping** — two ready tasks sharing any `key_files`
  path are never co-batched; disjoint tasks MAY share a batch.
- **Empty key_files never blocks a batch** — a task with empty `key_files`
  conflicts with nothing and is always eligible to join.
- **max_parallel<=1 single-task batches** — one task per batch, ready-set order
  preserved.
- **Per-batch cap** — no batch exceeds `max_parallel` tasks.
- **LLM fallback** — `plan_batches_llm` falls back to `plan_batches` on non-zero
  agent exit, non-list response, unparseable output, or empty valid batches; ids
  validated against the ready set.
- **Trivial ready sets** — <=1 ready task returns single-task batches without
  invoking the LLM.

Batch consumption by the run loop is referenced to the orchestration-loop
capability rather than re-specified (no v3 first-batch-then-re-evaluate coupling).

### Task 2 — worktree-isolation/spec.md (ORCH-06) — commit b3edc05

Seven requirements grounded in `whilly/workspaces.py`
(`RepoTargetWorkspaceResolver`, `prepare_git_workspace`) and
`whilly/worktree_runner.py` (`WorktreeManager`):

- **Repo-target-less tasks reuse the process cwd** — `prepare` returns a workspace
  at the resolver cwd with `reused_current_cwd=True`.
- **Repo-targeted deterministic checkout** — `<base>/<repo>/<plan>/<task>` under
  `WHILLY_WORKSPACE_BASE` (default `.whilly_workspaces/repos`), branch
  `whilly/<plan>/<task>`.
- **workspace.prepared event** — recorded with target id, repo full name, branch,
  and workspace path when a recorder is exposed.
- **Unregistered target raises** — unknown `repo_target_id` raises `RuntimeError`.
- **Per-task worktree creation** — `WorktreeManager.create` adds a worktree at
  `.whilly_worktrees/<task_id>` on branch `whilly/<task_id>`, clearing stale
  worktrees first; `git worktree add` failure raises.
- **Merge-back via cherry-pick** — `merge_back` cherry-picks `HEAD..<branch>`, a
  no-commit range is a successful no-op, a conflict aborts and reports.
- **Cleanup + real activation** — `cleanup` removes worktree and branch; the live
  `whilly run` path uses `RepoTargetWorkspaceResolver` and does NOT construct a
  `WorktreeManager` nor read any `WHILLY_WORKTREE` / `WHILLY_USE_WORKSPACE` flag.

## Verification

- `openspec validate batch-planning --strict` → "Specification 'batch-planning' is
  valid" (exit 0, 0 errors / 0 warnings).
- `openspec validate worktree-isolation --strict` → "Specification
  'worktree-isolation' is valid" (exit 0, 0 errors / 0 warnings).
- Neither file contains delta headers (`## ADDED/MODIFIED/REMOVED/RENAMED`).
- worktree-isolation contains no `.whilly_workspaces/{slug}/` plan-worktree-as-v4
  claim and no `WHILLY_WORKTREE` / `WHILLY_USE_WORKSPACE` activation-gate claim;
  it asserts their negation.

## Deviations from Plan

None — plan executed exactly as written. Both specs were authored from the
read-list source modules and pass strict validation on the first run.

## Notes on v3→v4 Grounding

Followed the CONTEXT.md architecture note. For batch-planning, `plan_batches` /
`plan_batches_llm` are real v4 helpers but are LEGACY/UNWIRED relative to the v4
live worker-claim path (parallelism in v4 is multiple workers claiming via FOR
UPDATE SKIP LOCKED); the spec captures the helpers' actual return contract
truthfully and defers dispatch coupling to orchestration-loop. For
worktree-isolation, the v4 plan workspace is `RepoTargetWorkspaceResolver`
(wired into `cli/run.py:434`), NOT the legacy `create_plan_workspace` /
`PlanWorkspace` (`.whilly_workspaces/{slug}/`) helpers that still exist in
`worktree_runner.py`; `WorktreeManager` is grounded from the class with its real
activation (direct construction, not the live run path, no env gate).

## Self-Check: PASSED

- openspec/specs/batch-planning/spec.md — FOUND
- openspec/specs/worktree-isolation/spec.md — FOUND
- commit 4ba9713 — FOUND
- commit b3edc05 — FOUND
