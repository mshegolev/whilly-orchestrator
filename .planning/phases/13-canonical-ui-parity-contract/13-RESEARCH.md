# Phase 13: Canonical UI Parity Contract - Research

**Researched:** 2026-05-11
**Domain:** Whilly operator UI contract across Rich TUI, Jinja/HTMX WUI, static WUI JavaScript, and WUI artifact classification
**Confidence:** HIGH

<user_constraints>
## User Constraints (from CONTEXT.md)

### Phase Boundary

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

### Locked Decisions

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

### Deferred Ideas (OUT OF SCOPE)

## Deferred Ideas

- Implementing full logs/admin/PRD server methods belongs to Phase 14.
- Adding matching TUI screens/commands for expanded capabilities belongs to Phase 15.
- Browser and assistive-technology QA belongs to a later QA milestone unless explicitly pulled into
  Phase 16.
</user_constraints>

<phase_requirements>
## Phase Requirements

| ID | Description | Research Support |
|----|-------------|------------------|
| UI-01 | Operator can rely on one canonical surface/action contract shared by `whilly/operator_views.py`, `whilly/cli/tui.py`, `whilly/api/templates/index.html.j2`, and active WUI hotkey code. | Extend the existing `operator_views` pure metadata pattern from surfaces/tables to actions, hotkeys, selectors, route prefixes, and artifact status; then make TUI/WUI renderers consume or get tested against it. |
| UI-02 | Repository tests fail when WUI templates or static files reference non-canonical surfaces, hotkeys, selectors, or server routes. | Add static unit tests over active WUI templates/JS and contract-aware artifact tests that fail on stale `1-7`, `.tabs [data-key]`, and `/admin/workers/*` in active files, while allowing explicitly quarantined artifacts. |
</phase_requirements>

## Summary

Phase 13 is a contract and guard phase, not a backend wiring phase. The repository already has the right local pattern from Phase 5: `whilly/operator_views.py` owns pure, typed operator metadata; the TUI imports it directly; the WUI receives it through `whilly/api/dashboard.py`; unit and HTMX tests pin the behavior. Phase 13 should extend that pattern from table columns to the whole operator UI contract: active surfaces, surface hotkeys, shared actions, WUI DOM selectors, supported route prefixes, and WUI artifact status.

The current active UI contract is five surfaces: `overview`, `compliance`, `plans_tasks`, `workers`, and `events`. The TUI maps keys `1` through `5`; the active `index.html.j2` page renders tabs from `surfaces`, uses `data-surface-tab`, switches on `/^[1-5]$/`, and posts worker controls to `/api/v1/admin/workers/${action}`. The stale assumptions are concentrated in `whilly/api/static/whilly-hotkeys.js` and the pulled partials `_admin.html`, `_logs.html`, and `_prd.html`. `_logs.html` is routeable through `?fragment=logs` today but is not in canonical navigation/TUI parity; `_admin.html` and `_prd.html` include unsupported routes.

**Primary recommendation:** Extend `whilly/operator_views.py` with a small pure operator UI contract for surfaces, actions, selectors, routes, and WUI artifact status; add static contract tests before changing WUI behavior; then either fix `whilly-hotkeys.js` to the five-surface contract or mark it inactive with a tested quarantine reason.

## Standard Stack

### Core

| Library / Module | Version | Purpose | Why Standard |
|------------------|---------|---------|--------------|
| Python `dataclasses`, `enum`, `typing.Final`, `Literal` | Python >=3.12 from `pyproject.toml` | Pure immutable UI metadata and helper APIs. | Existing `whilly/operator_views.py` already uses these for `OperatorSurface`, `OperatorTableColumn`, and table contracts. |
| `whilly.operator_views` | local package | Canonical source of truth for operator surfaces, labels, table columns, and Phase 13 UI contract metadata. | Existing TUI and WUI code already depend on this module for shared operator read models. |
| Rich | installed `15.0.0`; floor `>=13.0.0` | TUI renderer and table output. | Existing `whilly/cli/tui.py` uses Rich directly; no new TUI stack should be introduced. |
| Jinja2 | installed `3.1.6`; floor `>=3.1` | WUI templates and fragments. | Existing dashboard rendering uses `Jinja2Templates` and template includes. |
| FastAPI / Starlette | installed `0.136.1`; floor `>=0.110` | WUI route registration, static mount, HTMX dashboard endpoint. | Existing transport server and dashboard use FastAPI; worker control routes already live at `/api/v1/admin/workers/*`. |
| HTMX / htmx-ext-sse | CDN `htmx.org@1.9.12`, `htmx-ext-sse@2.2.4` in `index.html.j2` | Existing WUI refresh and SSE behavior. | Phase 13 should test current attributes and selectors, not replace the front-end model. |
| pytest / pytest-asyncio | installed `pytest 9.0.3`, `pytest_asyncio 1.3.0`; floors `>=8.0`, `>=0.23` | Pure unit tests, static file scans, and Docker-backed HTMX integration tests. | Existing focused tests use pytest for operator views, TUI, and dashboard behavior. |

### Supporting

| Library / Module | Version | Purpose | When to Use |
|------------------|---------|---------|-------------|
| Ruff | pinned `0.11.5` | Format and lint gate. | Run after code/test changes. |
| `whilly.api.dashboard` | local package | Pass surface/table contract metadata to templates; currently handles `workers`, `tasks`, and `logs` fragments. | Use when testing rendered WUI contract and routeable artifact status. |
| `whilly.adapters.transport.server` | local package | Source of supported admin routes. | Use to verify `/api/v1/admin/workers/control-state`, `/pause`, and `/resume` are the canonical worker control routes. |
| `tests/unit/test_operator_views.py` | local tests | Existing pure contract test home. | Extend or split when adding new contract metadata. |
| `tests/unit/test_tui.py` | local tests | TUI rendering and key handling. | Add contract-driven key map assertions. |
| `tests/integration/test_htmx_dashboard.py` | local tests | Rendered WUI behavior under FastAPI/HTMX. | Keep for rendered dashboard checks; prefer new unit static tests for cheap stale-file scans. |

### Alternatives Considered

| Instead of | Could Use | Tradeoff |
|------------|-----------|----------|
| Extend `whilly/operator_views.py` | New `whilly/operator_ui_contract.py` module | A new module would be clean if metadata grows, but Phase 5 already established `operator_views.py` as the shared UI contract home. Keep Phase 13 there unless the implementation becomes too large. |
| Contract metadata consumed by all templates immediately | Tests around existing literals first | Jinja templates cannot easily import Python constants directly. A staged approach is acceptable if tests enforce parity and downstream phases can reuse helpers. |
| Fix `whilly-hotkeys.js` now | Mark it inactive/quarantined | Fixing is small and reduces confusion, but the context allows pinning the failing contract and deferring implementation to Phase 14 if the handoff is explicit. If left stale, it must not be classified as active. |
| Parse JavaScript/Jinja fully | Targeted static scans plus rendered tests | Full parsers would be overkill. Phase risk is known stale strings, selectors, and route prefixes; focused scans are more maintainable. |

**Installation:**

```bash
pip install -e '.[dev]'
```

**Version verification:** Versions above were verified from `.venv` and `pyproject.toml` on 2026-05-11. No new package is recommended, so registry checks are not required for planning.

## Architecture Patterns

### Recommended Project Structure

```text
whilly/
+-- operator_views.py                  # Existing pure shared UI/read-model contract; extend here
+-- cli/
|   +-- tui.py                         # Consume or test against surface/action contract
+-- api/
|   +-- dashboard.py                   # Pass WUI contract metadata and classify fragments
|   +-- templates/
|   |   +-- index.html.j2              # Active WUI dashboard
|   |   +-- _tasks_table.html          # Active fragment
|   |   +-- _workers_table.html        # Active fragment
|   |   +-- _logs.html                 # Routeable, non-canonical artifact to cover/quarantine
|   |   +-- _admin.html                # Inactive/quarantined artifact
|   |   +-- _prd.html                  # Inactive/quarantined artifact
|   +-- static/
|       +-- whilly-hotkeys.js          # Fix to contract or mark inactive
+-- adapters/
    +-- transport/server.py            # Supported admin worker route source

tests/
+-- unit/
|   +-- test_operator_views.py         # Pure UI contract tests
|   +-- test_tui.py                    # TUI consumption/key assertions
|   +-- test_wui_contract_static.py    # New cheap static scans over active WUI files
+-- integration/
    +-- test_htmx_dashboard.py         # Rendered dashboard parity checks
```

### Pattern 1: Pure Operator UI Contract

**What:** Add pure data objects that describe canonical surfaces, shared actions, hotkeys, WUI selectors, supported route prefixes, and artifact status. Keep the module free of FastAPI, Rich rendering objects, filesystem reads, subprocesses, and network calls.

**When to use:** Use when a renderer, template context builder, or test needs to know what UI surfaces/actions/routes/selectors are canonical.

**Example:**

```python
# Source pattern: whilly/operator_views.py:20-130
from dataclasses import dataclass
from enum import Enum
from typing import Final


class OperatorAction(str, Enum):
    QUIT = "quit"
    REFRESH = "refresh"
    FILTER_FOCUS = "filter.focus"
    WORKERS_PAUSE = "workers.pause"
    WORKERS_RESUME = "workers.resume"
    REVIEW_APPROVE = "review.approve"
    REVIEW_REJECT = "review.reject"
    REVIEW_CHANGES_REQUESTED = "review.changes_requested"
    REVIEW_SELECT_NEXT = "review.select_next"
    REVIEW_SELECT_PREVIOUS = "review.select_previous"


@dataclass(frozen=True)
class OperatorActionSpec:
    action: OperatorAction
    label: str
    hotkeys: tuple[str, ...] = ()
    surfaces: tuple[OperatorSurface, ...] = ()
    wui_selector: str = ""
    wui_route_prefix: str = ""
    medium_note: str = ""


OPERATOR_ACTIONS: Final[tuple[OperatorActionSpec, ...]] = (
    OperatorActionSpec(OperatorAction.QUIT, "Quit", ("q", "Q")),
    OperatorActionSpec(OperatorAction.REFRESH, "Refresh", ("r",)),
    OperatorActionSpec(OperatorAction.WORKERS_PAUSE, "Pause workers", ("p", "P"), wui_route_prefix="/api/v1/admin/workers/"),
    OperatorActionSpec(OperatorAction.WORKERS_RESUME, "Resume workers", ("R",), wui_route_prefix="/api/v1/admin/workers/"),
)
```

Implementation guidance:

- Keep `OperatorSurface` as the canonical enum. Do not add logs/admin/PRD surfaces in Phase 13 unless the contract is deliberately expanded and TUI/WUI tests are updated in the same phase.
- Generate or test surface switch keys from `operator_surface_items()`: key `str(index)` maps to each surface in display order.
- Include action specs for the shared current actions: `q`, `r`, `R`, `1-5`, `/`, `p/P`, `j/k`, `a/x/c`.
- Include DOM selectors currently active in `index.html.j2`: `[data-surface-tab]`, `[data-surface]`, `#dashboard-filter`, `[data-control-action]`, `[data-review-decision]`, and `#review-gaps tbody tr[data-review-actionable="true"]`.
- Include route prefixes used by active controls: `/api/v1/admin/workers/` and `/api/v1/tasks/` for human-review submission. Do not bless `/admin/workers/`.

### Pattern 2: Contract-Aware WUI Artifact Classification

**What:** Add explicit artifact metadata for WUI templates and static JavaScript. Each artifact should be `active`, `routeable_noncanonical`, or `inactive_quarantined` with a reason and owner phase.

**When to use:** Use for UI-02 so tests can scan active files strictly while allowing known pulled artifacts only when they are explicitly classified.

**Example:**

```python
# Source pattern: same pure metadata style as OPERATOR_TABLE_COLUMNS
class OperatorUiArtifactStatus(str, Enum):
    ACTIVE = "active"
    ROUTEABLE_NONCANONICAL = "routeable_noncanonical"
    INACTIVE_QUARANTINED = "inactive_quarantined"


@dataclass(frozen=True)
class OperatorUiArtifact:
    path: str
    status: OperatorUiArtifactStatus
    reason: str = ""
    followup_phase: str = ""


OPERATOR_WUI_ARTIFACTS: Final[tuple[OperatorUiArtifact, ...]] = (
    OperatorUiArtifact("whilly/api/templates/index.html.j2", OperatorUiArtifactStatus.ACTIVE),
    OperatorUiArtifact("whilly/api/templates/_tasks_table.html", OperatorUiArtifactStatus.ACTIVE),
    OperatorUiArtifact("whilly/api/templates/_workers_table.html", OperatorUiArtifactStatus.ACTIVE),
    OperatorUiArtifact(
        "whilly/api/templates/_logs.html",
        OperatorUiArtifactStatus.ROUTEABLE_NONCANONICAL,
        reason="Routeable by ?fragment=logs but not in canonical nav/TUI parity yet.",
        followup_phase="14",
    ),
    OperatorUiArtifact(
        "whilly/api/templates/_admin.html",
        OperatorUiArtifactStatus.INACTIVE_QUARANTINED,
        reason="Contains admin controls without active server routes.",
        followup_phase="14",
    ),
)
```

Implementation guidance:

- Include `whilly/api/static/whilly-hotkeys.js` in this contract. If fixed in Phase 13, classify it as `active` or `active_static`; if left stale, classify it as `inactive_quarantined` with a Phase 14 follow-up.
- Keep CSS/fonts out of the Phase 13 artifact contract unless tests need to classify every static asset. The known scope is templates and static JavaScript with hotkeys/routes.
- If a file is quarantined, require a non-empty reason and follow-up phase in tests.
- Do not silently ignore `_admin.html`, `_logs.html`, `_prd.html`, or `whilly-hotkeys.js`.

### Pattern 3: Active Renderer Consumption Plus Static Guards

**What:** Use two layers of tests: consumption tests prove TUI/WUI match contract metadata, and static guards reject stale strings in active files.

**When to use:** Use consumption tests for TUI and rendered WUI. Use static guards for the stale-file problems because they are literal route/selector/hotkey assumptions.

**Example:**

```python
# Source pattern: tests/unit/test_operator_views.py and tests/unit/test_tui.py
def test_surface_hotkeys_derive_from_contract() -> None:
    assert operator_surface_hotkeys() == (
        ("1", OperatorSurface.OVERVIEW),
        ("2", OperatorSurface.COMPLIANCE),
        ("3", OperatorSurface.PLANS_TASKS),
        ("4", OperatorSurface.WORKERS),
        ("5", OperatorSurface.EVENTS),
    )


def test_active_wui_files_do_not_use_stale_worker_routes(project_root: Path) -> None:
    for artifact in active_wui_artifacts():
        text = (project_root / artifact.path).read_text(encoding="utf-8")
        assert "/admin/workers/" not in text
```

Implementation guidance:

- Test `_SURFACE_BY_KEY` in `whilly/cli/tui.py` against the derived surface hotkeys instead of maintaining a second expected tuple.
- Test WUI rendered tabs against `operator_surface_items()`.
- Test active WUI files do not contain `/^[1-7]$/`, `1-7`, `.tabs [data-key]`, or `/admin/workers/`.
- Scope static scans to contract-active artifacts. Then add a separate test that every template/static-JS artifact is either active, routeable noncanonical, or quarantined.
- Add an explicit test for `_logs.html` status because it is routeable through `dashboard.py` but not canonical nav/TUI.

### Anti-Patterns to Avoid

- **Duplicating surface/action lists in TUI, WUI, and tests:** creates a new drift point. Derive or compare against one contract helper.
- **Treating served static files as harmless:** `/static/whilly-hotkeys.js` is served by the app even if the main page does not link it; either fix it or quarantine it.
- **Scanning the whole repository for banned strings:** docs, tests, and context files intentionally mention stale patterns. Scan only active WUI artifacts, plus artifact classification tests.
- **Expanding canonical surfaces implicitly:** adding logs/admin/PRD to a JS list without TUI/WUI parity would violate Phase 13 scope.
- **Implementing `/admin/*`, `/prd/*`, or `/logs/*` backend routes now:** that belongs to Phase 14 unless needed only to mark artifacts inactive.
- **Letting routeable logs stay ambiguous:** `_logs.html` is currently routeable via `?fragment=logs`; the contract must name that status even if Phase 14 performs final wiring/quarantine.

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| UI source of truth | Separate JS JSON blob, YAML config, or copied test tuple | Pure metadata helpers in `whilly/operator_views.py` | Existing renderers already depend on this module; keeps the contract typed and importable. |
| Surface hotkey mapping | Hardcoded `1-5` or `1-7` literals in multiple places | Helper derived from `operator_surface_items()` | Surface count changes should update keys and tests together. |
| Static stale route checks | Full Jinja or JavaScript parser | Targeted static tests over contract-active artifacts | Known regressions are literal stale selectors/routes; parsers add complexity without improving coverage. |
| Backend support for pulled partials | New `/admin/*`, `/prd/*`, `/logs/*` methods in Phase 13 | Artifact status metadata plus Phase 14 follow-up | The phase boundary says canonical contract and regression guards only. |
| UI test harness | Browser automation or screenshot QA | Existing pytest unit/static tests plus focused HTMX integration tests | Browser/a11y QA is deferred; Phase 13 needs fast contract checks. |
| Orphan artifact tracking | Comments only | Metadata plus tests requiring reason/follow-up for non-active artifacts | Comments alone will not fail when a new orphan appears. |

**Key insight:** The phase succeeds when a stale UI assumption becomes mechanically visible. It does not need a new UI framework or backend capability; it needs one typed contract and cheap tests that make drift fail early.

## Common Pitfalls

### Pitfall 1: Updating `index.html.j2` But Leaving `whilly-hotkeys.js` Stale

**What goes wrong:** The active page looks correct, but the served static JS still advertises `1-7`, `.tabs [data-key]`, and `/admin/workers/*`.

**Why it happens:** The current dashboard uses inline JavaScript, so the standalone static file is easy to forget.

**How to avoid:** Include `whilly/api/static/whilly-hotkeys.js` in the artifact contract. Either fix it or quarantine it with a reason and Phase 14 follow-up.

**Warning signs:** No test mentions `whilly-hotkeys.js`, or the file still contains `/^[1-7]$/`.

### Pitfall 2: Routeable Logs Fragment Is Treated As Inactive Without Evidence

**What goes wrong:** `_logs.html` is called inactive, but `dashboard.py` still renders it for `?fragment=logs`.

**Why it happens:** The fragment is not visible in canonical nav, but `_normalise_fragment()` accepts `logs`.

**How to avoid:** Use a distinct `routeable_noncanonical` status or explicitly test the chosen quarantine behavior. Phase 14 can then wire or remove the route.

**Warning signs:** `_logs.html` has no contract entry, or tests only assert it is not in visible nav.

### Pitfall 3: Tests Pin Strings Instead Of Contract Relationships

**What goes wrong:** Tests pass because both code and test copied the same stale list.

**Why it happens:** It is quicker to assert `"Overview"` and `"1-5=switch"` directly.

**How to avoid:** Use `operator_surface_items()` and derived helpers in tests. Rendered tests can still assert user-visible labels, but the expected values should come from the contract.

**Warning signs:** A new surface requires changing three unrelated expected tuples.

### Pitfall 4: Over-Strict Global Greps

**What goes wrong:** Tests fail on documentation, planning context, or regression-test fixtures that intentionally mention stale strings.

**Why it happens:** A repo-wide grep is simpler than contract-scoped artifact scanning.

**How to avoid:** Scan only `OPERATOR_WUI_ARTIFACTS` with active status for banned runtime patterns, and separately assert non-active artifacts are classified.

**Warning signs:** A test rejects `.planning/` or `tests/` references to `/admin/workers/`.

### Pitfall 5: Phase 13 Accidentally Implements Later Phases

**What goes wrong:** Planning grows into logs/admin/PRD backend routes, TUI screens, or browser QA.

**Why it happens:** The pulled artifacts contain visible product ideas.

**How to avoid:** Use artifact statuses and route allowlists as the handoff. Wire backend methods in Phase 14, TUI parity in Phase 15, and QA/docs in Phase 16.

**Warning signs:** New server methods for `/prd/generate`, `/admin/slack`, or `/logs/{task_id}.jsonl` appear in Phase 13 tasks.

## Code Examples

Verified patterns from local sources:

### Existing Surface And Table Contract

```python
# Source: whilly/operator_views.py:20-130
class OperatorSurface(str, Enum):
    OVERVIEW = "overview"
    COMPLIANCE = "compliance"
    PLANS_TASKS = "plans_tasks"
    WORKERS = "workers"
    EVENTS = "events"


OPERATOR_SURFACE_LABELS: Final[Mapping[OperatorSurface, str]] = {
    OperatorSurface.OVERVIEW: "Overview",
    OperatorSurface.COMPLIANCE: "Compliance",
    OperatorSurface.PLANS_TASKS: "Plans/Tasks",
    OperatorSurface.WORKERS: "Workers",
    OperatorSurface.EVENTS: "Events",
}


def operator_surface_items() -> tuple[tuple[OperatorSurface, str], ...]:
    return tuple((surface, OPERATOR_SURFACE_LABELS[surface]) for surface in OperatorSurface)
```

### Existing TUI Surface Mapping To Replace Or Test Against

```python
# Source: whilly/cli/tui.py:61-67
_SURFACE_BY_KEY: Final[dict[str, OperatorSurface]] = {
    "1": OperatorSurface.OVERVIEW,
    "2": OperatorSurface.COMPLIANCE,
    "3": OperatorSurface.PLANS_TASKS,
    "4": OperatorSurface.WORKERS,
    "5": OperatorSurface.EVENTS,
}
```

### Existing WUI Active Selector And Route Patterns

```javascript
// Source: whilly/api/templates/index.html.j2:755-875
document.querySelectorAll("[data-surface-tab]").forEach((button) => {
  button.setAttribute("aria-selected", String(button.dataset.surfaceTab === surface));
});

const response = await fetch(`/api/v1/admin/workers/${action}`, {
  method: "POST",
  headers: {
    "Authorization": `Bearer ${token}`,
    "Content-Type": "application/json",
  },
  body: JSON.stringify(payload),
});
```

### Static Guard Shape

```python
# Source pattern: tests read project files in tests/integration/test_htmx_dashboard.py:836-855
BANNED_ACTIVE_WUI_PATTERNS = (
    "1-7",
    "/^[1-7]$/",
    ".tabs [data-key]",
    "/admin/workers/",
)


def test_active_wui_artifacts_do_not_reference_stale_contract(project_root: Path) -> None:
    for artifact in active_wui_artifacts():
        text = (project_root / artifact.path).read_text(encoding="utf-8")
        for pattern in BANNED_ACTIVE_WUI_PATTERNS:
            assert pattern not in text, f"{artifact.path} contains stale UI contract pattern {pattern!r}"
```

## State of the Art

| Old Approach | Current Approach | When Changed | Impact |
|--------------|------------------|--------------|--------|
| UI labels and table fields copied in TUI/WUI templates | Shared table metadata in `whilly/operator_views.py` consumed by TUI/WUI | v1.0 Phase 5, archived 2026-05-08 | Phase 13 should follow this pattern for actions/selectors/routes. |
| WUI active dashboard with inline literals only | `dashboard.py` passes `surfaces` and `table_columns` from `operator_views` | Current code | WUI already has a Python contract injection path. |
| Stale static hotkey assumptions (`1-7`, `.tabs [data-key]`, `/admin/workers/*`) | Active inline WUI uses five surfaces, `[data-surface-tab]`, and `/api/v1/admin/workers/*` | Current post-pull state | Static JS must be fixed or quarantined. |
| Pulled logs/admin/PRD partials implicitly present | Phase 13 contract should classify each artifact | Phase 13 target | Prevents orphan UI files from becoming silent runtime debt. |

**Deprecated/outdated:**

- `1-7` surface switching is outdated while canonical active surfaces remain five.
- `.tabs [data-key]` is outdated; active WUI tabs use `[data-surface-tab]`.
- `/admin/workers/*` is outdated; supported worker control routes are `/api/v1/admin/workers/*`.
- `_admin.html` and `_prd.html` route references are not active server routes.
- `_logs.html` is not canonical nav/TUI parity even though `?fragment=logs` currently renders it.

## Open Questions

1. **Should `whilly-hotkeys.js` be fixed in Phase 13 or quarantined?**
   - What we know: It is stale and served under `/static`, but `index.html.j2` does not currently link it.
   - What's unclear: Whether downstream WUI work wants this file as an active external hotkey implementation.
   - Recommendation: Prefer fixing it to the five-surface contract if small. If not fixed, classify it as `inactive_quarantined` and add a Phase 14 follow-up.

2. **How should routeable logs be represented before Phase 14?**
   - What we know: `dashboard.py` accepts `fragment=logs` and renders `_logs.html`, but the surface is not in active nav or TUI.
   - What's unclear: Whether Phase 14 will fully wire logs or quarantine the fragment route.
   - Recommendation: Use `routeable_noncanonical` status now, with a non-empty reason and Phase 14 follow-up. Do not add logs to `OperatorSurface` in Phase 13.

3. **Should `index.html.j2` inline JavaScript consume JSON contract data?**
   - What we know: The template already receives `surfaces`; `surfaceOrder` is still a literal JavaScript list.
   - What's unclear: Whether the planner wants a small Jinja-generated list or just a test comparing it to `operator_surface_items()`.
   - Recommendation: Generate `surfaceOrder` from the `surfaces` template context if the edit stays small; otherwise add a rendered test that fails on drift.

## Validation Architecture

### Test Framework

| Property | Value |
|----------|-------|
| Framework | pytest `9.0.3`, pytest-asyncio `1.3.0` locally; pytest floor `>=8.0` in `pyproject.toml` |
| Config file | `pyproject.toml` (`[tool.pytest.ini_options]`) |
| Quick run command | `.venv/bin/python -m pytest -q tests/unit/test_operator_views.py tests/unit/test_tui.py tests/unit/test_wui_contract_static.py` |
| Full suite command | `make test` |
| Docker-backed rendered WUI command | `.venv/bin/python -m pytest -q tests/integration/test_htmx_dashboard.py tests/integration/test_control_state_admin_api.py` |

### Phase Requirements -> Test Map

| Req ID | Behavior | Test Type | Automated Command | File Exists? |
|--------|----------|-----------|-------------------|--------------|
| UI-01 | Canonical surfaces remain five and drive TUI/WUI surface labels and switch keys. | unit | `.venv/bin/python -m pytest -q tests/unit/test_operator_views.py::test_operator_surface_items_pin_shared_order_and_labels tests/unit/test_tui.py::test_handle_tui_key_switches_views_filter_pause_refresh_review_actions_and_quit` | Exists, extend |
| UI-01 | Shared actions define hotkeys, WUI selectors, and route prefixes for pause/resume, refresh, filter, review decisions, and surface switching. | unit | `.venv/bin/python -m pytest -q tests/unit/test_operator_ui_contract.py` | Missing - Wave 0 |
| UI-01 | Rendered WUI tabs and inline hotkey code match `operator_surface_items()` and the shared action contract. | integration/static | `.venv/bin/python -m pytest -q tests/integration/test_htmx_dashboard.py::test_dashboard_mirrors_operator_surfaces_and_hotkeys` | Exists, extend |
| UI-02 | Active WUI artifacts fail on stale `1-7`, `.tabs [data-key]`, and `/admin/workers/*`. | unit static | `.venv/bin/python -m pytest -q tests/unit/test_wui_contract_static.py::test_active_wui_artifacts_reject_stale_patterns` | Missing - Wave 0 |
| UI-02 | Every WUI template/static JS artifact is active, routeable noncanonical, or quarantined with a reason and follow-up phase. | unit static | `.venv/bin/python -m pytest -q tests/unit/test_wui_contract_static.py::test_wui_artifacts_are_classified` | Missing - Wave 0 |
| UI-02 | Supported worker control route prefixes remain `/api/v1/admin/workers/*`. | integration | `.venv/bin/python -m pytest -q tests/integration/test_control_state_admin_api.py::test_admin_can_pause_resume_and_read_control_state` | Exists |

### Sampling Rate

- **Per task commit:** `.venv/bin/python -m pytest -q tests/unit/test_operator_views.py tests/unit/test_tui.py tests/unit/test_wui_contract_static.py`
- **Per wave merge:** `.venv/bin/python -m pytest -q tests/unit/test_operator_views.py tests/unit/test_tui.py tests/unit/test_wui_contract_static.py tests/integration/test_htmx_dashboard.py`
- **Phase gate:** Focused unit/static suite green, plus Docker-backed HTMX/admin-route tests when Docker is available. Run `make lint`; run `make test` when practical and report unrelated baseline failures separately.

### Wave 0 Gaps

- [ ] `tests/unit/test_operator_ui_contract.py` - covers UI-01 action, hotkey, selector, route-prefix contract if not added to `test_operator_views.py`.
- [ ] `tests/unit/test_wui_contract_static.py` - covers UI-02 active artifact stale-pattern scans and artifact classification.
- [ ] Contract helpers in `whilly/operator_views.py` - `operator_surface_hotkeys()`, `operator_action_items()` or equivalent, `active_wui_artifacts()` or equivalent.
- [ ] Optional rendered test update in `tests/integration/test_htmx_dashboard.py` - compare WUI tabs/actions to contract-derived expectations.

### Current Baseline Checked

```bash
.venv/bin/python -m pytest -q tests/unit/test_operator_views.py tests/unit/test_tui.py
# 25 passed in 0.25s
```

## Sources

### Primary (HIGH confidence)

- `.planning/phases/13-canonical-ui-parity-contract/13-CONTEXT.md` - phase boundary, locked decisions, stale artifact handling, deferred scope.
- `.planning/REQUIREMENTS.md` - UI-01 and UI-02 requirement definitions.
- `.planning/STATE.md` and `.planning/ROADMAP.md` - v1.1 milestone state and Phase 13 success criteria.
- `whilly/operator_views.py` - existing `OperatorSurface`, table metadata, pure contract pattern.
- `whilly/cli/tui.py` - TUI hotkeys, surface mapping, Rich rendering consumption.
- `whilly/api/dashboard.py` - WUI template context, fragment handling, routeable logs fragment.
- `whilly/api/templates/index.html.j2` - active WUI surfaces, inline hotkeys, selectors, worker/review routes.
- `whilly/api/static/whilly-hotkeys.js` - stale standalone hotkey file.
- `whilly/api/templates/_admin.html`, `_logs.html`, `_prd.html` - pulled artifacts to classify.
- `tests/unit/test_operator_views.py`, `tests/unit/test_tui.py`, `tests/integration/test_htmx_dashboard.py`, `tests/integration/test_control_state_admin_api.py` - existing test patterns and validation targets.

### Secondary (MEDIUM confidence)

- `.planning/phases/05-shared-operator-table-contract/05-01-PLAN.md` and `05-CONTEXT.md` - prior local pattern for shared operator UI metadata and renderer consumption.
- `pyproject.toml`, `Makefile`, `tests/conftest.py` - dependency floors, pytest configuration, Docker-backed integration fixture behavior.

### Tertiary (LOW confidence)

- None. External ecosystem research was not needed because Phase 13 uses the repository's existing stack and no new package is recommended.

## Metadata

**Confidence breakdown:**

- Standard stack: HIGH - verified from local `pyproject.toml`, `.venv` installed versions, and existing imports.
- Architecture: HIGH - based on current `operator_views`, TUI, dashboard, templates, and tests.
- Pitfalls: HIGH - stale strings and orphan artifacts are directly present in current files.
- Validation: HIGH for unit/static tests; MEDIUM for Docker-backed integration runtime because local Docker availability can vary.

**Research date:** 2026-05-11
**Valid until:** 2026-06-10 for local architecture; revisit sooner if Phase 14 changes WUI fragment routing before Phase 13 planning is consumed.
