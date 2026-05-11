# Phase 14: WUI Method And Fragment Wiring - Context

**Gathered:** 2026-05-11
**Status:** Ready for planning
**Source:** `$gsd-autonomous` smart discuss defaults

<domain>
## Phase Boundary

Phase 14 closes WUI-side method and fragment correctness. It does not expand the canonical
five-surface operator UI. The expected outcome is that active WUI code only uses current DOM/API
contracts, routeable fragments have backend coverage, and unsupported pulled fragments are
quarantined from active navigation.
</domain>

<decisions>
## Implementation Decisions

### Fragment Policy
- Keep the canonical WUI navigation to the existing five surfaces: Overview, Compliance,
  Plans/Tasks, Workers, Events.
- Keep `_logs.html` routeable through `?fragment=logs` because `dashboard_logs()` exists and is
  covered, but keep it out of canonical navigation until TUI parity expands.
- Keep `_admin.html` and `_prd.html` inactive/quarantined because they still reference unsupported
  `/admin/*` and `/prd/*` routes.

### Active WUI Contract
- Active WUI hotkeys must use `data-surface-tab` and the canonical `1-5` surface range.
- Active WUI worker controls must post only to `/api/v1/admin/workers/*`.
- Active dashboard navigation must not link to noncanonical logs/admin/PRD fragments.

### Claude's Discretion
- Exact wording for artifact quarantine reasons can be concise as long as tests pin the active vs
  nonactive boundary.
</decisions>

<canonical_refs>
## Canonical References

**Downstream agents MUST read these before planning or implementing.**

### WUI contract and fragments
- `whilly/operator_views.py` - WUI artifact status contract and route/selectors.
- `whilly/api/dashboard.py` - Dashboard fragment routing and logs fragment renderer.
- `whilly/api/templates/index.html.j2` - Active WUI navigation and dashboard hotkeys.
- `whilly/api/templates/_logs.html` - Routeable noncanonical logs fragment.
- `whilly/api/templates/_admin.html` - Quarantined admin fragment.
- `whilly/api/templates/_prd.html` - Quarantined PRD fragment.
- `whilly/api/static/whilly-hotkeys.js` - Active static hotkey file.

### Tests
- `tests/unit/test_wui_contract_static.py` - Static WUI artifact classification and stale-pattern
  guards.
- `tests/integration/test_htmx_dashboard.py` - Rendered WUI and logs fragment coverage.
</canonical_refs>

<code_context>
## Existing Code Insights

### Reusable Assets
- `operator_wui_artifacts()` already classifies active, routeable-noncanonical, and quarantined
  artifacts.
- `render_dashboard()` already routes `?fragment=logs` to `dashboard_logs()`.

### Established Patterns
- Active WUI contract checks live in unit/static tests and rendered HTMX integration tests.
- Unsupported pulled artifacts are not deleted; they are explicit nonactive artifacts.

### Integration Points
- Artifact status and reasons are consumed by tests, not by runtime navigation.
- Runtime navigation remains driven by `operator_surface_items()`.
</code_context>

<specifics>
## Specific Ideas

- Move Phase 14 follow-up labels off artifacts once their Phase 14 classification is resolved.
- Add a regression check that active dashboard navigation does not link to noncanonical fragments.
</specifics>

<deferred>
## Deferred Ideas

- Expanding logs into canonical WUI/TUI navigation belongs to Phase 15 or later.
- Wiring full admin/PRD WUI modules requires supported backend routes and matching TUI parity.
</deferred>

---
*Phase: 14-wui-method-and-fragment-wiring*
*Context gathered: 2026-05-11 via `$gsd-autonomous`*
