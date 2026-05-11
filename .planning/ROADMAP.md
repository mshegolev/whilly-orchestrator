# Roadmap: Whilly Orchestrator

## Overview

GSD is the canonical high-level execution plan for Whilly. v1.0 is shipped and archived under
`.planning/milestones/`. The active v1.1 milestone closes the post-v1.0 WUI/TUI interface gap found
after pulling `feat(wui): adopt 90s/TUI design system (#270)`.

## Current Milestone: v1.1 UI parity completion

Close inactive WUI artifacts, stale routes, and missing UI methods so every active operator
interface path is canonical, reachable, and verified across TUI and WUI. Phases 13.1 and 13.2 are
urgent inserted product lifecycle additions for version updates and GitHub feedback reporting.
Phase 17 adds the Jira-driven operator workflow foundation: classification, one-shot polling,
state persistence, repo hints, and code/test readiness gates.

## Phases

- [x] **Phase 13: Canonical UI parity contract** - Define the shared source of truth for surfaces, (completed 2026-05-11)
  hotkeys, actions, routes, and orphan-artifact checks.
- [x] **Phase 13.1: Version update checks and manual/automatic update modes (INSERTED)** - Add (completed 2026-05-11)
  classic package-update behavior: check for newer versions, run a manual update, and support an
  explicit automatic update policy.
- [x] **Phase 13.2: GitHub feedback issue reporter (INSERTED)** - Add (completed 2026-05-11)
  a fast `whilly feedback` command for explicit GitHub bug/idea reports.
- [x] **Phase 14: WUI method and fragment wiring** - Make active WUI static/templates use current (completed 2026-05-11)
  DOM/API contracts and wire or quarantine logs/admin/PRD fragments.
- [x] **Phase 15: TUI capability parity** - Add or adjust TUI surfaces/help so it matches every (completed 2026-05-11)
  canonical WUI capability.
- [x] **Phase 16: UI parity verification and docs** - Add focused regression coverage and update (completed 2026-05-11)
  operator-facing planning/docs evidence.
- [x] **Phase 17: Jira work classification and code readiness routing** - Classify incoming Jira (completed 2026-05-11)
  work, route it through the correct operator flow, reread GitLab links, and block autonomous work
  until code/test readiness is known.

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

### Phase 13.2: GitHub feedback issue reporter (INSERTED)

**Goal:** Give operators a fast, explicit support channel to create GitHub bug or idea issues from
Whilly while testing the package on another computer.
**Requirements**: FEED-01, FEED-02, FEED-03
**Depends on:** Phase 13.1
**Plans:** 1/1 plans complete
**Canonical refs**: `whilly/feedback.py`, `whilly/cli/feedback.py`, `whilly/cli/__init__.py`,
`whilly/gh_utils.py`, `tests/unit/test_feedback.py`, `tests/unit/test_feedback_cli.py`
**Success Criteria** (what must be TRUE):
  1. `whilly feedback` can create a GitHub issue for `bug` or `idea` reports.
  2. Feedback report bodies include Whilly/runtime context and redact known secret patterns.
  3. Dry-run prints the `gh issue create` command without creating an issue.

Plans:
- [x] 13.2-01-PLAN.md - Implement GitHub feedback issue reporter.

### Phase 14: WUI method and fragment wiring
**Goal**: Ensure every active WUI fragment/control has a current server method, supported route, auth behavior, and test.
**Depends on**: Phase 13
**Requirements**: WUI-01, WUI-02, WUI-03
**Plans:** 1/1 plans complete
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

Plans:
- [x] 14-01-PLAN.md - Update WUI artifact classifications and active navigation guards.

### Phase 15: TUI capability parity
**Goal**: Match TUI surfaces, commands, and help text to the canonical active WUI capabilities.
**Depends on**: Phase 14
**Requirements**: TUI-01, TUI-02
**Plans:** 1/1 plans complete
**Canonical refs**: `whilly/cli/tui.py`, `whilly/operator_views.py`,
`whilly/log_viewer.py`, `whilly/prd_launcher.py`, `whilly/prd_generator.py`,
`whilly/cli/admin.py`, `tests/unit/test_tui.py`
**Success Criteria** (what must be TRUE):
  1. Every active WUI navigation surface has a TUI equivalent or an explicit tested exclusion.
  2. TUI help text lists the same canonical shared hotkeys as WUI.
  3. TUI commands for logs/admin/PRD capabilities call existing domain/CLI services instead of
     duplicating backend behavior.
  4. TUI state transitions remain deterministic and existing expert review hotkeys keep working.

Plans:
- [x] 15-01-PLAN.md - Pin TUI active navigation parity and noncanonical fragment exclusions.

### Phase 16: UI parity verification and docs
**Goal**: Lock the fixed TUI/WUI contract with focused tests and concise documentation evidence.
**Depends on**: Phase 15
**Requirements**: QA-01, QA-02
**Plans:** 1/1 plans complete
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

Plans:
- [x] 16-01-PLAN.md - Update UI parity docs and add docs regression coverage.

### Phase 17: Jira work classification and code readiness routing

**Goal:** Make Jira-driven work safe to operate by classifying incoming issues, choosing the right
workflow, persisting task history, rereading GitLab/Jira links, and proving code/test readiness
before autonomous workers run.
**Depends on:** Phase 16
**Requirements**: JIRA-01, JIRA-02, JIRA-03, JIRA-04, JIRA-05, JIRA-06, JIRA-07
**Plans:** 5/5 plans complete
**Canonical refs**: `whilly/cli/jira.py`, `whilly/sources/jira.py`,
`whilly/qa_release/collector.py`, `whilly/qa_release/models.py`,
`docs/superpowers/specs/2026-05-11-jira-intake-system-design.md`,
`.planning/phases/17-jira-work-classification-and-code-readiness-routing/17-CONTEXT.md`
**Success Criteria** (what must be TRUE):
  1. Incoming Jira issues are classified as `feature`, `bug`, `task`, or `devops`, with `hotfix`
     stored as an urgency overlay instead of a separate work kind.
  2. Each classification maps to an explicit flow: feature PRD/acceptance, bug reproduction and
     regression test, hotfix risk/rollback/smoke, task checklist, or DevOps environment/dry-run
     verification.
  3. Jira watch/intake rereads description, comments, changelog, issue links, and remote links, and
     persists task history/state in Postgres.
  4. GitLab links from Jira are normalized into repo/ref/MR/pipeline hints and reconciled with
     selected `repo_targets`.
  5. A read-only code readiness probe checks linked repositories for relevant code context, unit
     tests, and verification commands before Whilly proposes or starts autonomous execution.
  6. Jira comments can approve, reclassify, ask/answer questions, continue, replan, or cancel work
     through an auditable command protocol.
  7. Focused tests cover classification confidence, routing decisions, Jira refresh deltas, GitLab
     link parsing, readiness verdicts, and no-unit-tests gates.

Plans:
- [x] 17-01-PLAN.md - Implement work classification model and routing profiles.
- [x] 17-02-PLAN.md - Add Jira watch session state and comment command protocol.
- [x] 17-03-PLAN.md - Reuse Jira/GitLab link collection for refresh deltas and repo hints.
- [x] 17-04-PLAN.md - Add read-only code readiness and unit-test detection.
- [x] 17-05-PLAN.md - Wire CLI/operator documentation and end-to-end tests.

## Progress

**Execution Order:**
Phases execute in numeric order. v1.1 continues after archived v1.0 Phase 12.

| Phase | Plans Complete | Status | Completed |
|-------|----------------|--------|-----------|
| 13. Canonical UI parity contract | 2/2 | Complete   | 2026-05-11 |
| 13.1. Version update checks and manual/automatic update modes | 1/1 | Complete | 2026-05-11 |
| 13.2. GitHub feedback issue reporter | 1/1 | Complete | 2026-05-11 |
| 14. WUI method and fragment wiring | 1/1 | Complete | 2026-05-11 |
| 15. TUI capability parity | 1/1 | Complete | 2026-05-11 |
| 16. UI parity verification and docs | 1/1 | Complete | 2026-05-11 |
| 17. Jira work classification and code readiness routing | 5/5 | Complete | 2026-05-11 |

## Archives

- `.planning/milestones/v1.0-ROADMAP.md`
- `.planning/milestones/v1.0-REQUIREMENTS.md`
- `.planning/milestones/v1.0-MILESTONE-AUDIT.md`

## Deferred Scope

- Browser and assistive-technology QA for the full WUI operator workflow.
- New operator modules beyond the pulled logs/admin/PRD artifacts.
- Replacing the current Jinja/HTMX WUI or Rich TUI architecture.
