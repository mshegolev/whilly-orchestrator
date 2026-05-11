---
phase: 05-shared-operator-table-contract
plan: 01
subsystem: ui
tags: [operator-ui, tui, wui, table-contract, htmx, rich]

requires:
  - phase: 04-compact-operator-identity-panel
    provides: "Stable operator dashboard shell and identity controls"
provides:
  - "Pure shared operator surface and table-column metadata"
  - "TUI table headers rendered from shared metadata"
  - "WUI table headers and worker row order rendered from shared metadata"
  - "Contract tests pinning medium-specific label differences"
affects: [06-mobile-wui-row-actions, operator-dashboard, tui, wui]

tech-stack:
  added: []
  patterns:
    - "Pure operator table metadata in whilly.operator_views"
    - "Renderer contexts pass table-column metadata into Rich and Jinja surfaces"

key-files:
  created: []
  modified:
    - whilly/operator_views.py
    - whilly/cli/tui.py
    - whilly/api/dashboard.py
    - whilly/api/templates/index.html.j2
    - whilly/api/templates/_tasks_table.html
    - whilly/api/templates/_workers_table.html
    - tests/unit/test_operator_views.py
    - tests/unit/test_tui.py
    - tests/integration/test_htmx_dashboard.py

key-decisions:
  - "Operator table labels and field-key order are centralized in pure whilly.operator_views metadata."
  - "TUI keeps compact worker labels and omits task Updated; WUI uses canonical task and worker labels."

patterns-established:
  - "OperatorTableColumn defines field key, canonical label, medium label overrides, visibility, and medium notes."
  - "TUI and WUI render table headers from operator_table_columns instead of local label literals."

requirements-completed: [OPUI-07]

duration: 7 min
completed: 2026-05-08
---

# Phase 05 Plan 01: Shared Operator Table Contract Summary

**Shared operator table metadata now drives TUI and WUI task, worker, review, and event headers with tested medium-specific differences.**

## Performance

- **Duration:** 7 min
- **Started:** 2026-05-08T11:24:02Z
- **Completed:** 2026-05-08T11:31:17Z
- **Tasks:** 3 completed
- **Files modified:** 9

## Accomplishments

- Added pure `OperatorTable`, `OperatorTableColumn`, `operator_surface_items`, `operator_table_columns`, and `operator_table_labels` contracts in `whilly/operator_views.py`.
- Updated TUI rendering to consume shared surface labels and table columns while preserving hotkeys, selection, review decisions, pause/resume controls, and compact labels.
- Updated WUI dashboard context and Jinja templates to render task, worker, review, and event headers from shared metadata.
- Removed WUI label drift: tasks now show `Worker` instead of `Claimed by`, and workers render `Worker`, `Hostname`, `Owner`, `Status`, `Last heartbeat`.
- Added focused unit and integration tests pinning labels, field-key order, WUI worker row order, and intentional medium differences.

## Task Commits

Each task was committed atomically through TDD red/green commits:

1. **Task 1: Add pure shared table-column metadata**
   - `d8c2d51` test: add failing operator table metadata contract
   - `410c589` feat: add shared operator table metadata
2. **Task 2: Make the TUI consume the shared contract**
   - `de6a502` test: add failing TUI table contract tests
   - `f6b61c7` feat: render TUI tables from shared contract
3. **Task 3: Make the WUI consume the shared contract**
   - `0943795` test: add failing WUI table contract test
   - `8f9ca6b` feat: render WUI tables from shared contract

## Files Created/Modified

- `whilly/operator_views.py` - Pure shared surface/table metadata and label helpers.
- `whilly/cli/tui.py` - Rich table headers and surface tabs read shared metadata.
- `whilly/api/dashboard.py` - Dashboard context exposes shared surface labels and WUI table columns.
- `whilly/api/templates/index.html.j2` - Review and event headers/empty colspans use shared table columns.
- `whilly/api/templates/_tasks_table.html` - Task headers and empty colspan use shared table columns.
- `whilly/api/templates/_workers_table.html` - Worker headers, empty colspan, and row cell order follow the shared contract.
- `tests/unit/test_operator_views.py` - Pure contract tests for field keys, labels, and medium notes.
- `tests/unit/test_tui.py` - TUI rendered-header tests and metadata-source guard.
- `tests/integration/test_htmx_dashboard.py` - WUI table contract and worker row-order integration test.

## Decisions Made

- Centralized operator table label and field order metadata in `whilly.operator_views` instead of adding renderer-local constants.
- Kept documented medium differences only: TUI omits task `Updated` and uses compact `Host` / `Heartbeat`; WUI shows the canonical full labels.

## Deviations from Plan

None - plan executed exactly as written.

## Issues Encountered

None.

## Authentication Gates

None.

## Verification

- `.venv/bin/python -m pytest -q tests/unit/test_operator_views.py tests/unit/test_tui.py tests/integration/test_htmx_dashboard.py` - `50 passed in 6.36s`
- `.venv/bin/python -m ruff check whilly/operator_views.py whilly/cli/tui.py whilly/api/dashboard.py tests/unit/test_operator_views.py tests/unit/test_tui.py tests/integration/test_htmx_dashboard.py` - `All checks passed!`

## User Setup Required

None - no external service configuration required.

## Next Phase Readiness

Phase 6 mobile WUI row layout can rely on tested table labels, field-key order, and documented medium-specific differences instead of reverse-engineering renderer templates.

## Self-Check: PASSED

- Summary file exists at `.planning/phases/05-shared-operator-table-contract/05-01-SUMMARY.md`.
- All six TDD task commits are present in git history: `d8c2d51`, `410c589`, `de6a502`, `f6b61c7`, `0943795`, `8f9ca6b`.

---
*Phase: 05-shared-operator-table-contract*
*Completed: 2026-05-08*
