---
phase: 22-orchestration-cluster
plan: 03
subsystem: openspec-capability-specs
tags: [openspec, orchestration, agent-dispatch, tmux, runner, documentation]
requires:
  - openspec/AUTHORING.md (format authority)
  - openspec/specs/task-model-fsm/spec.md (worked exemplar)
provides:
  - openspec/specs/agent-dispatch/spec.md (ORCH-05)
affects:
  - .planning/REQUIREMENTS.md (ORCH-05 marked complete)
  - .planning/STATE.md (Current Position advanced)
tech-stack:
  added: []
  patterns: [reverse-spec'd-from-v4-code, normative-testable-openspec]
key-files:
  created:
    - openspec/specs/agent-dispatch/spec.md
  modified:
    - .planning/REQUIREMENTS.md
    - .planning/STATE.md
decisions:
  - "Runner selection spec'd as tmux-when-available AND USE_TMUX else subprocess (run_task), grounded in tmux_runner.tmux_available() + config.USE_TMUX — NOT gated on the removed WHILLY_WORKTREE/WHILLY_USE_WORKSPACE no-ops."
  - "Dispatch-seam requirement points the workspace cwd source at worktree-isolation rather than re-specifying workspace layout; cwd injection comes from the workspace_runner closure in cli/run.py (run_task(..., cwd=workspace.path))."
  - "Added a deny-by-default permission requirement (--disallowedTools ...,Bash; WHILLY_AGENT_ALLOW_SHELL override) and a retry/fail-fast-auth requirement from claude_cli.py — these are real v4.7.0 dispatch-time guarantees worth pinning normatively."
metrics:
  duration: ~10m
  completed: 2026-06-15
  tasks: 1
  files: 3
---

# Phase 22 Plan 03: Agent Dispatch Spec Summary

One normative OpenSpec capability spec, `agent-dispatch` (ORCH-05),
reverse-spec'd from the real v4.7.0 dispatch path: runner selection between the
tmux-session runner and the subprocess Claude CLI wrapper, the
`whilly-{task_id}` one-session-per-task convention, the prompt + prepared-
workspace-cwd dispatch seam, the deny-by-default tool posture, and the
retry/fail-fast-auth behavior around a single agent invocation.

## What Was Built

### Task 1 — agent-dispatch/spec.md (ORCH-05) — commit f246105

Seven requirements grounded in `whilly/tmux_runner.py`, `whilly/agent_runner.py`,
`whilly/core/agent_runner.py`, `whilly/adapters/runner/{claude_cli,env}.py`,
`whilly/cli/run.py`, `whilly/agents/base.py`, `whilly/worker/local.py`, and
`whilly/config.py`:

- **Runner selection** — tmux runner when `tmux_available()` AND `WHILLY_USE_TMUX`,
  subprocess runner (`run_task`) otherwise (tmux-unavailable / USE_TMUX-disabled).
- **One tmux session per task** — `whilly-{safe_task_id}` (via `safe_task_id_filename`),
  pre-existing same-named session killed before launch.
- **Built prompt** — dispatched agent receives `core.prompts.build_task_prompt` output;
  tmux passes it via a `{task_id}_prompt.txt` file (single literal arg, no shell interp).
- **Prepared-workspace cwd** — `run_task(..., cwd=workspace.path)` injected by the
  `workspace_runner` closure in `cli/run.py`; workspace-prepare failure returns a failing
  `AgentResult(is_complete=False)` instead of dispatching/crashing. Layout deferred to
  worktree-isolation.
- **No removed env-flag gate** — `WHILLY_WORKTREE` / `WHILLY_USE_WORKSPACE` have no dispatch
  effect (removed no-ops, config.py:93-94).
- **Deny-by-default posture** — `--output-format json` + `--disallowedTools
  Write,Edit,MultiEdit,NotebookEdit,Bash`; `WHILLY_AGENT_ALLOW_SHELL` drops the denylist and
  re-emits `--dangerously-skip-permissions`.
- **Retry / fail-fast auth** — backoff 5/10/20/40/60s on transient API errors; auth failures
  (`failed to authenticate` / `403 Forbidden`) returned immediately, no retry consumed.

## Verification

- `openspec validate agent-dispatch --strict` → "Specification 'agent-dispatch' is valid"
  (exit 0, 0 errors / 0 warnings).
- No delta headers (`## ADDED/MODIFIED/REMOVED/RENAMED`) in the file.
- Spec contains the explicit negation of any `WHILLY_WORKTREE` / `WHILLY_USE_WORKSPACE`
  dispatch gate (the v3 CLAUDE.md claim).

## Deviations from Plan

None — plan executed exactly as written. The plan scoped a single dispatch-selection
requirement set; the deny-by-default and retry/auth requirements were added as
grounded-in-code dispatch-time guarantees (Rule 2 — capturing real v4 contract surface the
must_haves implied via the adapters/runner read list), staying inside the agent-dispatch
boundary and not encroaching on result-collection or worktree-isolation.

## Notes on v3→v4 Grounding

Followed the CONTEXT.md architecture note: did NOT re-assert the stale v3 claim that
per-task worktrees are gated on `WHILLY_WORKTREE=1` AND `MAX_PARALLEL>1`. Those env fields
are removed no-ops in v4 (`config.py:93-94`); the spec asserts their NEGATION. The live v4
dispatch seam is the async `workspace_runner` closure in `cli/run.py` (not a `run_plan`
chdir), which injects `cwd=workspace.path` into `run_task` — spec'd from the observed code.
Workspace directory layout/lifecycle is intentionally referenced to the worktree-isolation
capability (ORCH-06) rather than duplicated here.

## Self-Check: PASSED

- openspec/specs/agent-dispatch/spec.md — FOUND
- commit f246105 — FOUND
