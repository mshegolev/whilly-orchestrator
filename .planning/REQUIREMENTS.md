# Requirements: Whilly Orchestrator v1.1 UI Parity Completion

**Defined:** 2026-05-11
**Core Value:** Operators can safely coordinate AI-assisted engineering work with auditable state, human control, and verification before claiming success.

## v1.1 Requirements

### Canonical UI Contract

- [x] **UI-01**: Operator can rely on one canonical surface/action contract shared by
  `whilly/operator_views.py`, `whilly/cli/tui.py`, `whilly/api/templates/index.html.j2`, and active
  WUI hotkey code.
- [x] **UI-02**: Repository tests fail when WUI templates or static files reference non-canonical
  surfaces, hotkeys, selectors, or server routes.

### WUI Methods And Fragments

- [ ] **WUI-01**: Operator can use WUI keyboard shortcuts with current DOM selectors and supported
  `/api/v1/admin/*` worker endpoints; active WUI code does not post to stale `/admin/workers/*`
  routes.
- [ ] **WUI-02**: Operator can reach every server-rendered WUI fragment that remains active in the
  repository from visible WUI navigation, or the fragment is explicitly quarantined from active UI
  scope.
- [ ] **WUI-03**: Operator can use logs, admin, and PRD UI capabilities only when every visible
  control has a matching backend method, auth behavior, and integration test.

### Version Updates

- [x] **UPD-01**: Operator can run a non-mutating version check that reports the installed Whilly
  version, latest available version, update availability, and a clear message when the package index
  cannot be reached.
- [x] **UPD-02**: Operator can request a manual update that uses the detected supported installer
  path, supports dry-run output, and fails safely with copy-pastable guidance when the install
  context is unsupported.
- [x] **UPD-03**: Operator can opt into automatic update checks or automatic updates through an
  explicit setting or flag; the default behavior never silently mutates the installation.
- [x] **UPD-04**: Update behavior is covered by unit tests with mocked package-index and subprocess
  boundaries, including newer-version, up-to-date, network failure, manual dry-run, manual apply,
  and auto-update policy cases.

### Feedback Reporting

- [x] **FEED-01**: Operator can create a GitHub issue from Whilly for a bug or idea report using an
  explicit CLI command.
- [x] **FEED-02**: Feedback reports include Whilly/runtime context and redact known secret patterns
  before they are sent to GitHub.
- [x] **FEED-03**: Feedback issue creation supports dry-run output and is covered by unit tests with
  mocked GitHub CLI execution.

### TUI Parity

- [ ] **TUI-01**: Operator can access the same canonical user-interface capabilities from TUI that
  are exposed in active WUI navigation.
- [ ] **TUI-02**: TUI help text and hotkeys match WUI for canonical shared actions, including
  surface switching, filter, refresh, quit, worker pause/resume, and review decisions.

### Verification

- [ ] **QA-01**: Focused tests verify TUI/WUI parity, WUI route coverage, and absence of stale
  `/admin/*` actions or disconnected `1-7` hotkey assumptions.
- [ ] **QA-02**: Planning and documentation evidence record every intentional WUI-only or TUI-only
  exclusion with a reason and a regression test.

## v2 Requirements

### Future Capability

- **A11Y-01**: Browser and assistive-technology QA verifies the complete WUI operator workflow.
- **UIEXT-01**: Additional operator modules beyond logs, admin, and PRD can be added through the
  canonical UI contract without bespoke per-surface wiring.

## Out of Scope

| Feature | Reason |
|---------|--------|
| Broad visual redesign | v1.1 fixes correctness and parity of existing UI artifacts, not a new look. |
| Replacing the Jinja/HTMX dashboard or Rich TUI stack | The gap is route, hotkey, and method parity inside the existing architecture. |
| New Slack, repository, or source-management product scope unrelated to the current WUI partials | v1.1 only wires or quarantines capabilities already present in the pulled WUI artifacts. |
| Full browser/screen-reader certification | Deferred to future QA; v1.1 adds focused regression coverage first. |

## Traceability

| Requirement | Phase | Status |
|-------------|-------|--------|
| UI-01 | Phase 13 | Complete |
| UI-02 | Phase 13 | Complete |
| UPD-01 | Phase 13.1 | Complete |
| UPD-02 | Phase 13.1 | Complete |
| UPD-03 | Phase 13.1 | Complete |
| UPD-04 | Phase 13.1 | Complete |
| FEED-01 | Phase 13.2 | Complete |
| FEED-02 | Phase 13.2 | Complete |
| FEED-03 | Phase 13.2 | Complete |
| WUI-01 | Phase 14 | Pending |
| WUI-02 | Phase 14 | Pending |
| WUI-03 | Phase 14 | Pending |
| TUI-01 | Phase 15 | Pending |
| TUI-02 | Phase 15 | Pending |
| QA-01 | Phase 16 | Pending |
| QA-02 | Phase 16 | Pending |

**Coverage:**
- v1.1 requirements: 16 total
- Mapped to phases: 16
- Unmapped: 0

---
*Requirements defined: 2026-05-11*
*Last updated: 2026-05-11 after completing Phase 13.2 feedback reporter*
