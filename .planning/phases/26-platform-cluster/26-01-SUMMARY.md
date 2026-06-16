---
phase: 26-platform-cluster
plan: 01
subsystem: configuration
tags: [openspec, configuration, env-vars, toml, secrets, project-config]
requires:
  - openspec/AUTHORING.md (format rules)
  - whilly/config.py (reverse-spec source)
provides:
  - openspec/specs/configuration/spec.md (normative configuration capability spec)
affects:
  - .planning/REQUIREMENTS.md (PLAT-01 marked complete)
  - .planning/STATE.md (Current Position advanced to Phase 26)
tech-stack:
  added: []
  patterns: [reverse-spec-from-code, openspec-strict-validation]
key-files:
  created:
    - openspec/specs/configuration/spec.md
  modified:
    - .planning/REQUIREMENTS.md
    - .planning/STATE.md
decisions:
  - "Spec only the env/TOML layering + secret schemes + project-config surface; reference auth-security for secret *handling* rather than duplicate it."
  - "State WHILLY_WORKTREE/USE_WORKSPACE/USE_TMUX/STATE_FILE/ORCHESTRATOR as truthful no-ops in v4, not live behavior."
  - "Pin documented defaults from the real v4 dataclass: MODEL=claude-opus-4-6[1m], MAX_PARALLEL=3, HEARTBEAT_INTERVAL=1, LOG_DIR=whilly_logs, MAX_ITERATIONS=0, BUDGET_USD=0.0, MAX_TASK_RETRIES=5."
metrics:
  duration: ~15m
  completed: 2026-06-16
  tasks: 1
  files: 3
---

# Phase 26 Plan 01: Configuration Capability Spec Summary

Normative OpenSpec `configuration` capability reverse-spec'd from the real v4
config layer — enumerating the `WHILLY_` env-var contract and defaults, the
five-layer precedence pipeline, `_coerce` typing, the `env:`/`keyring:`/`file:`
secret schemes, the project-config surface, and the truthful no-op state
fields — passing `openspec validate configuration --strict` (exit 0).

## What was built

`openspec/specs/configuration/spec.md` with `## Purpose` (≥50 chars) and
`## Requirements` containing six `### Requirement:` blocks, each with a
SHALL/MUST first body line and ≥1 `#### Scenario:` (WHEN/THEN):

1. **WHILLY_ env-var contract and documented defaults** — `WhillyConfig`
   fields from `WHILLY_<FIELD>`; defaults `MODEL=claude-opus-4-6[1m]`,
   `MAX_PARALLEL=3`, `HEARTBEAT_INTERVAL=1`, `MAX_ITERATIONS=0`,
   `LOG_DIR=whilly_logs`, `BUDGET_USD=0.0` (unlimited), `TIMEOUT=0`,
   `MAX_TASK_RETRIES=5`; plus the bare `SLACK_ACCESS_TOKEN` convenience.
2. **Layered precedence** — `load_layered` order defaults → user TOML → repo
   `whilly.toml` → `.env` → shell `WHILLY_*` → CLI; scenarios prove repo-over-
   user, shell-over-TOML, and "only explicitly-set WHILLY_* fields override".
3. **Type coercion** — `_coerce` int/float/bool, with the exact falsey token
   set (`0|false|no|off|""`).
4. **Secret-reference schemes** — `resolved()` resolving `env:`/`keyring:`/
   `file:`; missing secret → empty string (never raises); non-strings pass
   through.
5. **Project-config surface** — `load_project_config`/`preset_pipeline`/
   resolver + `project-config`/`project-map`/`quick-setup` CLI; plaintext
   secret-like values rejected in favour of secret references.
6. **Legacy state fields as no-ops** — `WHILLY_WORKTREE`,
   `WHILLY_USE_WORKSPACE`, `WHILLY_USE_TMUX`, `WHILLY_STATE_FILE`,
   `WHILLY_ORCHESTRATOR` parsed but inert in v4.

## Verification

- `openspec validate configuration --strict` → "Specification 'configuration'
  is valid", exit 0 (0 errors, 0 warnings).
- Reverse-spec grounding read in full: `whilly/config.py`,
  `whilly/secrets.py`, `whilly/config_sections.py`,
  `whilly/external_integrations.py`, `whilly/project_config/{loader,resolver,
  presets}.py`, `whilly/cli/{project_config,project_map,quick_setup}.py`,
  `openspec/AUTHORING.md`, exemplar `openspec/specs/task-model-fsm/spec.md`.

## Deviations from Plan

Removed the placeholder `openspec/specs/configuration/.gitkeep` when adding the
real `spec.md` (the stub directory was created in Phase 21-02). Housekeeping
only — the directory now carries its capability spec.

Otherwise: none — plan executed exactly as written. Documentation-only; zero
`whilly/` changes.

## Known Stubs

None. The spec is fully authored; no placeholder/empty requirement bodies.

## Self-Check: PASSED

- openspec/specs/configuration/spec.md — FOUND
- Commit 769560c — FOUND
- `openspec validate configuration --strict` — exit 0, valid
