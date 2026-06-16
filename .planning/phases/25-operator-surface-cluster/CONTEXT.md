---
phase: 25-operator-surface-cluster
type: context
requirements: [OPS-01, OPS-02, OPS-03, OPS-04, OPS-05]
source: orchestrator-authored (autonomous run)
---

# Phase 25 Context — Operator Surface Cluster

## Goal

Capture the 5 operator-surface contracts as **normative, machine-checkable** OpenSpec specs,
each **reverse-spec'd from the real v4.7.0 code**, passing `openspec validate <slug> --strict`.

## Grounding discipline

READ the modules; spec observed behavior; state wiring/legacy status truthfully. The
plan-checker and verifier adversarially check every requirement against source.

## 5 specs to write (one per slug)

| Req | Slug | Reverse-spec from | Altitude / cautions |
|-----|------|-------------------|---------------------|
| OPS-01 | `dashboard-tui` | `whilly/dashboard.py`, `whilly/cli/dashboard.py`, `whilly/api/dashboard.py` | Rich Live TUI states + hotkeys; web dashboard (SSE) — spec what the code renders/handles |
| OPS-02 | `web-status-ui` | `whilly/adapters/transport/{server,client,auth,schemas,__init__}.py`, `whilly/api/{main,plans_api,tasks_api,tasks_api_crud,metrics,sse,sse_endpoint,event_flusher,static_mount,__init__}.py`, `whilly/cli/server.py`, `whilly/web_status.py` | FastAPI control plane + worker HTTP transport + SSE + web status. **Reference `auth-security` (Phase 26) for the full auth model**; here cover the transport bootstrap-token/per-worker-bearer RPC surface + endpoints. Subsystem altitude. |
| OPS-03 | `reporting` | `whilly/reporter.py` | per-iteration JSON + end-of-run Markdown reporting. VERIFY+STATE its v4 wiring status (may be legacy/unwired in the worker-claim path, like reporter/dashboard from v3). |
| OPS-04 | `cli-surface` | `whilly/cli/__init__.py` (`main`, `_print_help`, the v3-compat shim), `whilly/cli/__main__.py`, `whilly/__main__.py`, `whilly/__init__.py`, `whilly/cli/feedback.py`, `whilly/feedback.py`, `whilly/cli/skill.py` | **GROUNDING CAUTION:** spec the REAL v4 exit codes, NOT the legacy "0/1/2/3" set named in ROADMAP/REQUIREMENTS. Real v4: 0=ok, 1=validation error, 2=environment failure, 3=timeout (legacy), 4=workspace-prep failure (see CLAUDE.md + cli/run.py/plan.py EXIT_* constants — verify per command). No-args prints HELP (`_print_help`), NOT an interactive menu. Headless JSON: verify `WHILLY_HEADLESS` behavior in v4 before asserting. Cover the subcommand dispatch + v3-compat flag shim. |
| OPS-05 | `operator-views-logs` | `whilly/log_viewer.py`, `whilly/operator_views.py`, `whilly/cli/tui.py` | operator views + log viewer behavior |

(Authoritative module→capability assignments: `openspec/COVERAGE-MATRIX.md`.)

## Boundaries

- `web-status-ui` references `auth-security` (Phase 26) for session/OIDC/WebAuthn — don't re-spec auth; cover the transport RPC + web/API surface and the bootstrap-token/per-worker-bearer used by workers.
- `cli-surface` exit codes must match the real EXIT_* constants per command, not the v3 headless 0/1/2/3 lore.
- `dashboard-tui` (Rich Live) vs `web-status-ui` (FastAPI web dashboard) vs `operator-views-logs` (log viewer/operator views) — keep boundaries clean; reference, don't duplicate.
- Reference earlier capabilities (orchestration-loop, state-persistence) where surfaces read them.

## Spec format

Mirror `openspec/specs/task-model-fsm/spec.md`; follow `openspec/AUTHORING.md`. `## Purpose`
(≥50 chars) → `## Requirements` with `### Requirement:` blocks (FIRST body line contains
SHALL/MUST, ≤500 chars) each ≥1 `#### Scenario:` (WHEN/THEN).

## Out of scope

Phases 26–27 capabilities; any `whilly/` Python changes. **Documentation only.**

## Success criteria (ROADMAP)

1. 5 capabilities specced.
2. `cli-surface` spec captures flags, headless JSON output, and the real exit-code contract.
3. Each spec ≥1 scenario; all 5 pass `openspec validate --strict`.
4. Covered modules accounted for in the coverage matrix.
