---
phase: 26-platform-cluster
type: context
requirements: [PLAT-01, PLAT-02, PLAT-03, PLAT-04, PLAT-05]
source: orchestrator-authored (autonomous run)
---

# Phase 26 Context — Platform Cluster

## Goal

Capture the 5 platform contracts as **normative, machine-checkable** OpenSpec specs, each
**reverse-spec'd from the real v4.7.0 code**, passing `openspec validate <slug> --strict`.

## Grounding discipline

READ the modules; spec observed behavior; state wiring/legacy status truthfully. Several
REQUIREMENTS.md wordings below carry v3 lore — supersede with real v4 behavior (the spec is
source-grounded; the plan-checker and verifier adversarially confirm).

## 5 specs to write (one per slug)

| Req | Slug | Reverse-spec from | Cautions / altitude |
|-----|------|-------------------|---------------------|
| PLAT-01 | `configuration` | `whilly/config.py` (`WhillyConfig`, `from_env`, `load_layered`, `_coerce`, `resolved()` env:/keyring:/file: schemes), `config_sections.py`, `external_integrations.py`, `project_config/*` (loader/models/plan_builder/presets/resolver), `cli/{project_config,project_map,quick_setup}.py`, `adapters/__init__.py` | Env-var contract + layered precedence (defaults→user TOML→repo whilly.toml→.env→WHILLY_*→CLI). State no-op fields truthfully (WHILLY_WORKTREE/USE_WORKSPACE/USE_TMUX/STATE_FILE/ORCHESTRATOR). |
| PLAT-02 | `auth-security` | `whilly/api/*` auth set (auth_routes, auth_tokens, sessions, oidc_header_auth, webauthn_*, totp_*, second_factor, passwords, must_change_gate, csrf, rate_limit, route_audit, dashboard_token, admin_users_routes, users_repo, auth_audit_repo, prod_mode), `whilly/cli/admin.py`, `whilly/secrets.py`, `whilly/security/*` (prompt_sanitizer, secret_lint) | **Security-sensitive — subsystem altitude.** Cover: session auth, gated/forced password change (must_change_gate), flag-gated OIDC/WebAuthn/TOTP, CSRF, login rate-limit, route auth audit, prod-mode hardening, secret handling/lint, prompt sanitization, and the **ADR-001 path-traversal sink mitigation** (`core/task_id.validate_task_id` — the task-id sink class). See `.planning/E15-E17-auth-security-design.md` for shipped intent. Reference (don't duplicate) web-status-ui transport tokens. |
| PLAT-03 | `scheduling` | `whilly/scheduler/*` (config, deduplicator, docs, jql_executor, metrics, models, rate_limit, repository, sql_repository, webhooks, worker), `whilly/core/scheduler.py`, `whilly/cli/scheduler.py` | Scheduler behavior: rules/cycles, JQL execution, dedup, webhooks, rate limiting, Postgres-backed repository. |
| PLAT-04 | `state-persistence` | **Real v4 persistence**: `whilly/adapters/db/{pool,repository}.py`, `adapters/db/migrations/*` (Alembic chain 001–028), plus `whilly/state_store.py`, `history.py`, `pause_control.py` | **GROUNDING CAUTION:** REQUIREMENTS PLAT-04 says "StateStore resume contract (plan/iteration/cost/sessions)" — that is v3 lore. In v4 state lives in **Postgres** (pool/repository/migrations); `.whilly_state.json`/`WHILLY_STATE_FILE` are no-ops. VERIFY whether `state_store.py`/`history.py`/`pause_control.py` are wired in the v4 worker-claim path or legacy, and spec the REAL persistence layer (asyncpg pool, TaskRepository optimistic-locking + version, events audit, migrations) as primary. The 37 mapped modules are mostly generated migrations — batch-reference them, spec at subsystem altitude. |
| PLAT-05 | `self-update-doctor` | `whilly/update.py`, `cli/update.py`, `whilly/doctor.py`, `whilly/rollback/*` (git_ops, models, service), `cli/rollback.py`, `whilly/repair/*` (events, models, policy, tasks) | update / doctor / repair / rollback behaviors. State wiring where relevant. |

(Authoritative module→capability assignments: `openspec/COVERAGE-MATRIX.md`.)

## Boundaries

- `auth-security` is the full auth spec; web-status-ui (Phase 25) only referenced it — now define it. Cover the ADR-001 path-sink mitigation explicitly.
- `state-persistence` spec the real Postgres layer; do NOT pin the v3 `.whilly_state.json` StateStore as the live contract unless the code proves it's wired.
- `configuration` covers env/TOML layering + secret schemes; reference auth-security for secret *handling* if overlapping, but the env-var contract lives here.
- Reference earlier capabilities (orchestration-loop, task-model-fsm) where platform serves them.

## Spec format

Mirror `openspec/specs/task-model-fsm/spec.md`; follow `openspec/AUTHORING.md`. `## Purpose`
(≥50 chars) → `## Requirements` with `### Requirement:` (FIRST body line SHALL/MUST, ≤500
chars) each ≥1 `#### Scenario:` (WHEN/THEN).

## Out of scope

Phase 27 capabilities; any `whilly/` Python changes. **Documentation only.**

## Success criteria (ROADMAP)

1. 5 capabilities specced.
2. `configuration` enumerates the env-var contract + defaults; `auth-security` covers session/gated-password/flag-gated OIDC-WebAuthn + the path-sink mitigation.
3. Each spec ≥1 scenario; all 5 pass `openspec validate --strict`.
4. Covered modules accounted for in the coverage matrix.
