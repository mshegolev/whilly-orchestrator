---
phase: 14-wui-method-and-fragment-wiring
plan: "01"
subsystem: wui
tags: [wui, fragments, routes, parity]
requirements-completed: [WUI-01, WUI-02, WUI-03]
completed: 2026-05-11
---

# Phase 14 Plan 01: WUI Method And Fragment Wiring Summary

Closed the WUI fragment boundary without expanding the canonical five-surface dashboard.

## Files Modified

- `whilly/operator_views.py` - Updated artifact status reasons so Phase 14 no longer points to
  itself as an unresolved follow-up.
- `tests/unit/test_wui_contract_static.py` - Added a regression check that active dashboard
  navigation does not expose routeable-noncanonical or quarantined fragments.

## Decisions

- `_logs.html` remains routeable through `?fragment=logs` because the backend renderer exists and is
  integration-tested, but it stays outside canonical navigation until TUI parity expands.
- `_admin.html` and `_prd.html` remain inactive/quarantined because their routes are not supported
  active WUI/API routes.
- The active WUI dashboard continues to expose exactly the canonical five surfaces.

## Verification

- `python3 -m pytest -q tests/unit/test_wui_contract_static.py tests/integration/test_htmx_dashboard.py::test_logs_fragment_is_routeable_noncanonical_artifact tests/integration/test_htmx_dashboard.py::test_dashboard_mirrors_operator_surfaces_and_hotkeys --maxfail=1` - 8 passed, 2 warnings.
- `python3 -m ruff check whilly/operator_views.py tests/unit/test_wui_contract_static.py` - All checks passed.

## Self-Check

- Active hotkeys and worker routes were already canonical and remain covered.
- Noncanonical fragments are not visible active dashboard navigation.
- Unsupported admin/PRD controls are documented as quarantined instead of silently shipped.
