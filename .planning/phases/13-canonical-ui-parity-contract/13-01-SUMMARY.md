---
phase: 13-canonical-ui-parity-contract
plan: "01"
subsystem: ui
tags: [operator-ui, tui, wui, contract, hotkeys]

# Dependency graph
requires:
  - phase: 05-shared-operator-table-contract
    provides: Pure operator table metadata pattern reused for UI action/surface contracts
provides:
  - Canonical operator surface hotkeys, action specs, WUI selectors, and route prefixes
  - TUI surface key handling derived from the shared surface hotkey contract
  - WUI rendered surface order, hotkey copy, and active route prefixes derived from dashboard context
affects: [13-canonical-ui-parity-contract, 14-wui-method-and-fragment-wiring, 15-tui-capability-parity]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - Pure Python operator UI metadata in whilly/operator_views.py
    - Dashboard context injection for Jinja-rendered JavaScript contract values

key-files:
  created:
    - tests/unit/test_operator_ui_contract.py
  modified:
    - whilly/operator_views.py
    - whilly/cli/tui.py
    - whilly/api/dashboard.py
    - whilly/api/templates/index.html.j2
    - tests/unit/test_tui.py
    - tests/integration/test_htmx_dashboard.py

key-decisions:
  - "Kept Phase 13 UI-01 metadata in whilly/operator_views.py to extend the existing pure operator contract pattern."
  - "Derived TUI key mapping from operator_surface_hotkeys() rather than maintaining a duplicate literal map."
  - "Injected WUI surface order, hotkey copy, and route prefixes through dashboard context so active template JavaScript consumes the contract."

patterns-established:
  - "Operator UI actions are represented by frozen OperatorActionSpec values with optional hotkeys, surfaces, selectors, and route prefixes."
  - "Rendered WUI JavaScript receives route prefixes and surface order from Python context, not hand-copied literals."

requirements-completed: [UI-01]

# Metrics
duration: 9 min
completed: 2026-05-11
---

# Phase 13 Plan 01: Canonical UI Surface/Action Contract Summary

**Canonical operator UI contract with TUI hotkeys and WUI surface/route rendering derived from shared metadata.**

## Performance

- **Duration:** 9 min
- **Started:** 2026-05-11T10:15:40Z
- **Completed:** 2026-05-11T10:24:20Z
- **Tasks:** 3
- **Files modified:** 7

## Accomplishments

- Added `OperatorAction`, `OperatorActionSpec`, surface hotkey helpers, WUI selectors, and WUI route prefix helpers to `whilly/operator_views.py`.
- Changed TUI surface key handling and help copy to derive from `operator_surface_hotkeys()` and `operator_surface_hotkey_help()`.
- Passed contract-derived surface order, hotkey copy, and route prefixes into the active WUI dashboard template.
- Added focused unit and rendered integration coverage for the UI-01 contract.

## Task Commits

Each TDD task was committed atomically:

1. **Task 1 RED: Operator UI contract test** - `0ba98db` (test)
2. **Task 1 GREEN: Operator UI action contract** - `7bee441` (feat)
3. **Task 2 RED: TUI surface contract test** - `ea981c6` (test)
4. **Task 2 GREEN: TUI derived hotkeys** - `3938ddf` (feat)
5. **Task 3 RED: WUI rendered contract test** - `9e0274c` (test)
6. **Task 3 GREEN: WUI contract context/template** - `10053a6` (feat)

## Files Created/Modified

- `tests/unit/test_operator_ui_contract.py` - New UI-01 contract test for surface hotkeys, actions, selectors, and route prefixes.
- `whilly/operator_views.py` - Canonical pure operator UI action, hotkey, selector, and route metadata.
- `whilly/cli/tui.py` - TUI surface map and help copy now derive from shared hotkey helpers.
- `tests/unit/test_tui.py` - TUI test covering contract-derived surface switching and ignored key `6`.
- `whilly/api/dashboard.py` - Dashboard context now includes surface order JSON, hotkey label, and WUI route prefixes.
- `whilly/api/templates/index.html.j2` - Active WUI hotkey copy, `surfaceOrder`, worker fetch, and review fetch use contract context.
- `tests/integration/test_htmx_dashboard.py` - Rendered WUI assertions derive expected surfaces/hotkeys/routes from the contract.

## Decisions Made

- Kept new UI contract metadata in `whilly/operator_views.py` instead of creating a separate module, matching the existing Phase 5 table contract pattern.
- Injected route prefixes into the rendered WUI template as JavaScript constants so worker and review controls use supported `/api/v1/...` prefixes from the contract.
- Left logs/admin/PRD route expansion out of scope for this plan; the canonical surface set remains exactly five surfaces.

## Deviations from Plan

None - plan executed exactly as written.

## Issues Encountered

- `gsd-tools state advance-plan`, `state record-metric`, `state add-decision`, and
  `state record-session` could not parse the early v1.1 `STATE.md` shape because the expected
  sections were not present. `state update-progress`, `roadmap update-plan-progress`, and
  `requirements mark-complete UI-01` succeeded; the remaining STATE.md position, decision, metric,
  and next-step updates were applied manually.

## Verification

- `.venv/bin/python -m pytest -q tests/unit/test_operator_ui_contract.py tests/unit/test_tui.py tests/integration/test_htmx_dashboard.py::test_dashboard_mirrors_operator_surfaces_and_hotkeys tests/integration/test_htmx_dashboard.py::test_dashboard_review_prompt_recovery_copy_and_hotkey_contract` - 20 passed
- `.venv/bin/python -m ruff check whilly/operator_views.py whilly/cli/tui.py whilly/api/dashboard.py tests/unit/test_operator_ui_contract.py tests/unit/test_tui.py tests/integration/test_htmx_dashboard.py` - All checks passed

## User Setup Required

None - no external service configuration required.

## Next Phase Readiness

UI-01 is implemented and verified. Phase 13 Plan 02 can build on the contract to classify WUI artifacts, fix static hotkeys, and add stale-pattern guards for UI-02.

## Self-Check: PASSED

- Summary file and all key created/modified files exist on disk.
- Task commits verified: `0ba98db`, `7bee441`, `ea981c6`, `3938ddf`, `9e0274c`, `10053a6`.

---
*Phase: 13-canonical-ui-parity-contract*
*Completed: 2026-05-11*
