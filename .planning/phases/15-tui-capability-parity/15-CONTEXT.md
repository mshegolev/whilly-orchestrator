# Phase 15: TUI Capability Parity - Context

**Gathered:** 2026-05-11
**Status:** Ready for planning
**Source:** `$gsd-autonomous` smart discuss defaults

<domain>
## Phase Boundary

Phase 15 proves that the TUI matches every active WUI navigation capability. It does not add logs,
admin, or PRD as new TUI surfaces because Phase 14 kept those WUI fragments noncanonical or
quarantined.
</domain>

<decisions>
## Implementation Decisions

### Active Capability Parity
- TUI surfaces must match the canonical active WUI navigation surfaces from `operator_surface_items()`.
- TUI surface hotkeys must continue to derive from `operator_surface_hotkeys()`.
- TUI help text must list the same shared hotkeys as active WUI.

### Explicit Exclusions
- `_logs.html` is routeable-noncanonical and is not a TUI surface yet.
- `_admin.html` and `_prd.html` are quarantined inactive WUI artifacts, so the TUI must not expose
  placeholder capabilities for them.
- Expanding any of these into active operator capabilities needs a future phase with backend routes
  and matching TUI/WUI tests.

### Claude's Discretion
- The parity assertion can be a focused regression test rather than new runtime metadata, because
  the existing shared contract already drives both media.
</decisions>

<canonical_refs>
## Canonical References

**Downstream agents MUST read these before planning or implementing.**

- `whilly/cli/tui.py` - TUI surface rendering, help copy, and hotkey handling.
- `whilly/operator_views.py` - Shared surface/action/artifact contract.
- `tests/unit/test_tui.py` - TUI parity and hotkey tests.
- `tests/unit/test_wui_contract_static.py` - Active/nonactive WUI artifact boundary.
- `.planning/phases/14-wui-method-and-fragment-wiring/14-CONTEXT.md` - Fragment policy decisions.
</canonical_refs>

<code_context>
## Existing Code Insights

### Reusable Assets
- `operator_surface_items()` and `operator_surface_hotkeys()` already provide the active navigation
  contract.

### Established Patterns
- TUI tests render Rich output to plain text and assert labels/hotkeys.
- Nonactive WUI artifacts are tested through `operator_wui_artifacts()`.

### Integration Points
- `tui_module._SURFACE_BY_KEY` is derived from `operator_surface_hotkeys()`.
- TUI header renders the same canonical surface labels in numeric order.
</code_context>

<specifics>
## Specific Ideas

- Add a test that compares TUI key mapping/rendered labels to active WUI navigation surfaces.
- Assert logs/admin/PRD are explicit nonactive WUI artifacts and absent from rendered TUI output.
</specifics>

<deferred>
## Deferred Ideas

- Add canonical logs/admin/PRD surfaces only in a future phase that wires backend routes and TUI/WUI
  behavior together.
</deferred>

---
*Phase: 15-tui-capability-parity*
*Context gathered: 2026-05-11 via `$gsd-autonomous`*
