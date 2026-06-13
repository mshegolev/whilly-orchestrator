# Whilly Project Context for OpenSpec

This document provides the tech stack, load-bearing contracts, and domain
glossary for Whilly capability specs. All spec authors MUST read this document
before writing or reviewing a spec under `openspec/specs/`.

---

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Language | Python 3.10+ (CI matrix: 3.10 / 3.11 / 3.12) |
| Entry point | `whilly.cli:main` (console script `whilly`) |
| TUI | Rich (Rich Live, dashboard hotkeys) |
| Web API | FastAPI |
| Database layer | SQLAlchemy |
| Test runner | pytest (async via pytest-asyncio) |
| Lint / format | ruff (line length 120, target `py310`) |
| Agent integration | Claude CLI — shelled out via `CLAUDE_BIN` or `claude` on PATH |
| State file | `.whilly_state.json` (JSON, atomic writes via `StateStore`) |
| Events log | `whilly_logs/whilly_events.jsonl` |

---

## Conventions

### Environment Variable Contract

All runtime configuration is read by `WhillyConfig.from_env()` in
`whilly/config.py`. Every environment variable uses the `WHILLY_` prefix.

Key variables:

| Variable | Default | Meaning |
|----------|---------|---------|
| `WHILLY_MAX_PARALLEL` | 3 | Maximum concurrent agent tasks |
| `WHILLY_BUDGET_USD` | (configured) | Hard budget ceiling in USD |
| `WHILLY_MODEL` | `claude-opus-4-6[1m]` | Claude model for agents |
| `WHILLY_USE_TMUX` | (auto-detected) | Use tmux runner when available |
| `WHILLY_HEADLESS` | false | Emit structured JSON; suppress TUI |
| `WHILLY_TIMEOUT` | (configured) | Wall-clock timeout in seconds |
| `WHILLY_STATE_FILE` | `.whilly_state.json` | Resume state file path |
| `WHILLY_USE_WORKSPACE` | false (off since v3.3.0) | Enable plan-level worktree |
| `WHILLY_WORKTREE` | false | Enable per-task worktree isolation |
| `WHILLY_LOG_DIR` | `whilly_logs/` | Directory for event log and reports |
| `CLAUDE_BIN` | `claude` (on PATH) | Path to Claude CLI binary |

### Task Status FSM

A task progresses through exactly five legal status values:

```
pending → in_progress → done
                      → failed
                      → skipped
```

- `pending`: not yet started; eligible for dispatch when dependencies are met.
- `in_progress`: agent is running; transitions to `done`, `failed`, or `skipped`.
- `done`: terminal — task completed successfully; not re-run.
- `failed`: terminal — task failed after exhausting retries (typically auth
  failure); not retried further.
- `skipped`: terminal — task was deadlocked (stuck `in_progress` >= 3 iterations)
  and abandoned; not retried.

Stale `in_progress` tasks found at startup are reset to `pending` before
dispatch. Terminal states (`done`, `failed`, `skipped`) are immutable once set.

### Completion Signal

An agent marks its task done by emitting the literal string:

```
<promise>COMPLETE</promise>
```

in its output text. The result parser in `agent_runner.collect_result` checks
for this exact string. Any agent that exits without emitting it is treated as
incomplete; the task may be retried.

### Exit Code Contract

Whilly uses four exit codes for machine-readable completion signaling:

| Exit Code | Meaning |
|-----------|---------|
| `0` | All tasks completed successfully |
| `1` | One or more tasks failed |
| `2` | Budget ceiling exceeded (all tmux sessions killed) |
| `3` | Wall-clock timeout reached |

In `--headless` mode (or when stdout is not a TTY), `whilly` emits structured
JSON on stdout. The exit codes are the canonical machine-readable signal for CI.

### Plan JSON Envelope

A plan file MUST be valid JSON with this top-level shape:

```json
{
  "project": "<project name>",
  "prd_file": "<path to PRD Markdown file or null>",
  "tasks": [ ... ]
}
```

Each task object carries these fields (extra keys are tolerated but dropped
on round-trip by `Task.to_dict`):

| Field | Required | Type | Notes |
|-------|----------|------|-------|
| `id` | YES | string | Unique task identifier within the plan |
| `status` | YES | string | One of the five FSM values |
| `dependencies` | YES | list[str] | IDs of tasks that must be `done` first |
| `key_files` | YES | list[str] | Files the task reads or writes (for batch isolation) |
| `priority` | YES | string | `critical`, `high`, `medium`, or `low` |
| `description` | YES | string | Human-readable task description |
| `acceptance_criteria` | YES | string | Completion definition for the agent |
| `test_steps` | YES | string | Steps to verify the task outcome |

Schema validation by `cli.validate_schema` checks only the first 3 tasks.

### Retry and Deadlock Policy

- Exponential backoff on API errors: 5 / 10 / 20 / 40 / 60 seconds.
- `MAX_TASK_RETRIES` (default 5) before a task is marked `skipped` (deadlock)
  or `failed` (auth failure).
- A task stuck `in_progress` for >= 3 consecutive iterations is marked `skipped`.
- 5 consecutive iterations with no `done_count` progress → pause 60 seconds.

### Budget Guards

- At 80% of `WHILLY_BUDGET_USD`: warning emitted to dashboard and log.
- At 100% of `WHILLY_BUDGET_USD`: all tmux sessions killed, process exits with
  code 2.

---

## Domain Glossary

| Term | Definition |
|------|-----------|
| **capability** | A named subsystem-level behavior cluster that maps to one `openspec/specs/<slug>/spec.md` |
| **plan** | A JSON file with `{project, prd_file, tasks: [...]}` describing one execution run |
| **task** | A unit of agent work with an FSM status, dependencies, key_files, and acceptance criteria |
| **agent** | A Claude CLI process dispatched per task, identified by `whilly-{task_id}` in tmux |
| **workspace** | An optional git worktree at `.whilly_workspaces/{slug}/` isolating a plan's execution (off by default since v3.3.0; enable with `--workspace` / `WHILLY_USE_WORKSPACE=1`) |
| **worktree** | A per-task git worktree at `.whilly_worktrees/{task_id}/` for parallel isolation (enable with `WHILLY_WORKTREE=1` and `MAX_PARALLEL > 1`) |
| **tmux session** | Named tmux session (`whilly-{task_id}`) hosting one agent process |
| **PRD** | Product Requirements Document — a Markdown file that is the input to task generation |
| **Decision Gate** | The pre-execution filter that applies TRIZ contradiction analysis to refuse nonsense or contradictory tasks before any agent is dispatched |
| **TRIZ** | Inventive principles framework used by the Decision Gate to identify contradictions in task definitions |
| **StateStore** | Persists iteration count, cumulative cost, per-task status, and live tmux session names for `--resume` recovery |
| **opsx** | The `openspec` change proposal workflow — propose → apply → archive — used for forward-delta spec updates |
| **delta spec** | A spec fragment under `openspec/changes/<name>/specs/<capability>/spec.md` that describes additions, modifications, or removals relative to a baseline capability spec |
| **coverage matrix** | The `openspec/COVERAGE-MATRIX.md` table mapping every `whilly/` Python module to exactly one capability slug; zero silent gaps required |

---

## Normative Language Convention

Capability specs in `openspec/specs/*/spec.md` use RFC 2119 normative language.

| Keyword | Strength | Use |
|---------|----------|-----|
| `SHALL` | Unconditional | Required in every requirement body line |
| `MUST` | Same as SHALL | Required in every requirement body line |
| `SHALL NOT` | Prohibition | Explicit prohibitions |
| `MUST NOT` | Same as SHALL NOT | Explicit prohibitions |
| `should` | Recommended | AVOID — not machine-checkable by the validator |
| `may` | Optional | AVOID — not machine-checkable |

Every requirement body line in a capability spec MUST start with a normative
assertion using `SHALL` or `MUST`. Descriptive language ("the module reads...",
"the dashboard shows...") is prohibited in requirement bodies.

---

## Spec Location Pattern

```
openspec/specs/<capability-slug>/spec.md
```

The capability slug is the directory name and the spec ID. Slugs MUST be
kebab-case. The authoring rules are in `openspec/AUTHORING.md`.
