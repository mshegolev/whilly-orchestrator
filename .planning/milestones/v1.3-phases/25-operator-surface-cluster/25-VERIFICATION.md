---
phase: 25-operator-surface-cluster
verified: 2026-06-16T00:00:00Z
status: passed
score: 5/5 must-haves verified
overrides_applied: 0
re_verification:
  previous_status: none
  previous_score: n/a
---

# Phase 25: Operator Surface Cluster Verification Report

**Phase Goal:** The 5 operator-surface contracts (OPS-01..05) are captured as normative OpenSpec specs reverse-spec'd from real v4.7.0 code.
**Verified:** 2026-06-16
**Status:** passed
**Re-verification:** No — initial verification

## Goal Achievement

### Observable Truths

| # | Truth | Status | Evidence |
|---|-------|--------|----------|
| 1 | OPS-01 `dashboard-tui` spec exists, passes `--strict`, grounded in real hotkeys + Rich Live + NullDashboard | ✓ VERIFIED | `openspec validate dashboard-tui --strict` → valid, exit 0. dashboard.py:88-99 registers exactly d,l,t,s,$,p,g,c,n,r,h,? (11 keys) + 1/2/3 wizard keys (686-688); `Live(..., refresh_per_second=1, screen=True)` at line 86; `_overlay_mode` ∈ {log,task_log,detail} at line 73; `class NullDashboard` at line 1346 |
| 2 | OPS-02 `web-status-ui` spec exists, passes `--strict`, two-token auth + real endpoints/SSE, references auth-security | ✓ VERIFIED | `openspec validate web-status-ui --strict` → valid, exit 0. auth.py: WORKER_TOKEN_ENV=WHILLY_WORKER_TOKEN (95), BOOTSTRAP_TOKEN_ENV=WHILLY_WORKER_BOOTSTRAP_TOKEN (108), `WWW-Authenticate: Bearer realm="whilly"` (171), `secrets.compare_digest` (constant-time), RuntimeError at config time (347). Routes confirmed: /tasks/claim (204), /workers/register, /workers/{id}/heartbeat, /tasks/{id}/complete\|fail (409), /health, /api/v1/plans (201/428/412), /events/stream (Last-Event-ID, replay_truncated), /metrics, web_status 9191 /api/status (404). Spec §269-283 delegates session/OIDC/WebAuthn to auth-security; subsystem altitude in Purpose |
| 3 | OPS-03 `reporting` spec exists, passes `--strict`, truthfully states Reporter/generate_summary legacy/unwired; live consumer = dashboard helper imports | ✓ VERIFIED | `openspec validate reporting --strict` → valid, exit 0. Grep: `Reporter(` instantiation = 0 hits in whilly/ (excl. reporter.py); `generate_summary(` call sites = 0 hits in whilly/. dashboard.py:34 imports exactly `CostTotals, fmt_duration, fmt_tokens`. Spec §77-91 "Legacy v4 wiring status" requirement states this truthfully (non-asserting form) |
| 4 | OPS-04 `cli-surface` spec exists, passes `--strict`, pins REAL EXIT_* constants, no-args→help, headless truthful | ✓ VERIFIED | `openspec validate cli-surface --strict` → valid, exit 0. plan.py: EXIT_OK=0/EXIT_VALIDATION_ERROR=1/EXIT_ENVIRONMENT_ERROR=2 (120-122); run.py: 0/2, no validation path (60,121-122); workspaces.py: WORKSPACE_FAILED_EXIT_CODE=-4 (22). Spec §17-22 pins exactly these and explicitly rejects legacy "0/1/2/3 budget/timeout" lore. no-args→`_print_help` return 0 (__init__.py:397-398); unknown→2 (545-547); WHILLY_HEADLESS set by shim (242) but read by NO subcommand (grep: only set in __init__.py; config.HEADLESS never consumed) — spec §150-156 states this truthfully, no fabricated headless JSON. Behavioral spot-check confirms |
| 5 | OPS-05 `operator-views-logs` spec exists, passes `--strict`, real log_viewer/operator_views/tui symbols | ✓ VERIFIED | `openspec validate operator-views-logs --strict` → valid, exit 0. log_viewer.py: cmd_list/cmd_show/cmd_tail/run_logs_command/discover_tasks/cleanup_old_logs all present. operator_views.py: OperatorSurface{overview,compliance,plans_tasks,workers,events}, OperatorTable, OperatorAction, OPERATOR_ACTIONS with wui_route_prefix /api/v1/admin/workers/ + /api/v1/tasks/, OPERATOR_WUI_ARTIFACTS, OperatorUiArtifactStatus{active,routeable_noncanonical,inactive_quarantined}. cli/tui.py: handle_tui_key, _SURFACE_BY_KEY, REVIEWER_ENV=WHILLY_OPERATOR_EMAIL, EXIT_ENVIRONMENT_ERROR on missing DSN |

**Score:** 5/5 truths verified

### Required Artifacts

| Artifact | Expected | Status | Details |
|----------|----------|--------|---------|
| `openspec/specs/dashboard-tui/spec.md` | Normative spec, Rich Live + hotkeys + web SSE | ✓ VERIFIED | 136 lines, ## Purpose + 7 requirements, all with SHALL/MUST + scenarios, passes --strict |
| `openspec/specs/web-status-ui/spec.md` | Normative spec, FastAPI control plane + transport auth + SSE | ✓ VERIFIED | 320 lines, ## Purpose + 16 requirements, passes --strict, references auth-security |
| `openspec/specs/reporting/spec.md` | Normative spec, JSON/Markdown reporting + truthful wiring | ✓ VERIFIED | 91 lines, ## Purpose + 5 requirements incl. Legacy v4 wiring status, passes --strict |
| `openspec/specs/cli-surface/spec.md` | Normative spec, real exit codes + shim + help | ✓ VERIFIED | 182 lines, ## Purpose + 10 requirements, real EXIT_* constants, passes --strict |
| `openspec/specs/operator-views-logs/spec.md` | Normative spec, log viewer + operator views + TUI | ✓ VERIFIED | 180 lines, ## Purpose + 8 requirements, real symbols, passes --strict |

### Key Link Verification

| From | To | Via | Status | Details |
|------|----|----|--------|---------|
| cli-surface spec | cli/plan.py | EXIT_OK/EXIT_VALIDATION_ERROR/EXIT_ENVIRONMENT_ERROR | ✓ WIRED | Constants 0/1/2 confirmed at lines 120-122 |
| cli-surface spec | cli/run.py | EXIT_OK=0/EXIT_ENVIRONMENT_ERROR=2, no validation path | ✓ WIRED | Lines 60,121-122 confirm 0/2 + intentional no-1 path |
| cli-surface spec | workspaces.py | WORKSPACE_FAILED_EXIT_CODE=-4 | ✓ WIRED | Line 22 confirms -4 (negative, not positive 4) |
| reporting spec | reporter.py | Reporter/generate_summary + helpers | ✓ WIRED | Symbols present; 0 instantiation/call sites in worker path |
| reporting spec | dashboard.py | CostTotals/fmt_duration/fmt_tokens import | ✓ WIRED | dashboard.py:34 imports exactly those 3 helpers |
| dashboard-tui spec | dashboard.py | keyboard.register hotkeys | ✓ WIRED | 11 base + 3 wizard keys match exactly |
| web-status-ui spec | transport/auth.py | bearer_auth + bootstrap_auth, two-token split | ✓ WIRED | Both env vars + WWW-Authenticate + compare_digest confirmed |
| web-status-ui spec | api/plans_api.py | /api/v1/plans routes, If-Match | ✓ WIRED | GET/POST 201/PATCH 428/412 confirmed |
| operator-views-logs spec | log_viewer.py | cmd_list/cmd_show/cmd_tail | ✓ WIRED | All three + run_logs_command present |

### Behavioral Spot-Checks

| Behavior | Command | Result | Status |
|----------|---------|--------|--------|
| no-args prints HELP, not a menu, exit 0 | `python3 -m whilly` | "Whilly v4 — distributed task orchestrator." + exit 0 | ✓ PASS |
| unknown command returns 2 | `python3 -m whilly bogusxyz` | "whilly: unknown command 'bogusxyz'" + exit 2 | ✓ PASS |
| version fast path exit 0 + confirms v4.7.0 | `python3 -m whilly --version` | "whilly 4.7.0" + exit 0 | ✓ PASS |

### Probe Execution

| Probe | Command | Result | Status |
|-------|---------|--------|--------|
| dashboard-tui strict | `openspec validate dashboard-tui --strict` | valid, exit 0 | PASS |
| web-status-ui strict | `openspec validate web-status-ui --strict` | valid, exit 0 | PASS |
| reporting strict | `openspec validate reporting --strict` | valid, exit 0 | PASS |
| cli-surface strict | `openspec validate cli-surface --strict` | valid, exit 0 | PASS |
| operator-views-logs strict | `openspec validate operator-views-logs --strict` | valid, exit 0 | PASS |

### Requirements Coverage

| Requirement | Source Plan | Description | Status | Evidence |
|-------------|-------------|-------------|--------|----------|
| OPS-01 | 25-01 | dashboard-tui Rich Live states + hotkeys | ✓ SATISFIED | spec valid --strict; hotkeys/Live/NullDashboard grounded; REQUIREMENTS.md [x] |
| OPS-02 | 25-02 | web-status-ui control plane + transport + SSE | ✓ SATISFIED | spec valid --strict; two-token auth + routes grounded; auth-security referenced; REQUIREMENTS.md [x] |
| OPS-03 | 25-01 | reporting JSON/Markdown + truthful wiring | ✓ SATISFIED | spec valid --strict; legacy/unwired status verified by grep; REQUIREMENTS.md [x] |
| OPS-04 | 25-03 | cli-surface flags + real exit-code contract | ✓ SATISFIED | spec valid --strict; real EXIT_* pinned (0/1/2/-4); no legacy lore; behavioral spot-checks pass; REQUIREMENTS.md [x] |
| OPS-05 | 25-03 | operator-views-logs viewer + views + TUI | ✓ SATISFIED | spec valid --strict; all symbols grounded; REQUIREMENTS.md [x] |

### Anti-Patterns Found

| File | Line | Pattern | Severity | Impact |
|------|------|---------|----------|--------|
| (none) | — | No TODO/FIXME/TBD/placeholder/delta-header in any of the 5 specs | ℹ️ Info | Clean documentation-only output |

### ROADMAP Success Criteria Reconciliation

| SC | ROADMAP Wording | Status | Note |
|----|-----------------|--------|------|
| 1 | 5 capabilities specced | ✓ MET | All 5 spec.md present and valid |
| 2 | cli-surface enumerates flags, headless JSON, exit codes 0/1/2/3 | ✓ MET (corrected) | The literal "0/1/2/3" + "headless JSON output" is **stale v3 lore** the phase was explicitly chartered (CONTEXT.md "GROUNDING CAUTION") to supersede. The spec correctly pins real v4 EXIT_OK=0/EXIT_VALIDATION_ERROR=1/EXIT_ENVIRONMENT_ERROR=2/WORKSPACE_FAILED_EXIT_CODE=-4 and truthfully states WHILLY_HEADLESS is set-but-not-read (no fabricated headless JSON contract). Source-grounded truth supersedes stale roadmap wording — this is the intended outcome, not a gap. |
| 3 | dashboard-tui captures states + hotkeys | ✓ MET | 11 hotkeys + overlay states grounded |
| 4 | Each spec ≥1 scenario, all pass --strict | ✓ MET | All 5 pass; every requirement has ≥1 scenario |
| 5 | Covered modules checked off in coverage matrix | ✓ MET | COVERAGE-MATRIX.md maps all phase-25 modules to the 5 slugs |

### Boundary / Hygiene Checks

| Check | Status | Evidence |
|-------|--------|----------|
| No whilly/ Python changes | ✓ PASS | `git diff --name-only 125d140^ 13c63e4 | grep ^whilly/` → NONE. Only openspec/specs/ + .planning/ touched |
| No delta headers | ✓ PASS | grep "## ADDED/MODIFIED/REMOVED/RENAMED Requirements" across all 5 specs → NONE |
| OPS-01..05 marked done in REQUIREMENTS.md | ✓ PASS | All 5 rows `[x]`, traceability table rows 157-159 mark all Done |
| 1:1 spec mapping | ✓ PASS | OPS-01→dashboard-tui, OPS-02→web-status-ui, OPS-03→reporting, OPS-04→cli-surface, OPS-05→operator-views-logs |

### Human Verification Required

None. All truths are programmatically verifiable: spec validity via `openspec validate --strict`, groundedness via grep against real source, CLI behavior via direct invocation. All checks passed.

### Gaps Summary

No gaps. Every spec is reverse-spec'd from real v4.7.0 code (version confirmed `whilly 4.7.0` via direct CLI invocation):

- **cli-surface (OPS-04):** Pins the real EXIT_* constants (0/1/2/-4), explicitly rejects the legacy "0=ok/1=some failed/2=budget/3=timeout" set, invents no positive 4 / no 3=timeout. no-args→_print_help (verified by behavioral spot-check, not a menu). WHILLY_HEADLESS correctly stated as set-by-shim/read-by-nobody — no fabricated headless JSON contract. The stale ROADMAP SC#2 "0/1/2/3 + headless JSON" wording is the exact lore the phase corrected; source truth wins.
- **reporting (OPS-03):** Truthfully states Reporter/generate_summary are legacy/unwired in the v4 worker-claim path (grep confirms zero instantiation/call sites); live consumer is dashboard.py importing only the three formatter helpers.
- **dashboard-tui (OPS-01):** Real hotkeys (11 + 3 wizard), Rich Live (screen=True, refresh_per_second=1), NullDashboard substitution — all grounded.
- **web-status-ui (OPS-02):** Two-token auth (per-worker bearer WHILLY_WORKER_TOKEN vs bootstrap WHILLY_WORKER_BOOTSTRAP_TOKEN), real endpoints/SSE (Last-Event-ID/replay_truncated), references auth-security (not re-spec'd), subsystem altitude in Purpose.
- **operator-views-logs (OPS-05):** Real log_viewer (list/show/tail/cleanup), operator_views taxonomy enums + route prefixes, cli/tui handle_tui_key/REVIEWER_ENV — all grounded.

No whilly/ Python changes; no delta headers; OPS-01..05 each marked done with 1:1 spec mapping.

---

_Verified: 2026-06-16_
_Verifier: Claude (gsd-verifier)_
