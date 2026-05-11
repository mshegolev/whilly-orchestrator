---
gsd_state_version: 1.0
milestone: v1.1
milestone_name: UI parity completion
current_phase: "14"
current_phase_name: WUI method and fragment wiring
current_plan: null
status: ready_for_planning
last_updated: "2026-05-11T11:20:00Z"
last_activity: 2026-05-11
progress:
  total_phases: 6
  completed_phases: 3
  total_plans: 4
  completed_plans: 4
  percent: 50
---

# Project State

## Project Reference

See: `.planning/PROJECT.md` (updated 2026-05-11)

**Core value:** Operators can safely coordinate AI-assisted engineering work with auditable state,
human control, and verification before claiming success.
**Current focus:** v1.1 UI parity completion.

## Current Position

Current Milestone: v1.1
Phase: 14 - WUI method and fragment wiring
Plan: Not planned yet
Status: Ready for planning
Last Activity: 2026-05-11
Last Activity Description: Completed urgent Phase 13.2. Whilly now has `whilly feedback` for
explicit GitHub bug/idea issue reports.

Progress: [#####-----] 50%

## Active Scope

- Canonical surface, hotkey, action, selector, and route contract for TUI and WUI.
- WUI static/template cleanup so active code uses current DOM selectors and supported API paths.
- Logs/admin/PRD fragment decisions: fully wire with backend methods and TUI parity, or quarantine
  from active UI scope.
- Focused tests that prevent orphan WUI files or stale routes from drifting back in.
- Version lifecycle controls so operators can check for a newer Whilly release, apply a manual
  update, or opt into automatic update behavior without hidden environment mutation.
- A fast GitHub feedback channel so operators testing Whilly on another computer can report bugs
  or ideas directly from the CLI.

## Recent Decisions

- Phase 13 Plan 01 kept UI-01 metadata in `whilly/operator_views.py` to extend the existing pure
  operator contract pattern.
- TUI surface key handling now derives from `operator_surface_hotkeys()` instead of a duplicate
  literal map.
- Active WUI dashboard JavaScript receives surface order, hotkey copy, and route prefixes through
  dashboard context.
- Phase 13 Plan 02 classifies templates and static JavaScript only; CSS, fonts, and images remain
  outside the UI-02 artifact scope.
- `_logs.html` remains routeable but noncanonical with Phase 14 follow-up, while `_admin.html` and
  `_prd.html` remain inactive quarantined artifacts.
- `whilly/api/static/whilly-hotkeys.js` is now active after replacing stale `1-7` selectors and
  `/admin/workers/*` routes with the canonical five-surface API contract.
- Phase 13.1 was inserted after Phase 13 because update checks/manual update/automatic update
  policy are product lifecycle controls that should be available before continuing lower-priority
  WUI/TUI parity work.
- Phase 13.1 keeps automatic updates explicit: default mode is off, and no unrelated command
  silently upgrades Whilly.
- Update tests mock PyPI and subprocess boundaries so verification does not mutate the local
  environment.
- Phase 13.2 keeps feedback explicit and single-channel: GitHub Issues via `gh`, no email/GitLab,
  and no automatic crash reporting.

## Accumulated Context

### Roadmap Evolution

- Phase 13.1 inserted after Phase 13: Version update checks and manual/automatic update modes
  (URGENT).
- Phase 13.2 inserted after Phase 13.1: GitHub feedback issue reporter (URGENT).

## Performance Metrics

| Phase | Plan | Duration | Tasks | Files |
|-------|------|----------|-------|-------|
| 13 | 01 | 9 min | 3 | 7 |
| 13 | 02 | 8 min | 3 | 4 |
| 13.1 | 01 | focused | 3 | 6 |
| 13.2 | 01 | focused | 3 | 6 |

## Previous Milestone

v1.0 shipped and archived on 2026-05-08.

Archives:
- `.planning/milestones/v1.0-ROADMAP.md`
- `.planning/milestones/v1.0-REQUIREMENTS.md`
- `.planning/milestones/v1.0-MILESTONE-AUDIT.md`

## Deferred Items

- Browser/screen-reader QA for the complete WUI operator workflow.
- New operator modules beyond the pulled logs/admin/PRD artifacts.
- Replacement of the current Jinja/HTMX dashboard or Rich TUI architecture.

## Next Step

Run `$gsd-plan-phase 14` or `$gsd-discuss-phase 14` for WUI method and fragment wiring.
