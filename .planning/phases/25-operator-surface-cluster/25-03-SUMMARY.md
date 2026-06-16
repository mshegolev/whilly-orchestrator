---
phase: 25-operator-surface-cluster
plan: 03
subsystem: operator-surface
tags: [openspec, cli-surface, operator-views-logs, reverse-spec, documentation-only]
requires:
  - openspec/AUTHORING.md
  - openspec/specs/task-model-fsm/spec.md (exemplar)
provides:
  - openspec/specs/cli-surface/spec.md (OPS-04)
  - openspec/specs/operator-views-logs/spec.md (OPS-05)
affects:
  - openspec/COVERAGE-MATRIX.md (cli + log/operator-view modules now specced)
tech-stack:
  added: []
  patterns: [reverse-spec-from-source, normative-shall-must, scenario-when-then]
key-files:
  created:
    - openspec/specs/cli-surface/spec.md
    - openspec/specs/operator-views-logs/spec.md
  modified:
    - .planning/REQUIREMENTS.md
    - .planning/STATE.md
decisions:
  - "cli-surface exit codes pinned to the REAL v4 EXIT_* constants (0/1/2/-4), NOT the legacy v3 0/1/2/3 budget/timeout lore."
  - "WHILLY_HEADLESS is documented as a v3-compat env var the shim SETS but no v4 subcommand reads — spec states this truthfully rather than asserting a headless JSON output shape that does not exist."
  - "TUI surface-switch hotkeys specced as sequential digits 1..N (from operator_surface_hotkeys), not the surface enum values."
metrics:
  duration: ~12m
  completed: 2026-06-16
---

# Phase 25 Plan 03: Operator Surface Specs (cli-surface + operator-views-logs) Summary

Reverse-spec'd two operator surfaces from real Whilly v4.7.0 code into normative,
strict-valid OpenSpec capability specs: the `whilly` CLI surface (OPS-04) and the
operator log viewer + operator-views taxonomy + operator TUI (OPS-05). Both pass
`openspec validate <slug> --strict` with zero errors and zero warnings.
Documentation-only — no `whilly/` Python changes.

## What was built

### Task 1 — cli-surface (OPS-04) — commit 32d8d0d
`openspec/specs/cli-surface/spec.md` reverse-spec'd from `whilly/cli/__init__.py`
(`main`, `_print_help`, `_apply_legacy_shim`), `whilly/cli/plan.py`,
`whilly/cli/run.py`, `whilly/workspaces.py`, the entry shims, and `whilly/__init__.py`.

Pinned the REAL v4 exit-code contract from the verified `EXIT_*` constants:
- `EXIT_OK = 0` (success; also the help and version fast paths)
- `EXIT_VALIDATION_ERROR = 1` (plan.py: schema / missing-field / dependency-cycle)
- `EXIT_ENVIRONMENT_ERROR = 2` (plan.py + run.py: plan absent / env failure; also
  the `main` unknown-command path returns 2)
- `WORKSPACE_FAILED_EXIT_CODE = -4` (workspaces.py; surfaced via run.py on
  workspace-prep failure)

Also captured: the run command's intentional absence of a validation-error (1)
path (argparse SystemExits first); no-args / `-h`/`--help` print HELP via
`_print_help` and return 0 (NOT an interactive menu); `-V`/`--version` prints
`whilly <__version__>` and returns 0; unknown command writes a diagnostic + help
to stderr and returns 2; the v3 legacy flag shim rewrites
`--tasks`/`--init`/`--prd-wizard`/`--from-jira`/`--reset` into v4 subcommands,
treats `--resume`/`--all` as exit-0 no-ops, consumes `--headless` by exporting
`WHILLY_HEADLESS=1`, and strips `--workspace`/`--worktree`/`--no-workspace`/
`--no-worktree` no-ops.

### Task 2 — operator-views-logs (OPS-05) — commit 66bf4af
`openspec/specs/operator-views-logs/spec.md` reverse-spec'd from
`whilly/log_viewer.py`, `whilly/operator_views.py`, and `whilly/cli/tui.py`.

Captured: `whilly logs --list` → `cmd_list`; `whilly logs <task_id>` → `cmd_show`
(prompt + events + stdout, with global-jsonl fallback, returns 1 when no artifact);
missing task id → usage + exit 2; `--tail`/`-f <task_id>` → `cmd_tail` byte-offset
poll loop, exit 0 on interrupt; `cleanup_old_logs` TTL removal sparing
`whilly.log*`, no-op when TTL <= 0. The operator-views taxonomy: the
`OperatorSurface`/`OperatorTable`/`OperatorAction` enums, `OPERATOR_ACTIONS`
hotkey bindings (q/r/p/R//, review j/k/a/x/c scoped to COMPLIANCE), sequential
digit surface-switch hotkeys, WUI route prefixes (`/api/v1/admin/workers/`,
`/api/v1/tasks/`), and the `OPERATOR_WUI_ARTIFACTS` inventory with status +
follow-up phase. The TUI: `handle_tui_key` single-hotkey state machine, reviewer
identity required (`--reviewer` / `WHILLY_OPERATOR_EMAIL`) for human-review
decisions, and exit 2 when `WHILLY_DATABASE_URL` is unset.

## Verification

- `openspec validate cli-surface --strict` → "Specification 'cli-surface' is valid"
- `openspec validate operator-views-logs --strict` → "Specification 'operator-views-logs' is valid"
- Both files contain `## Purpose` (>= 50 chars) and `## Requirements`; every
  `### Requirement:` has SHALL/MUST on the first body line and >= 1 `#### Scenario:`.
- No delta headers in either main spec.

## Deviations from Plan

None — plan executed exactly as written. Both tasks autonomous, no checkpoints,
no auth gates, no Rules 1-4 deviations.

## Grounding notes (truthful status)

- `WHILLY_HEADLESS` is SET by the legacy shim but read by no v4 subcommand
  (verified: only `whilly/cli/__init__.py` and tests reference it; `init` uses its
  own `--headless`/TTY detection in `_resolve_mode`). The spec documents this
  honestly rather than inventing a headless JSON output contract.
- The plan/CONTEXT mentioned a possible "3=timeout (legacy)" and "4=workspace"
  code; the real source has no `3` exit code in these handlers and the workspace
  code is `-4` (negative), surfaced as an `AgentResult.exit_code`. Specced the
  real constants only.

## Self-Check: PASSED

- FOUND: openspec/specs/cli-surface/spec.md
- FOUND: openspec/specs/operator-views-logs/spec.md
- FOUND commit: 32d8d0d (cli-surface)
- FOUND commit: 66bf4af (operator-views-logs)
