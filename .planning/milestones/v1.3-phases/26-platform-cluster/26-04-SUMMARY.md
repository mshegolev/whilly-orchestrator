---
phase: 26-platform-cluster
plan: 04
subsystem: state-persistence
tags: [openspec, spec, postgres, persistence, documentation-only]
requires:
  - openspec/AUTHORING.md
  - openspec/specs/task-model-fsm/spec.md (exemplar)
provides:
  - openspec/specs/state-persistence/spec.md (normative state-persistence capability spec)
affects:
  - .planning/REQUIREMENTS.md (PLAT-04 checked)
  - .planning/STATE.md (Current Position advanced)
tech-stack:
  added: []
  patterns:
    - reverse-spec from real v4 code (Postgres layer primary)
    - wiring verification before speccing (truthful legacy/no-op recording)
key-files:
  created:
    - openspec/specs/state-persistence/spec.md
  modified:
    - .planning/REQUIREMENTS.md
    - .planning/STATE.md
decisions:
  - "Speced the Postgres adapters/db layer as PRIMARY; v3 StateStore/.whilly_state.json marked legacy/no-op (verified zero instantiations)."
  - "PauseControl (.whilly_pause) specced as a LIVE local file-based control signal distinct from Postgres state — confirmed imported/instantiated in cli/jira_watch_loop."
metrics:
  duration: ~10m
  completed: 2026-06-16
---

# Phase 26 Plan 04: state-persistence Spec Summary

Reverse-spec'd Whilly's v4 state-persistence capability from the real Postgres adapter
layer into a normative, strict-valid OpenSpec spec — asyncpg pool, optimistic-locked
TaskRepository, atomic events audit log, and the Alembic migration chain as primary;
the legacy v3 JSON StateStore truthfully marked unwired/no-op.

## What Was Built

`openspec/specs/state-persistence/spec.md` — `## Purpose` (well over 50 chars) plus
`## Requirements` with 8 `### Requirement:` blocks, each with a SHALL/MUST first body
line and ≥1 `#### Scenario:` (WHEN/THEN):

1. Connection pool lifecycle + DSN coercion (`pool.py`): `postgresql+asyncpg://` →
   `postgresql://` stripping, env sizing (`WHILLY_DB_POOL_MIN`/`MAX`, defaults 2/10),
   `SELECT 1` fail-fast health check, graceful `close_pool`.
2. Atomic claim via `SELECT ... FOR UPDATE SKIP LOCKED` (`claim_task`) — no two workers
   claim the same row.
3. Optimistic-locked complete/fail filtered by `WHERE id=$1 AND version=$2 AND status IN (...)`,
   version increment, `VersionConflictError` on lost update.
4. Events audit written in the same transaction as every transition, incl. the
   visibility-timeout `RELEASE` sweep (`release_stale_tasks`).
5. Worker registration (token-hash only, never plaintext) + heartbeat liveness
   (`register_worker` / `update_heartbeat`).
6. Alembic migration chain (001–028) as the schema source of truth — batch-referenced at
   subsystem altitude, not enumerated per file.
7. Legacy/no-op StateStore: v3 `StateStore` / `.whilly_state.json` / `WHILLY_STATE_FILE`
   are NOT the live persistence contract.
8. PauseControl `.whilly_pause` specced as a LIVE local file-based control signal distinct
   from Postgres persistence.

## Wiring Verification (performed before speccing)

- `grep "StateStore("` → **zero instantiations** in `whilly/`. Only a docstring/comment
  reference to its atomic-write pattern in `cli/jira_watch_loop.py`. Confirmed
  legacy/no-op — `.whilly_state.json` / `WHILLY_STATE_FILE` are no-ops in v4. NOT pinned
  as live (honors CONTEXT.md grounding caution).
- `PauseControl` (`pause_control.py`) → **LIVE**: imported and instantiated in
  `cli/jira_watch_loop.py` (`from whilly.pause_control import PauseControl`;
  `effective_pause_ctrl = ... PauseControl()`), backed by `.whilly_pause`. Specced
  truthfully as a local control signal, explicitly NOT part of the Postgres contract.
- `SessionHistory` (`history.py`) → no live instantiation found in cli/orchestrator/api;
  treated as out of the primary Postgres contract (not pinned as live).

## Verification

- `openspec validate state-persistence --strict` → "Specification 'state-persistence' is
  valid", exit 0 (0 errors, 0 warnings).
- No delta headers; requirement bodies use SHALL/MUST; every requirement has a 4-hashtag
  `#### Scenario:` with WHEN/THEN.

## Deviations from Plan

None — plan executed exactly as written. Documentation-only; zero `whilly/` changes.

## Self-Check: PASSED

- FOUND: openspec/specs/state-persistence/spec.md
- FOUND: `openspec validate state-persistence --strict` exit 0
- Commit hash recorded below post-commit.
