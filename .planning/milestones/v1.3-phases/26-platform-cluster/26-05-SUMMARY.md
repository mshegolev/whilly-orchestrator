---
phase: 26-platform-cluster
plan: 05
subsystem: self-update-doctor
tags: [openspec, spec, self-maintenance, update, doctor, repair, rollback]
requires:
  - openspec/AUTHORING.md
  - whilly/update.py
  - whilly/cli/update.py
  - whilly/doctor.py
  - whilly/repair/*
  - whilly/rollback/*
provides:
  - openspec/specs/self-update-doctor/spec.md
affects:
  - .planning/REQUIREMENTS.md
  - .planning/STATE.md
tech-stack:
  added: []
  patterns: [reverse-spec from v4 code, normative SHALL/MUST requirements, WHEN/THEN scenarios]
key-files:
  created:
    - openspec/specs/self-update-doctor/spec.md
  modified:
    - .planning/REQUIREMENTS.md
    - .planning/STATE.md
decisions:
  - "Specced the OBSERVED v4 behavior, not REQUIREMENTS lore — fail-closed semantics surfaced as normative."
  - "Doctor pinned as strictly read-only (never deletes) — a deliberate, testable contract."
  - "Restore pinned to the exact confirmation phrase + dry-run-no-reset, mirroring service.py guarantees."
metrics:
  duration: ~12m
  completed: 2026-06-16
---

# Phase 26 Plan 05: self-update-doctor Spec Summary

Authored the normative OpenSpec capability spec for `self-update-doctor` (PLAT-05),
reverse-spec'd from the real v4 update / doctor / repair / rollback code — 10
requirements, all WHEN/THEN scenarios, passing `openspec validate
self-update-doctor --strict` with 0 errors / 0 warnings.

## What was built

`openspec/specs/self-update-doctor/spec.md` (filled the BASE-01 stub directory).
`## Purpose` (≥50 chars) scoping the four self-maintenance subsystems, then
`## Requirements` with 10 `### Requirement:` blocks (each first body line uses
SHALL/MUST, each has ≥1 `#### Scenario:`):

**Update (`update.py` + `cli/update.py`)**
1. Non-mutating update check — PyPI JSON compare via `compare_versions`; fails
   closed to an `error` result (latest/available = None) on any boundary failure;
   CLI exit 0 (available / up-to-date) vs 1 (lookup failed).
2. Explicit package update — `auto` prefers pipx only inside a pipx context
   (PIPX_HOME/PIPX_BIN_DIR), else pip; dry-run prints `Would run:`; unsupported
   installer returns non-zero without executing.
3. Auto policy fails closed — `WHILLY_UPDATE_MODE` resolves to off/check/install,
   unknown ⇒ off; install mode applies an available update and reports the version.

**Doctor (`doctor.py`)**
4. Read-only diagnostics — orphan plans, stale state file, leftover
   workspaces/worktrees, leftover `whilly-` tmux sessions; never deletes/kills;
   clean env ⇒ empty `findings` + "All clean".
5. Ghost/stale classification — all-resolved or all-pending-with-all-linked-GH-
   issues-CLOSED ⇒ ghost; partial closures ⇒ stale; colon in filename ⇒
   invalid_name (no JSON parse).

**Repair (`repair/policy.py` + `models.py` + `tasks.py` + `events.py`)**
6. Bounded decision — `decide_repair` requests `<orig>-repair-<attempt>` while
   under budget; escalates `repair_disabled` (max≤0) / `repair_budget_exhausted`.
7. Repair task + audit events — task carries no dependency on the failed
   original, inherits key_files/priority/repo_target; requested/completed events
   raise ValueError on wrong action or non-DONE/FAILED terminal status.

**Rollback (`rollback/git_ops.py` + `service.py` + `models.py` + `cli/rollback.py`)**
8. Rollback point creation — annotated `whilly/rollback/<branch>/<ts>-<sha12>` tag
   at HEAD; Git failures surface as `RollbackError`.
9. Refusal-first preflight — dirty-worktree blocks protected ops; detached-HEAD
   blocks merge/restore; protected target blocks; missing backup warns; not-a-repo
   blocks.
10. Confirmed restore — `git reset --hard` only after preflight ok + exact
    `restore <sha12> to <branch>` phrase; dry-run performs no reset.

## Verification

- `openspec validate self-update-doctor --strict` → "is valid", exit 0.
- All PLAT-05-mapped modules (update.py, cli/update.py, doctor.py, rollback/*,
  cli/rollback.py, repair/*) accounted for by the capability.

## Deviations from Plan

None — plan executed exactly as written. Documentation-only; zero `whilly/` changes.

## Grounding notes

State wiring described truthfully: the doctor's stale `.whilly_state.json` check
is specced as the doctor's own filesystem inspection (it reads the legacy file to
flag rot), NOT as a live v4 state contract — consistent with PLAT-04 marking
StateStore/`.whilly_state.json` as legacy/no-op. No JSON-state-as-primary lore
was introduced.

## Self-Check: PASSED
- FOUND: openspec/specs/self-update-doctor/spec.md
- openspec validate self-update-doctor --strict → exit 0, valid
