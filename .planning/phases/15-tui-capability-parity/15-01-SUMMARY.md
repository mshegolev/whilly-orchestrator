---
phase: 15-tui-capability-parity
plan: "01"
subsystem: tui
tags: [tui, wui, parity, operator-ui]
requirements-completed: [TUI-01, TUI-02]
completed: 2026-05-11
---

# Phase 15 Plan 01: TUI Capability Parity Summary

Locked TUI parity to the active WUI navigation contract and made logs/admin/PRD exclusions explicit.

## Files Modified

- `tests/unit/test_tui.py` - Added a regression test comparing TUI surfaces to active WUI
  navigation and asserting noncanonical fragments are not exposed as TUI capabilities.

## Decisions

- No new TUI surfaces were added because Phase 14 kept logs/admin/PRD outside active canonical
  navigation.
- TUI remains driven by the shared `operator_surface_items()` and `operator_surface_hotkeys()`
  contract.

## Verification

- `python3 -m pytest -q tests/unit/test_tui.py tests/unit/test_operator_views.py tests/unit/test_operator_ui_contract.py tests/unit/test_wui_contract_static.py --maxfail=1` - 34 passed, 2 warnings.
- `python3 -m ruff check tests/unit/test_tui.py whilly/cli/tui.py whilly/operator_views.py tests/unit/test_wui_contract_static.py` - All checks passed.

## Self-Check

- Every active WUI navigation surface has a rendered TUI equivalent.
- TUI help and key handling continue to derive from the shared contract.
- Noncanonical WUI fragments are explicitly excluded from TUI capability parity.
