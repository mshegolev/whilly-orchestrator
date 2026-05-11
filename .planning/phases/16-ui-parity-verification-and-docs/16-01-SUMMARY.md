---
phase: 16-ui-parity-verification-and-docs
plan: "01"
subsystem: docs
tags: [ui-parity, docs, tests]
requirements-completed: [QA-01, QA-02]
completed: 2026-05-11
---

# Phase 16 Plan 01: UI Parity Verification And Docs Summary

Locked the fixed TUI/WUI contract with docs and regression tests.

## Files Created/Modified

- `docs/Getting-Started.md` - Updated quickstart dashboard hotkey copy.
- `docs/Whilly-Usage.md` - Added operator dashboard parity section and current keyboard shortcuts.
- `docs/CODEX-MISSION.md` - Added current v1.1 UI parity evidence and fragment boundaries.
- `tests/unit/test_ui_parity_docs.py` - Added docs regression tests.

## Verification

- `python3 -m pytest -q tests/unit/test_ui_parity_docs.py tests/unit/test_wui_contract_static.py tests/unit/test_tui.py tests/unit/test_operator_views.py tests/integration/test_htmx_dashboard.py::test_dashboard_mirrors_operator_surfaces_and_hotkeys tests/integration/test_htmx_dashboard.py::test_logs_fragment_is_routeable_noncanonical_artifact --maxfail=1` - 37 passed, 2 warnings.
- `python3 -m ruff check tests/unit/test_ui_parity_docs.py tests/unit/test_tui.py tests/unit/test_wui_contract_static.py whilly/operator_views.py` - All checks passed.

## Self-Check

- Active surfaces, hotkeys, and routes are documented.
- Noncanonical/quarantined fragments are documented.
- Focused verification avoids unrelated Docker/testcontainers baseline.
