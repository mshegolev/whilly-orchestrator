# Global Worker Pause / Resume Design

## Goal

Add a real operator stop-crane for Whilly: pressing `Pause` stops work across
all workers, and pressing `Resume` lets workers continue. This replaces the
current overloaded dashboard meaning where `p` only freezes UI refresh.

## Approved Semantics

Pause is soft. It does not immediately kill a runner or subprocess. It does:

- stop all workers from claiming new tasks,
- make active workers release their current task back to `PENDING` at the
  nearest safe checkpoint,
- record auditable pause/resume and release reasons,
- show the paused state in WUI and TUI.

Resume clears the global pause state. Workers then resume normal claim loops.

## Current State

The current WUI/TUI `pause` is local to the view:

- WUI blocks automatic HTMX/SSE swaps while paused.
- TUI stops polling the operator snapshot while paused.
- Workers keep running.

Postgres does not currently have a control-plane pause state. The older
`paused`, `pause_reason`, and `paused_at` fields exist only in a legacy local
state snapshot fixture, not in the control-plane schema.

## Architecture

Add a singleton control-state table:

```sql
CREATE TABLE control_state (
    id TEXT PRIMARY KEY,
    paused BOOLEAN NOT NULL DEFAULT FALSE,
    pause_reason TEXT,
    paused_by TEXT,
    paused_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
```

The singleton id is `global`. Repository methods read and mutate this row:

- `get_control_state()`
- `pause_workers(reason, operator)`
- `resume_workers(operator)`
- `is_workers_paused()`

The worker claim path checks this state before claiming. If paused, local and
remote workers sleep/poll without claiming. Active workers check after each
safe boundary: after claim, before starting runner, after runner/verification,
and before completion. If pause is observed while a task is in-flight, the
worker releases the task with `reason="operator_pause"` and returns to the idle
paused loop.

## API

Add admin-only endpoints:

- `POST /api/v1/admin/workers/pause`
- `POST /api/v1/admin/workers/resume`
- `GET /api/v1/admin/workers/control-state`

`pause` accepts an optional reason. The existing admin bearer model is used;
non-admin dashboard tokens cannot pause workers.

## UI And Hotkeys

WUI:

- `Pause` button calls the admin pause endpoint.
- `Resume` button calls the admin resume endpoint.
- `p` triggers global pause.
- `R` triggers global resume.
- The old UI-only pause becomes `Freeze view` with hotkey `f`.

TUI:

- `p` triggers global pause through the DB-backed operator path.
- `R` triggers global resume.
- `f` freezes local screen refresh.
- The header shows `WORKERS PAUSED` plus reason/operator/time when paused.

## Audit And Observability

Record events:

- `control.pause`
- `control.resume`
- `RELEASE` with `payload.reason = "operator_pause"` when a worker returns a
  task because of the stop-crane.

Dashboards must show the paused state even when local view refresh is frozen.

## Non-Goals

- No hard kill of already-running subprocesses in the first version.
- No per-plan pause in the first version; this is global.
- No new `TaskStatus`; pause is control-plane state plus release events.
- No use of read-only dashboard tokens for mutation.

## Acceptance Criteria

- Pressing WUI/TUI pause prevents new claims across local and remote workers.
- Active workers release their current task with `operator_pause` at a safe
  checkpoint and do not complete it while paused.
- Pressing resume lets workers claim again.
- WUI and TUI distinguish global worker pause from local view freeze.
- Admin auth protects HTTP pause/resume endpoints.
- Tests cover repository state, admin API, local worker, remote worker, WUI,
  TUI, and audit event payloads.
