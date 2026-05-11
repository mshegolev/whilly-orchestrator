# Roadmap: Whilly Orchestrator

## Overview

GSD is the canonical high-level execution plan for Whilly. v1.0 is shipped and archived under
`.planning/milestones/`. The active v1.1 milestone closes the post-v1.0 WUI/TUI interface gap found
after pulling `feat(wui): adopt 90s/TUI design system (#270)`.

## Current Milestone: v1.1 UI parity completion

Close inactive WUI artifacts, stale routes, and missing UI methods so every active operator
interface path is canonical, reachable, and verified across TUI and WUI.

## Phases

- [ ] **Phase 13: Canonical UI parity contract** - Define the shared source of truth for surfaces,
  hotkeys, actions, routes, and orphan-artifact checks.
- [ ] **Phase 14: WUI method and fragment wiring** - Make active WUI static/templates use current
  DOM/API contracts and wire or quarantine logs/admin/PRD fragments.
- [ ] **Phase 15: TUI capability parity** - Add or adjust TUI surfaces/help so it matches every
  canonical WUI capability.
- [ ] **Phase 16: UI parity verification and docs** - Add focused regression coverage and update
  operator-facing planning/docs evidence.

## Phase Details

### Phase 13: Canonical UI parity contract
**Goal**: Establish one testable contract for operator UI surfaces, hotkeys, actions, selectors, and routes.
**Depends on**: v1.0 archive
**Requirements**: UI-01, UI-02
**Canonical refs**: `.planning/REQUIREMENTS.md`, `whilly/operator_views.py`,
`whilly/cli/tui.py`, `whilly/api/templates/index.html.j2`,
`whilly/api/static/whilly-hotkeys.js`, `tests/unit/test_tui.py`,
`tests/integration/test_htmx_dashboard.py`
**Success Criteria** (what must be TRUE):
  1. A shared contract names every canonical active UI surface and action.
  2. TUI, WUI templates, and active WUI hotkey code consume or are tested against that contract.
  3. Tests fail for stale `1-7` surface switching, stale `.tabs [data-key]` selectors, and stale
     `/admin/workers/*` actions in active WUI code.
  4. Orphan WUI templates/static files are either covered by the contract or explicitly marked
     inactive.

### Phase 14: WUI method and fragment wiring
**Goal**: Ensure every active WUI fragment/control has a current server method, supported route, auth behavior, and test.
**Depends on**: Phase 13
**Requirements**: WUI-01, WUI-02, WUI-03
**Canonical refs**: `whilly/api/dashboard.py`, `whilly/adapters/transport/server.py`,
`whilly/api/templates/_admin.html`, `whilly/api/templates/_logs.html`,
`whilly/api/templates/_prd.html`, `whilly/api/templates/index.html.j2`,
`whilly/api/static/whilly-hotkeys.js`
**Success Criteria** (what must be TRUE):
  1. Active WUI hotkeys use `data-surface-tab` and the canonical surface range.
  2. Active WUI worker controls post only to supported `/api/v1/admin/workers/*` endpoints.
  3. Logs, admin, and PRD fragments are reachable from visible navigation only when their backend
     methods exist and are covered by integration tests.
  4. Any fragment not ready for active use is quarantined from navigation and documented as inactive
     rather than silently shipping dead controls.

### Phase 15: TUI capability parity
**Goal**: Match TUI surfaces, commands, and help text to the canonical active WUI capabilities.
**Depends on**: Phase 14
**Requirements**: TUI-01, TUI-02
**Canonical refs**: `whilly/cli/tui.py`, `whilly/operator_views.py`,
`whilly/log_viewer.py`, `whilly/prd_launcher.py`, `whilly/prd_generator.py`,
`whilly/cli/admin.py`, `tests/unit/test_tui.py`
**Success Criteria** (what must be TRUE):
  1. Every active WUI navigation surface has a TUI equivalent or an explicit tested exclusion.
  2. TUI help text lists the same canonical shared hotkeys as WUI.
  3. TUI commands for logs/admin/PRD capabilities call existing domain/CLI services instead of
     duplicating backend behavior.
  4. TUI state transitions remain deterministic and existing expert review hotkeys keep working.

### Phase 16: UI parity verification and docs
**Goal**: Lock the fixed TUI/WUI contract with focused tests and concise documentation evidence.
**Depends on**: Phase 15
**Requirements**: QA-01, QA-02
**Canonical refs**: `.planning/REQUIREMENTS.md`, `.planning/ROADMAP.md`,
`tests/unit/test_operator_views.py`, `tests/unit/test_tui.py`,
`tests/integration/test_htmx_dashboard.py`, `tests/integration/test_control_state_admin_api.py`,
`docs/CODEX-MISSION.md`
**Success Criteria** (what must be TRUE):
  1. Focused unit and integration tests cover TUI/WUI surface parity, route coverage, and active
     WUI hotkeys.
  2. Static regression tests reject stale `/admin/*` actions and disconnected hotkey assumptions in
     active WUI files.
  3. Documentation or phase verification states which UI capabilities are active, quarantined, or
     intentionally medium-specific.
  4. The milestone can be verified without relying on unrelated Docker-backed or repo-wide baseline
     tests.

## Progress

**Execution Order:**
Phases execute in numeric order. v1.1 continues after archived v1.0 Phase 12.

| Phase | Plans Complete | Status | Completed |
|-------|----------------|--------|-----------|
| 13. Canonical UI parity contract | 0/0 | Pending | - |
| 14. WUI method and fragment wiring | 0/0 | Pending | - |
| 15. TUI capability parity | 0/0 | Pending | - |
| 16. UI parity verification and docs | 0/0 | Pending | - |

## Archives

- `.planning/milestones/v1.0-ROADMAP.md`
- `.planning/milestones/v1.0-REQUIREMENTS.md`
- `.planning/milestones/v1.0-MILESTONE-AUDIT.md`

## Deferred Scope

- Browser and assistive-technology QA for the full WUI operator workflow.
- New operator modules beyond the pulled logs/admin/PRD artifacts.
- Replacing the current Jinja/HTMX WUI or Rich TUI architecture.
