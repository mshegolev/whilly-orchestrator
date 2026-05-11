# Phase 16: UI Parity Verification And Docs - Context

**Gathered:** 2026-05-11
**Status:** Ready for planning
**Source:** `$gsd-autonomous` smart discuss defaults

<domain>
## Phase Boundary

Phase 16 locks the completed UI parity work with focused tests and concise operator-facing
documentation. It should not add new UI capabilities; it records the active canonical surfaces,
hotkeys, routes, and explicit logs/admin/PRD exclusions.
</domain>

<decisions>
## Implementation Decisions

### Documentation Scope
- Update docs that still describe old TUI hotkeys.
- Document active WUI/TUI surfaces and the shared `1-5=switch` hotkey copy.
- State that `_logs.html` is routeable noncanonical and `_admin.html`/`_prd.html` are quarantined.

### Verification Scope
- Add a docs regression test so old `q/d/l/t/h` hotkeys do not drift back.
- Keep verification focused on operator UI unit/static tests plus targeted dashboard integration
  checks.

### Claude's Discretion
- Place the concise UI parity status in existing docs instead of creating a new standalone guide.
</decisions>

<canonical_refs>
## Canonical References

**Downstream agents MUST read these before planning or implementing.**

- `docs/Getting-Started.md` - Quickstart operator hotkey copy.
- `docs/Whilly-Usage.md` - Detailed operator dashboard parity and keyboard shortcut copy.
- `docs/CODEX-MISSION.md` - Current v1.1 UI parity evidence and boundaries.
- `tests/unit/test_ui_parity_docs.py` - Docs regression coverage.
- `tests/unit/test_tui.py` - TUI active parity coverage.
- `tests/unit/test_wui_contract_static.py` - Static WUI artifact coverage.
- `tests/integration/test_htmx_dashboard.py` - Rendered WUI coverage.
</canonical_refs>

<code_context>
## Existing Code Insights

### Reusable Assets
- UI parity facts come from the shared operator contract and Phase 14/15 verification.

### Established Patterns
- Docs tests read markdown directly and assert exact current-scope language.
- Focused phase verification can avoid repo-wide Docker/testcontainers baseline.

### Integration Points
- Docs must not imply logs/admin/PRD are canonical active WUI/TUI capabilities.
</code_context>

<specifics>
## Specific Ideas

- Replace stale `q/d/l/t/h` docs with current `q`, `r`, `R`, `1-5`, `/`, `p`, `j/k`, `a/x/c`.
- Pin the fragment boundary in `docs/CODEX-MISSION.md`.
</specifics>

<deferred>
## Deferred Ideas

- Browser/screen-reader QA remains deferred outside this focused parity verification phase.
</deferred>

---
*Phase: 16-ui-parity-verification-and-docs*
*Context gathered: 2026-05-11 via `$gsd-autonomous`*
