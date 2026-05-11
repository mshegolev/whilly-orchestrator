# Roadmap: Whilly Orchestrator

## Overview

GSD is the canonical high-level execution plan for Whilly. v1.0 is shipped and archived under
`.planning/milestones/`. The active v1.1 milestone closes the post-v1.0 WUI/TUI interface gap found
after pulling `feat(wui): adopt 90s/TUI design system (#270)`.

## Current Milestone: v1.1 UI parity completion

Close inactive WUI artifacts, stale routes, and missing UI methods so every active operator
interface path is canonical, reachable, and verified across TUI and WUI. Phase 13.1 is an urgent
inserted product lifecycle addition for version update checks and safe package updates before
continuing WUI/TUI parity work.

## Phases

- [x] **Phase 13: Canonical UI parity contract** - Define the shared source of truth for surfaces, (completed 2026-05-11)
  hotkeys, actions, routes, and orphan-artifact checks.
- [x] **Phase 13.1: Version update checks and manual/automatic update modes (INSERTED)** - Add (completed 2026-05-11)
  classic package-update behavior: check for newer versions, run a manual update, and support an
  explicit automatic update policy.
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
**Plans:** 2/2 plans complete
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

Plans:
- [x] 13-01-PLAN.md - Define shared operator UI surface/action contract and make active TUI/WUI consume it.
- [x] 13-02-PLAN.md - Classify WUI artifacts, fix static hotkeys, and add static/rendered stale-pattern guards.

### Phase 13.1: Version update checks and manual/automatic update modes (INSERTED)

**Goal:** Give operators a safe, explicit way to detect newer Whilly releases and either update
manually or opt into automatic update behavior that follows classic package-manager expectations.
**Requirements**: UPD-01, UPD-02, UPD-03, UPD-04
**Depends on:** Phase 13
**Plans:** 1/1 plans complete
**Canonical refs**: `pyproject.toml`, `whilly/__init__.py`, `whilly/cli/__init__.py`,
`whilly/cli/__main__.py`, `whilly/cli/*`, `tests/unit/`
**Success Criteria** (what must be TRUE):
  1. A CLI command can report the installed Whilly version, the latest available version, and
     whether an update is available without mutating the environment.
  2. A manual update command performs an explicit operator-requested package update, with dry-run
     and clear command/error output for pip, pipx, or unsupported install contexts.
  3. Automatic update mode is opt-in, configurable, and constrained by policy so Whilly never
     silently upgrades itself during unrelated commands.
  4. Tests cover newer, current, unavailable-network, unsupported-installer, manual-update, and
     auto-update policy paths without depending on live package indexes.

Plans:
- [x] 13.1-01-PLAN.md - Implement version check, manual update, and opt-in auto-update modes.

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
| 13. Canonical UI parity contract | 2/2 | Complete   | 2026-05-11 |
| 13.1. Version update checks and manual/automatic update modes | 1/1 | Complete | 2026-05-11 |
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
