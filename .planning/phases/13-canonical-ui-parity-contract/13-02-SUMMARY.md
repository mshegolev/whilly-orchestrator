---
phase: 13-canonical-ui-parity-contract
plan: "02"
subsystem: ui
tags: [operator-ui, wui, static-guards, hotkeys, parity]

# Dependency graph
requires:
  - phase: 13-canonical-ui-parity-contract
    provides: UI-01 canonical surface, action, selector, and route contract from Plan 01
provides:
  - WUI artifact status metadata for active, routeable noncanonical, and quarantined files
  - Static active-WUI stale-pattern guards for templates and static JavaScript
  - Static whilly-hotkeys.js implementation aligned to five canonical surfaces and API worker routes
  - Rendered dashboard stale-pattern guards plus routeable logs-fragment contract coverage
affects: [14-wui-method-and-fragment-wiring, 15-tui-capability-parity, 16-ui-parity-verification-and-docs]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - Pure Python WUI artifact metadata in whilly/operator_views.py
    - Static tests discover template and static JavaScript artifacts from disk
    - Rendered dashboard tests derive surface and route expectations from operator_views helpers

key-files:
  created:
    - tests/unit/test_wui_contract_static.py
  modified:
    - whilly/operator_views.py
    - whilly/api/static/whilly-hotkeys.js
    - tests/integration/test_htmx_dashboard.py

key-decisions:
  - "Classified templates and static JavaScript only for Phase 13; CSS and other static assets remain outside UI-02 artifact scope."
  - "Kept logs routeable but noncanonical for Phase 14 instead of adding it to active five-surface navigation."
  - "Activated whilly-hotkeys.js after replacing stale 1-7 selectors and /admin/workers routes with the canonical five-surface API contract."

patterns-established:
  - "Non-active WUI artifacts require a reason and follow-up phase."
  - "Active WUI files are scanned for banned stale literals and route regexes before later wiring phases."

requirements-completed: [UI-02]

# Metrics
duration: 8 min
completed: 2026-05-11
---

# Phase 13 Plan 02: WUI Artifact Classification And Stale-Pattern Guards Summary

**WUI artifact classification with active-file stale-pattern guards and canonical static hotkeys.**

## Performance

- **Duration:** 8 min
- **Started:** 2026-05-11T10:33:47Z
- **Completed:** 2026-05-11T10:41:56Z
- **Tasks:** 3
- **Files modified:** 4

## Accomplishments

- Added `OperatorUiArtifactStatus`, `OperatorUiArtifact`, `OPERATOR_WUI_ARTIFACTS`, and `operator_wui_artifacts()` to classify every WUI template and static JavaScript artifact.
- Added `tests/unit/test_wui_contract_static.py`, which discovers current WUI templates/static JS and scans active artifacts for stale `1-7`, `.tabs [data-key]`, and non-API worker routes.
- Replaced `whilly/api/static/whilly-hotkeys.js` with the five-surface contract using `[data-surface-tab]`, `#dashboard-filter`, review actionable rows, and `/api/v1/admin/workers/*`.
- Extended rendered WUI integration coverage to reject stale active-dashboard patterns and confirm `/?fragment=logs` remains a routeable noncanonical artifact.

## Task Commits

Each TDD task was committed atomically:

1. **Task 1 RED: WUI artifact contract tests** - `a886c86` (test)
2. **Task 1 GREEN: WUI artifact classification** - `deee4fd` (feat)
3. **Task 2 RED: Static hotkey contract test** - `6c947da` (test)
4. **Task 2 GREEN: Canonical static WUI hotkeys** - `7cb1921` (feat)
5. **Task 3: Rendered WUI stale-pattern guards** - `f4e3983` (test)

## Files Created/Modified

- `tests/unit/test_wui_contract_static.py` - Static WUI artifact classification and active-file stale-pattern regression tests.
- `whilly/operator_views.py` - WUI artifact status metadata and filtering helper.
- `whilly/api/static/whilly-hotkeys.js` - Static hotkeys aligned to five canonical surfaces, current selectors, and API worker routes.
- `tests/integration/test_htmx_dashboard.py` - Rendered dashboard stale-pattern assertions and logs-fragment contract test.

## Decisions Made

- Classified only templates and static JavaScript artifacts in Phase 13 because UI-02 targets stale UI behavior, routes, and selectors rather than CSS/fonts/images.
- Kept `_logs.html` routeable but noncanonical with Phase 14 follow-up instead of expanding active navigation beyond the five canonical surfaces.
- Removed the static `t` theme-cycle hotkey from `whilly-hotkeys.js` because it is outside the Phase 13 shared action contract.

## Deviations from Plan

None - plan executed exactly as written.

## Issues Encountered

- Task 3's new rendered regression tests passed immediately because Plan 13-01 and Task 2 already made the rendered dashboard and logs fragment satisfy the asserted behavior. No production code change was required for Task 3.

## Verification

- `.venv/bin/python -m pytest -q tests/unit/test_operator_ui_contract.py tests/unit/test_wui_contract_static.py tests/integration/test_htmx_dashboard.py::test_dashboard_mirrors_operator_surfaces_and_hotkeys tests/integration/test_htmx_dashboard.py::test_logs_fragment_is_routeable_noncanonical_artifact tests/integration/test_control_state_admin_api.py::test_admin_can_pause_resume_and_read_control_state` - 8 passed
- `.venv/bin/python -m pytest -q tests/unit/test_operator_views.py tests/unit/test_tui.py tests/unit/test_wui_contract_static.py tests/integration/test_htmx_dashboard.py tests/integration/test_control_state_admin_api.py` - 64 passed
- `.venv/bin/python -m ruff check whilly/operator_views.py whilly/cli/tui.py whilly/api/dashboard.py tests/unit/test_operator_ui_contract.py tests/unit/test_wui_contract_static.py tests/unit/test_tui.py tests/integration/test_htmx_dashboard.py` - All checks passed

## User Setup Required

None - no external service configuration required.

## Next Phase Readiness

UI-02 is implemented and verified. Phase 14 can use the artifact status contract to wire or quarantine logs/admin/PRD fragments without silently shipping stale routes or orphan UI files.

## Self-Check: PASSED

- Summary file and key created/modified files exist on disk.
- Task commits verified: `a886c86`, `deee4fd`, `6c947da`, `7cb1921`, `f4e3983`.

---
*Phase: 13-canonical-ui-parity-contract*
*Completed: 2026-05-11*
