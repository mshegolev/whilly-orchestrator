# Phase 20: Jira Watcher Daemon - Context

**Gathered:** 2026-06-12
**Status:** Ready for planning
**Mode:** Smart discuss (autonomous) — all recommendations accepted by user

<domain>
## Phase Boundary

Operators can run a continuous Jira intake daemon (`whilly jira watch`) that wraps the
live-validated one-shot poll cycle, with lifecycle controls (graceful stop, status inspection,
single-instance guard), exponential backoff on transient failures, and the existing global-pause
and code/test-readiness gates honored before any autonomous work is dispatched. Covers WATCH-01,
WATCH-02, WATCH-03. CLI + files + optional DB events; no WUI surface.

</domain>

<decisions>
## Implementation Decisions

### Daemon model
- `whilly jira watch` as a new ACTION in the existing `whilly jira` subparser — a thin foreground
  loop wrapping the validated one-shot poll cycle (`collect_jira_work_snapshot` path), with
  `--issue KEY` repeatable.
- Foreground process; the operator backgrounds it via tmux/systemd. No fork/detach magic.
- Interval via `--interval SECONDS` and `WHILLY_JIRA_WATCH_INTERVAL` env; default 300 seconds.
- Independent of `SchedulerWorker` / `scheduler_rules` tables — that remains the JQL-rule intake
  path; watch stays a thin loop to avoid coupling.

### Lifecycle & status
- SIGINT/SIGTERM handled gracefully: finish the current cycle, write final status, exit 0.
- Status file `whilly_logs/watch/jira-watch-status.json` (running/stopped, pid, last poll time,
  cycle count, error count, backoff state) plus a `whilly jira watch-status` reader command.
- Single-instance guard: lock/pid check; a second watcher exits with a clear hint.
- Audit trail: file log always; DB audit event per cycle/failure when WHILLY_DATABASE_URL is set
  (same optional-DB pattern as Phase 19 smoke — never a hard DB requirement).

### Backoff & gates
- Exponential backoff on consecutive transient failures: 5/10/20/40/60s cap, reset on success
  (existing project convention).
- Global pause (`PauseControl` / `.whilly_pause`): while paused, keep read-only polling, dispatch
  nothing, record the paused state in status/audit.
- Readiness gate: when code/test readiness is not satisfied, record the block reason as an audit
  event and wait — never dispatch.
- Watch is intake/refresh-only by default. Autonomous dispatch happens only behind an explicit
  `--dispatch` flag, routed through the existing Phase-17-gated `jira run` path
  (`--readiness-repo-path` / `--allow-unready-run` semantics preserved).

### Claude's Discretion
- Internal module layout (e.g., `whilly/cli/jira_watch_loop.py` vs inline in cli/jira.py vs
  extending whilly/jira_watch.py); status-file JSON schema field names; lock mechanism choice
  (pidfile vs lockfile); how watch-status renders.
- Exact audit event types/payloads, consistent with existing `append_jira_work_event` usage.

</decisions>

<code_context>
## Existing Code Insights

### Reusable Assets
- `whilly/jira_watch.py` — `collect_jira_work_snapshot` (live-validated in Phase 19),
  `persist_jira_work_snapshot` (async, repo protocol with `append_jira_work_event`).
- `whilly/cli/jira.py` — `whilly jira` subparser pattern, credential gate (`_ensure_jira_config`),
  smoke implementation as the freshest reference (exit codes, env injection, optional persist).
- `whilly/pause_control.py` — `PauseControl.is_paused()` / `.get_pause_info()` file-based global
  pause (`.whilly_pause`).
- `whilly/cli/smoke.py` — report/redaction helpers; `_log_dir()` (WHILLY_LOG_DIR) convention.
- `whilly/scheduler/worker.py` — graceful-stop loop reference (`stop()` + `asyncio.Event`,
  per-interval due-checking) — reference only, not a dependency.
- Phase 17 readiness: `_run_readiness` in cli/jira.py, `_read_jira_work_readiness`,
  `--readiness-repo-path` / `--allow-unready-run` gating for `jira run`.

### Established Patterns
- Optional-DB persistence: best-effort, warn-not-fail (Phase 19 WR-03 fix is the reference).
- Exponential backoff 5/10/20/40/60s exists as a project convention (agent retry path).
- Unit tests inject collaborators (collector/getter lambdas) instead of patching HTTP.

### Integration Points
- New ACTION + dispatch branch in `build_jira_parser()` / `run_jira_command()`.
- Status/log files under `whilly_logs/watch/`.
- DB events through the existing jira-work-events table via `append_jira_work_event`.

</code_context>

<specifics>
## Specific Ideas

Success criteria from ROADMAP (must be TRUE):
1. `whilly jira watch` polls on a configurable interval without manual intervention.
2. Graceful stop + status inspection (running/stopped, last poll, error count).
3. Transient failures retried with exponential backoff; each retry/failure recorded as a
   queryable audit event.
4. Global pause active → no autonomous work dispatched until lifted.
5. Readiness gates unsatisfied → block reason recorded, watcher waits rather than dispatching.

</specifics>

<deferred>
## Deferred Ideas

- Merging watch with SchedulerWorker JQL-rule intake into one daemon framework.
- systemd unit / launchd plist packaging for the watcher.
- WUI surface for watcher status.

</deferred>
