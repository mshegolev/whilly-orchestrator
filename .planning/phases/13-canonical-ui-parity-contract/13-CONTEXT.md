# Phase 13: Canonical UI Parity Contract - Context

**Gathered:** 2026-05-11
**Status:** Ready for planning
**Source:** `$gsd-new-milestone` prompt and post-pull TUI/WUI gap audit

<domain>
## Phase Boundary

Phase 13 defines the canonical operator UI contract that later phases will implement. It should not
wire every WUI fragment or add every TUI surface directly. Its job is to make the source of truth and
regression guard explicit enough that WUI/TUI drift becomes test-visible.

The known post-pull gap is:
- Active operator dashboard currently matches TUI on five surfaces: `Overview`, `Compliance`,
  `Plans/Tasks`, `Workers`, `Events`.
- `whilly/api/static/whilly-hotkeys.js` describes `1-7` tabs, queries `.tabs [data-key]`, and posts
  to stale `/admin/workers/*` routes.
- `_admin.html`, `_prd.html`, and `_logs.html` were added as WUI partials, but they are not aligned
  with the active TUI/WUI surface contract. `_logs.html` is routeable by `?fragment=logs`; `_admin.html`
  and `_prd.html` contain routes that are not active server routes.
</domain>

<decisions>
## Implementation Decisions

### Canonical Contract
- Keep one explicit canonical list of active operator UI surfaces and shared actions.
- The contract must be checkable from tests against TUI, WUI templates, and active WUI JavaScript.
- The contract should cover surfaces, hotkeys, DOM selectors, and route prefixes used by active UI
  controls.

### Stale Artifact Handling
- Active WUI code must not reference stale `1-7` switching unless the canonical contract is expanded
  to seven surfaces in the same phase.
- Active WUI code must not post to `/admin/workers/*`; worker controls use the supported
  `/api/v1/admin/workers/*` API.
- WUI partials that are not ready to be active must be explicitly marked inactive/quarantined, not
  silently left as reachable dead controls.

### Claude's Discretion
- The exact shape of the contract can be a Python data structure, tests around existing constants,
  or a small helper API, as long as downstream phases can reuse it without duplicating lists.
- The plan may choose whether to fix `whilly-hotkeys.js` in Phase 13 or only pin the failing
  contract and defer implementation to Phase 14, but it must make the handoff explicit.
</decisions>

<canonical_refs>
## Canonical References

**Downstream agents MUST read these before planning or implementing.**

### Current shared operator contract
- `whilly/operator_views.py` - Current operator surfaces, table labels, and shared view metadata.
- `whilly/cli/tui.py` - TUI surface key mapping, help text, and operator actions.
- `whilly/api/templates/index.html.j2` - Active WUI dashboard tabs, inline hotkeys, and API calls.

### Pulled WUI artifacts to classify
- `whilly/api/static/whilly-hotkeys.js` - Stale static hotkey assumptions to fix or quarantine.
- `whilly/api/templates/_admin.html` - Admin partial with currently unsupported route references.
- `whilly/api/templates/_logs.html` - Logs partial routeable through `?fragment=logs` but not in
  canonical nav/TUI.
- `whilly/api/templates/_prd.html` - PRD partial with currently unsupported route references.

### Existing parity tests
- `tests/unit/test_operator_views.py` - Shared operator surface and snapshot assertions.
- `tests/unit/test_tui.py` - TUI surface/help/hotkey assertions.
- `tests/integration/test_htmx_dashboard.py` - WUI dashboard, hotkey, and admin route assertions.
</canonical_refs>

<specifics>
## Specific Ideas

- Add a regression test that fails if active WUI files contain `/admin/workers/`.
- Add a regression test that fails if active WUI files contain `1-7` switching while the canonical
  surface count is five.
- Add a regression test that compares WUI tab labels and TUI surface labels through
  `operator_surface_items()`.
- Add an explicit inactive/quarantine marker for WUI partials that are intentionally not in active
  navigation yet.
</specifics>

<deferred>
## Deferred Ideas

- Implementing full logs/admin/PRD server methods belongs to Phase 14.
- Adding matching TUI screens/commands for expanded capabilities belongs to Phase 15.
- Browser and assistive-technology QA belongs to a later QA milestone unless explicitly pulled into
  Phase 16.
</deferred>

---

*Phase: 13-canonical-ui-parity-contract*
*Context gathered: 2026-05-11 via `$gsd-new-milestone`*
