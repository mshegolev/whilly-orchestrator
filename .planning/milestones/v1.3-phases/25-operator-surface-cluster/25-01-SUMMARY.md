---
phase: 25-operator-surface-cluster
plan: 01
subsystem: ui
tags: [openspec, dashboard, rich-live, sse, htmx, reporting, reverse-spec]

# Dependency graph
requires:
  - phase: 21-spec-baseline-taxonomy
    provides: AUTHORING.md conventions + task-model-fsm exemplar + capability slug taxonomy
  - phase: 24-integrations-cluster
    provides: established reverse-spec discipline (truthful wiring-status reporting)
provides:
  - openspec/specs/dashboard-tui/spec.md — normative spec for the Rich Live TUI + web SSE dashboard
  - openspec/specs/reporting/spec.md — normative spec for JSON/Markdown reporting + truthful v4 wiring status
affects: [25-02, 25-VERIFICATION, cli-surface, operator-views-logs, web-status-ui, Phase 28 coverage audit]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "Reverse-spec from real v4 source; truthful legacy/unwired status as an explicit normative requirement"

key-files:
  created:
    - openspec/specs/dashboard-tui/spec.md
    - openspec/specs/reporting/spec.md
  modified:
    - .planning/REQUIREMENTS.md
    - .planning/STATE.md

key-decisions:
  - "Reporting spec records Reporter/generate_summary as a legacy/unwired contract (verified: no Reporter( instantiation and no generate_summary() call site anywhere in whilly/), while the formatter helpers are the live dashboard presentation contract."
  - "dashboard-tui enumerates exactly the 11 registered hotkeys (d,l,t,s,$,p,g,c,n,r,h/?) plus the wizard 1/2/3 mode keys; unbound keys spec'd as no-op."

patterns-established:
  - "Pattern: a 'wiring status' requirement states SHALL NOT-required-to-instantiate to capture legacy surfaces truthfully without over-asserting."

requirements-completed: [OPS-01, OPS-03]

# Metrics
duration: ~12min
completed: 2026-06-16
---

# Phase 25 Plan 01: Operator Surface — dashboard-tui + reporting Summary

**Reverse-spec'd the Rich Live TUI + web SSE dashboard (OPS-01) and the JSON/Markdown reporting layer with its truthful v4 legacy/unwired status (OPS-03) as two strict-valid OpenSpec capability specs.**

## Performance

- **Duration:** ~12 min
- **Started:** 2026-06-16
- **Completed:** 2026-06-16
- **Tasks:** 2
- **Files modified:** 4 (2 specs created, REQUIREMENTS.md + STATE.md updated)

## Accomplishments
- `dashboard-tui` spec: Rich Live full-screen cadence (`screen=True`, `refresh_per_second=1`), the 11 registered hotkeys + wizard 1/2/3 mode keys with unbound-key no-op, overlay-mode state transitions (`log`/`task_log`/`detail`), `NullDashboard` headless substitution, the `whilly dashboard` subcommand 1s polling + exit codes (0/2), and the web SSE dashboard with `hx-get` fragment fallback.
- `reporting` spec: per-iteration JSON report path, `finalize` cost totals, `generate_summary` cross-plan Markdown (+ None-on-empty), and the `fmt_tokens`/`fmt_duration`/`CostTotals` presentation helpers.
- Verified-and-stated the truthful v4 wiring status as a normative requirement: `Reporter` is never instantiated and `generate_summary` is never called in `whilly/`; the only live consumer is `whilly/dashboard.py` importing the three formatter helpers.
- Both specs pass `openspec validate <slug> --strict` (exit 0, "is valid").

## Task Commits

Each task was committed atomically:

1. **Task 1: Spec the dashboard-tui capability (OPS-01)** - `63ee179` (docs)
2. **Task 2: Spec the reporting capability (OPS-03)** - `cd82770` (docs)

**Plan metadata:** (this commit) docs(25-01): complete plan

## Files Created/Modified
- `openspec/specs/dashboard-tui/spec.md` - Normative spec for Rich Live TUI states/hotkeys + web SSE dashboard (created)
- `openspec/specs/reporting/spec.md` - Normative spec for JSON/Markdown reporting + truthful v4 wiring status (created)
- `.planning/REQUIREMENTS.md` - Marked OPS-01 + OPS-03 complete; updated traceability row
- `.planning/STATE.md` - Updated Current Position + progress to Phase 25 in progress

## Decisions Made
- Encoded the truthful v4 reporting wiring status as a dedicated "Legacy v4 wiring status" requirement using a non-asserting form ("SHALL NOT be required to instantiate Reporter per iteration") rather than claiming the reporter runs each iteration — matches the grounding caution and the v4 Phase 22 worker-claim reality.
- Grounded the dashboard hotkey requirement strictly on `Dashboard.start`'s `keyboard.register` calls (no invented keys); the `cli/dashboard.py` subcommand hotkeys (q/r/p) are spec'd separately from the full Rich `dashboard.py` set since they are different surfaces.

## Deviations from Plan

None - plan executed exactly as written.

## Issues Encountered
None.

## User Setup Required
None - no external service configuration required.

## Next Phase Readiness
- OPS-01 + OPS-03 complete. Remaining Phase 25 requirements: OPS-02 (`web-status-ui`), OPS-04 (`cli-surface`), OPS-05 (`operator-views-logs`).
- Boundary note for OPS-02: `dashboard-tui` references but does not duplicate the FastAPI control plane / transport / auth surface — `web-status-ui` owns it.

## Self-Check: PASSED

- FOUND: openspec/specs/dashboard-tui/spec.md
- FOUND: openspec/specs/reporting/spec.md
- FOUND: .planning/phases/25-operator-surface-cluster/25-01-SUMMARY.md
- FOUND commit: 63ee179 (Task 1, dashboard-tui)
- FOUND commit: cd82770 (Task 2, reporting)
- `openspec validate dashboard-tui --strict` → valid (exit 0)
- `openspec validate reporting --strict` → valid (exit 0)

---
*Phase: 25-operator-surface-cluster*
*Completed: 2026-06-16*
