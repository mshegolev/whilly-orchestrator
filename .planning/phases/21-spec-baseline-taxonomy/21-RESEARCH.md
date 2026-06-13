# Phase 21: Spec Baseline & Taxonomy — Research

**Researched:** 2026-06-13
**Domain:** OpenSpec 1.4.1 (spec-driven schema) — capability taxonomy, authoring
conventions, coverage matrix, project.md
**Confidence:** HIGH — all critical claims verified by running the installed CLI
and reading the installed package source code directly

---

<phase_requirements>
## Phase Requirements

| ID | Description | Research Support |
|----|-------------|-----------------|
| BASE-01 | Capability taxonomy of ~30 capabilities under `openspec/specs/`, with naming convention and one-line purpose per capability | Taxonomy scaffold derived from REQUIREMENTS.md phase clusters (22–27); see Recommended Taxonomy section |
| BASE-02 | `module → capability` coverage matrix mapping all 242 `whilly/` modules to exactly one capability | All 244 `.py` files enumerated; 242 unique modules (excluding `__init__` files where appropriate); matrix format designed below |
| BASE-03 | Authoring-conventions doc (MUST/SHALL normative language, `#### Scenario:` format that `openspec validate --strict` accepts) | Fully verified by reading validator.js, base.schema.js, markdown-parser.js and running live probe tests against the installed CLI |
| BASE-04 | `openspec/project.md` (or config context) carries Whilly's tech stack, conventions, and domain glossary | Location decision researched and recommended below; content outline provided |
</phase_requirements>

---

## Summary

Phase 21 is a pure documentation and scaffolding phase — no `whilly/` code changes.
Its output is the spec format + taxonomy that every later phase (22–28) builds on.
Getting the format wrong cascades across 30 specs; getting it right means the rest
of the milestone is mechanical.

The primary research vehicle was the installed OpenSpec 1.4.1 CLI at
`~/.reflex/.nvm/versions/node/v20.19.6/bin/openspec`, its compiled dist files
(schema, validator, parser), and live probe tests run against a temporary
`/tmp/test-openspec-probe/` project. Every format claim in this document is
`[VERIFIED]` from that source, not assumed.

The key insight from reading the source: the validator `SpecSchema` (Zod schema)
checks the **`requirements[i].text`** field for `SHALL` or `MUST`, and that field
is populated from the **body line immediately after `### Requirement:`**, not from
the header text. If the body line is empty, the header text is used as fallback.
In practice, writing the normative statement as the first body line (not in the
header) is the conventional pattern and avoids confusion.

**Primary recommendation:** Write all capability specs in `openspec/specs/<slug>/spec.md`
with exactly two `##` sections (`## Purpose` ≥ 50 chars, `## Requirements`), each
requirement as `### Requirement: <name>` + normative body line + at least one
`#### Scenario:` block. Use the `task-model-fsm` capability as the Phase 21
reference exemplar (most self-contained, strongest contract, well-bounded scope).

---

## Architectural Responsibility Map

This phase produces documentation artifacts, not code. The "tiers" are document
types within the spec ecosystem.

| Capability | Primary Tier | Secondary Tier | Rationale |
|------------|-------------|----------------|-----------|
| Capability taxonomy (BASE-01) | `openspec/specs/` index | `.planning/phases/21-*/` authoring doc | Taxonomy lives where consumers (phases 22–27) find it |
| Coverage matrix (BASE-02) | `openspec/` directory | `openspec/specs/` adjacent file | Must be checkable by `openspec validate --specs` and auditable by humans |
| Authoring conventions (BASE-03) | `openspec/AUTHORING.md` | Referenced from `openspec/config.yaml` context | Not inside a spec (conventions doc is not itself a capability spec) |
| Project glossary (BASE-04) | `openspec/project.md` | `openspec/config.yaml` context field | `project.md` is the `spec-driven` schema's intended home for this |
| Reference exemplar (SC-5) | `openspec/specs/task-model-fsm/spec.md` | Validates with `openspec validate --strict` | One working, validated example anchors all subsequent specs |

---

## OpenSpec 1.4.1 Spec Format (VERIFIED)

### Physical Location of a Capability Spec

[VERIFIED: openspec dist/core/validation/validator.js + live probe tests]

```
openspec/specs/<capability-slug>/spec.md
```

The `extractNameFromPath` function in the validator looks for a directory named
`specs` in the path, then uses the next path component as the spec name. The spec
slug MUST be the directory name. Kebab-case is the naming convention per the
proposal template instruction (`user-auth`, `data-export`).

### Minimal Valid Spec (confirmed passing `openspec validate --strict`)

[VERIFIED: live probe test — see `/tmp/test-openspec-probe/openspec/specs/shall-in-body/`]

```markdown
## Purpose

<at least 50 characters describing this capability's purpose
and scope in plain prose — one paragraph is fine>

## Requirements

### Requirement: <requirement name>
The system SHALL <normative statement on the first body line>.

#### Scenario: <scenario name>
- **WHEN** <condition or trigger>
- **THEN** <expected outcome>
```

This is the **minimal** spec. Every addition (more requirements, more scenarios,
AND clauses) is additive and continues to pass strict validation.

### The Two Required `##` Sections

[VERIFIED: markdown-parser.js `parseSpec()` + live probe tests]

| Section | Required? | Min Content | Strict Failure Mode |
|---------|-----------|-------------|---------------------|
| `## Purpose` | YES | ≥ 50 characters | WARNING (→ strict failure) if < 50 chars |
| `## Requirements` | YES | ≥ 1 `### Requirement:` child | ERROR if missing section; ERROR if 0 requirements |

The parser calls `findSection(sections, 'Purpose')` and `findSection(sections, 'Requirements')`
(case-insensitive). If either is absent the spec throws an error and fails.

### Requirement Block Format

[VERIFIED: base.schema.js RequirementSchema, markdown-parser.js `parseRequirements()`,
validator.js `applySpecRules()`]

```markdown
### Requirement: <human-readable name>
<First body line MUST contain SHALL or MUST — this is what the validator checks>

#### Scenario: <scenario name>
- **WHEN** <condition>
- **THEN** <outcome>
- **AND** <additional outcome> (optional, any number)
```

**Critical mechanics verified:**

1. `### Requirement:` — exactly 3 hashtags. The parser matches `### Requirement: (.+)`.
2. **Body line**: The validator checks `requirements[i].text` for `SHALL` or `MUST`.
   `text` = first non-empty line of `child.content` (lines before the first `####`).
   If there is no body content before the scenario, the header title text is used as
   fallback. Placing `SHALL`/`MUST` in the header title works mechanically but the
   schema instructions say body is the correct location. Writing it in the body is
   clearer and unambiguous.
3. `#### Scenario:` — exactly 4 hashtags. Using 3 or bullet lists does NOT produce
   an error from the validator (the schema only checks `scenarios.length >= 1`), but
   the schema instruction says explicitly: "CRITICAL: Scenarios MUST use exactly
   4 hashtags (`####`). Using 3 hashtags or bullets will fail silently." The delta
   validator for change specs DOES count `^####\s+` lines, confirming this convention.
4. Every requirement needs ≥ 1 scenario. Missing scenario = ERROR (not just WARNING).

### What `--strict` Adds Beyond Normal Validation

[VERIFIED: validator.js `createReport()` + live probe tests]

```javascript
const valid = this.strictMode
  ? errors === 0 && warnings === 0
  : errors === 0;
```

In normal mode: only ERRORs cause failure.
In strict mode: both ERRORs and WARNINGs cause failure.

**WARNINGs that become failures under `--strict`:**

| Warning | Threshold | Location |
|---------|-----------|----------|
| Purpose section too brief | < 50 characters | `## Purpose` content |
| Requirement text too long (INFO only) | > 500 chars | per requirement body |
| Requirement has no scenario | 0 scenarios | auto-downgraded to WARNING after ERROR |

The `brief-purpose` test confirmed: 5-char Purpose → WARNING → strict failure.
The `shall-in-body` test confirmed: 50+ char Purpose + SHALL in body + scenario → passes.

### What Delta Specs Look Like (for Phase 28 understanding)

[VERIFIED: schema.yaml instruction block for `specs` artifact]

Delta specs live under `openspec/changes/<change-name>/specs/<capability>/spec.md`
and use delta headers:

```markdown
## ADDED Requirements

### Requirement: New capability
The system SHALL <new behavior>.

#### Scenario: New scenario
- **WHEN** ...
- **THEN** ...

## MODIFIED Requirements
## REMOVED Requirements
## RENAMED Requirements
```

Main specs (under `openspec/specs/`) MUST NOT contain delta headers. The
`spec-structure.js` parser detects `## ADDED/MODIFIED/REMOVED/RENAMED Requirements`
in a main spec file and reports ERROR with a clear message. This was verified
with the `delta-in-main2` probe test.

### Forbidden Anti-Patterns (VERIFIED by live probe tests)

| Anti-Pattern | What Goes Wrong | Error Message |
|--------------|-----------------|---------------|
| `## Purpose` with < 50 chars | WARNING in strict mode → failure | `"Purpose section is too brief (less than 50 characters)"` |
| No `## Requirements` section | ERROR | `"Spec must have a Requirements section"` |
| No `### Requirement:` inside `## Requirements` | ERROR | spec has 0 requirements |
| Requirement body lacks `SHALL`/`MUST` | ERROR | `"Requirement must contain SHALL or MUST keyword"` |
| `### Requirement:` with no scenario | ERROR | `"Requirement must have at least one scenario"` |
| `## ADDED Requirements` in a main spec | ERROR | `"Main spec contains delta header..."` |
| Scenario as bullet list instead of `####` | Silent fail — no scenario parsed | Spec passes schema but scenarios = 0 → then ERROR |
| `### Scenario:` (3 hashtags) | Silent fail | Not parsed as scenario |

---

## Capability Taxonomy (BASE-01)

### Recommended ~30 Capability Set

[CITED: .planning/REQUIREMENTS.md v1.3 capability list from phases 22–27]

The taxonomy is already defined in REQUIREMENTS.md and ROADMAP.md. Phase 21's
job is to formalize it — write the index, assign slugs, define one-line purposes,
and create the `openspec/specs/<slug>/` directory stubs. Do not invent a new
taxonomy; use the one locked in the milestone requirements.

**Taxonomy by cluster (29 capabilities total):**

| Cluster | Phase | Slug | One-Line Purpose |
|---------|-------|------|-----------------|
| Orchestration | 22 | `orchestration-loop` | The `run_plan` iteration model: load → ready → batch → dispatch → collect → guard → persist |
| Orchestration | 22 | `task-model-fsm` | Task status state machine: pending → in_progress → done \| failed \| skipped |
| Orchestration | 22 | `plan-json-contract` | Required task fields and plan envelope (`project`, `prd_file`, `tasks[]`) |
| Orchestration | 22 | `batch-planning` | Non-overlapping `key_files` batching and first-batch dispatch re-evaluation |
| Orchestration | 22 | `agent-dispatch` | tmux vs subprocess runner selection and per-task isolation preconditions |
| Orchestration | 22 | `worktree-isolation` | Plan workspace and per-task worktree lifecycle: create → cherry-pick → cleanup |
| Orchestration | 22 | `result-collection` | `AgentResult` parsing and the `<promise>COMPLETE</promise>` completion signal |
| PRD Pipeline | 23 | `prd-generation` | Non-interactive PRD synthesis from a description |
| PRD Pipeline | 23 | `prd-wizard` | Interactive PRD authoring via Claude CLI |
| PRD Pipeline | 23 | `task-generation` | PRD → `tasks.json` generation contract |
| PRD Pipeline | 23 | `decomposition` | Mid-run splitting of oversized pending tasks every `DECOMPOSE_EVERY` iterations |
| PRD Pipeline | 23 | `decision-gate` | Decision Gate + TRIZ contradiction analysis refuse/accept criteria |
| Integrations | 24 | `jira-integration` | Jira read/work-snapshot behavior and auth expectations |
| Integrations | 24 | `gitlab-integration` | GitLab CLI surface behavior |
| Integrations | 24 | `github-integration` | GitHub PR/projects/converter behavior |
| Integrations | 24 | `jira-watcher-daemon` | Watch loop daemon lifecycle and fail-closed behavior |
| Integrations | 24 | `notifications` | Slack/sink notification dispatch |
| Integrations | 24 | `mcp-integration` | MCP server/client integration surface |
| Operator Surface | 25 | `dashboard-tui` | Rich Live TUI dashboard states and hotkeys |
| Operator Surface | 25 | `web-status-ui` | Web status and API surface behavior |
| Operator Surface | 25 | `reporting` | Per-iteration JSON and end-of-run Markdown reporting |
| Operator Surface | 25 | `cli-surface` | CLI flags, headless JSON output, and exit codes (0/1/2/3) |
| Operator Surface | 25 | `operator-views-logs` | Operator views and log viewer behavior |
| Platform | 26 | `configuration` | `WhillyConfig.from_env()` env-var contract and defaults |
| Platform | 26 | `auth-security` | Session auth, gated password change, flag-gated OIDC/WebAuthn, ADR-001 path-sink mitigation |
| Platform | 26 | `scheduling` | Scheduler behavior |
| Platform | 26 | `state-persistence` | `StateStore` resume contract: plan/iteration/cost/sessions |
| Platform | 26 | `self-update-doctor` | Update, doctor, repair, and rollback behaviors |
| Safety | 27 | `budget-resource-guards` | Budget thresholds (80% warn / 100% kill→exit 2) and resource monitoring |
| Safety | 27 | `recovery-self-healing` | Deadlock detection, stall pause, retry/backoff, and self-healing |
| Safety | 27 | `quality-compliance-audit` | Quality/compliance/audit-event behavior |
| Safety | 27 | `verification-gates` | Verifier and human-review gate behavior |

**Total: 32 capabilities.** This is within the "~30" target from REQUIREMENTS.md.

### Naming Convention

[CITED: openspec proposal.md template, schema.yaml instruction block]

- Slugs are kebab-case (e.g., `task-model-fsm`, `cli-surface`)
- Slugs map directly to `openspec/specs/<slug>/spec.md`
- Slug = the directory name = the spec ID returned by `openspec spec list`

### Taxonomy Index Format

[ASSUMED — OpenSpec has no built-in taxonomy index format; this is a
recommendation for a project-level artifact]

The taxonomy index should live at `openspec/TAXONOMY.md` (not inside a capability
spec, not inside the cluster spec). Format:

```markdown
# Whilly Capability Taxonomy

32 capabilities across 6 clusters. Each maps to one spec file.

## Cluster: Orchestration (7)
| Slug | Purpose |
|------|---------|
| `orchestration-loop` | The `run_plan` iteration model... |
...
```

---

## Module → Capability Coverage Matrix (BASE-02)

### Module Count

[VERIFIED: `find whilly/ -name "*.py" -not -path "*/__pycache__/*" | wc -l`]

Total Python files in `whilly/`: **244**

The REQUIREMENTS.md says "242 whilly/ modules." The 2-file discrepancy is
`whilly/__init__.py` and `whilly/__main__.py` — these are package-level files
that may or may not count as "modules" for coverage purposes. The planner should
decide: include all 244 (safest, zero gaps) or exclude `__init__` files and
`__main__` shims. Recommend: include all 244 and note the convention explicitly
in the matrix header.

[ASSUMED — the "242" count in REQUIREMENTS.md may reflect a prior manual count
that excluded `__init__.py` files or used a different method. Needs resolution
before the matrix is finalized.]

### Coverage Matrix Format

[ASSUMED — OpenSpec provides no built-in coverage matrix; this recommendation
is based on the project's needs and maintainability goals]

**Recommendation: A single Markdown table in `openspec/COVERAGE-MATRIX.md`.**

Rationale:
- A Markdown table is human-readable, diff-friendly, and auditable in git
- YAML/JSON is harder to review in PR diffs for 242 rows
- The matrix does not need to be machine-parsed by `openspec` (OpenSpec has no
  built-in coverage command); it is an auditable artifact for Phase 28 COV-01

Format:

```markdown
# Module → Capability Coverage Matrix

**Total modules:** 244 (all .py files under whilly/, including __init__ and __main__)
**Total capabilities:** 32
**Unmapped:** 0 (zero silent gaps — explicit UNMAPPED rows for anything pending)

| Module Path | Capability Slug | Notes |
|-------------|----------------|-------|
| whilly/__init__.py | configuration | Package version/metadata |
| whilly/__main__.py | cli-surface | Entry point shim |
| whilly/agent_runner.py | result-collection | Top-level agent runner |
| whilly/orchestrator.py | batch-planning | Batch planning logic |
| ...244 rows... | | |
```

**Key design decisions:**
1. Every row is explicit. No implicit mapping. Zero silent gaps.
2. `UNMAPPED` is a valid capability slug for Phase 21 — Phase 28 COV-01 must
   clear all UNMAPPED rows. Writing `UNMAPPED` is allowed; leaving a blank is not.
3. The matrix is checked into `openspec/COVERAGE-MATRIX.md` (not in `.planning/`)
   so it is adjacent to the specs it cross-references.

### Module → Capability Pre-Assignment

The following is a high-confidence first-pass assignment based on the module tree.
Use this as the starting point for the matrix; the planner should verify/correct
during task execution.

**Sub-package to capability mapping (ASSUMED — needs code-reading verification):**

| Module Pattern | Primary Capability | Rationale |
|---------------|-------------------|-----------|
| `whilly/cli/run.py`, `whilly/cli/plan.py` | `orchestration-loop` | Main run loop entry points |
| `whilly/task_manager.py`, `whilly/core/state_machine.py` | `task-model-fsm` | FSM implementation |
| `whilly/orchestrator.py` | `batch-planning` | `plan_batches` logic |
| `whilly/tmux_runner.py`, `whilly/agent_runner.py`, `whilly/core/agent_runner.py`, `whilly/adapters/runner/` | `agent-dispatch` | Runner selection logic |
| `whilly/worktree_runner.py`, `whilly/workspaces.py` | `worktree-isolation` | Worktree lifecycle |
| `whilly/adapters/runner/result_parser.py` | `result-collection` | AgentResult parsing |
| `whilly/prd_generator.py`, `whilly/adapters/runner/claude_cli.py` | `prd-generation` | Non-interactive PRD |
| `whilly/prd_wizard.py`, `whilly/prd_launcher.py` | `prd-wizard` | Interactive PRD |
| `whilly/decomposer.py`, `whilly/core/prompts.py` | `decomposition` | Task splitting |
| `whilly/decision_gate.py`, `whilly/triz_analyzer.py`, `whilly/core/triz.py` | `decision-gate` | Decision Gate + TRIZ |
| `whilly/jira_board.py`, `whilly/jira_work.py`, `whilly/sources/jira.py`, `whilly/cli/jira.py`, `whilly/cli/jira_tui.py` | `jira-integration` | Jira read/work |
| `whilly/jira_watch.py`, `whilly/cli/jira_watch_loop.py` | `jira-watcher-daemon` | Watch daemon |
| `whilly/notifications.py`, `whilly/slack_task_notify.py`, `whilly/adapters/notifications/` | `notifications` | Notification dispatch |
| `whilly/sinks/gitlab_mr.py`, `whilly/cli/gitlab.py` | `gitlab-integration` | GitLab surface |
| `whilly/github_pr.py`, `whilly/github_projects.py`, `whilly/github_converter.py`, `whilly/github_interactive.py`, `whilly/gh_utils.py`, `whilly/sinks/github_pr.py`, `whilly/sources/github_*`, `whilly/ci/github.py`, `whilly/forge/`, `whilly/hierarchy/`, `whilly/workflow/github.py` | `github-integration` | GitHub PR/projects |
| `whilly/mcp/` | `mcp-integration` | MCP server/client |
| `whilly/dashboard.py`, `whilly/cli/dashboard.py`, `whilly/api/dashboard.py` | `dashboard-tui` | Rich TUI |
| `whilly/web_status.py`, `whilly/api/` (most routes) | `web-status-ui` | Web API/status |
| `whilly/reporter.py` | `reporting` | Iteration/end-of-run reports |
| `whilly/log_viewer.py`, `whilly/operator_views.py`, `whilly/cli/tui.py` | `operator-views-logs` | Log viewer |
| `whilly/cli/__main__.py`, `whilly/__main__.py`, `whilly/cli/run.py` | `cli-surface` | CLI flags, exit codes |
| `whilly/config.py`, `whilly/config_sections.py`, `whilly/project_config/` | `configuration` | WhillyConfig.from_env() |
| `whilly/api/auth_routes.py`, `whilly/api/auth_tokens.py`, `whilly/api/sessions.py`, `whilly/api/oidc_header_auth.py`, `whilly/api/webauthn_routes.py`, `whilly/api/totp_routes.py`, `whilly/api/second_factor.py`, `whilly/api/passwords.py`, `whilly/api/must_change_gate.py`, `whilly/security/` | `auth-security` | Auth hardening |
| `whilly/scheduler/` | `scheduling` | Scheduler |
| `whilly/state_store.py`, `whilly/pause_control.py`, `whilly/history.py` | `state-persistence` | Resume contract |
| `whilly/update.py`, `whilly/doctor.py`, `whilly/rollback/`, `whilly/repair/`, `whilly/cli/update.py`, `whilly/cli/rollback.py` | `self-update-doctor` | Update/doctor/repair |
| `whilly/resource_monitor.py`, `whilly/cli/smoke.py` | `budget-resource-guards` | Budget/resource monitoring |
| `whilly/recovery.py`, `whilly/self_healing.py` | `recovery-self-healing` | Deadlock/stall/retry |
| `whilly/quality/`, `whilly/compliance/`, `whilly/audit/`, `whilly/qa_release/` | `quality-compliance-audit` | Quality/compliance |
| `whilly/verifier.py`, `whilly/pipeline/`, `whilly/ci/verification.py`, `whilly/pipeline/human_review.py` | `verification-gates` | Verifier gates |
| `whilly/adapters/db/` | `state-persistence` | Database state layer |
| `whilly/adapters/transport/` | `web-status-ui` | HTTP transport for web API |
| `whilly/adapters/confluence/` | `github-integration` | Documentation publishing (or `UNMAPPED` if out of scope) |
| `whilly/classifier/` | `prd-generation` | Task/epic classification (may be `decision-gate`) |
| `whilly/worker/`, `whilly/cli/worker*.py` | `agent-dispatch` | Worker runtime |
| `whilly/llm_ops.py`, `whilly/llm_otel.py` | `orchestration-loop` | LLM operation plumbing (or `configuration`) |
| `whilly/external_integrations.py` | `configuration` | Integration config surface |
| `whilly/feedback.py`, `whilly/cli/feedback.py` | `cli-surface` | Feedback CLI command |
| `whilly/secrets.py` | `auth-security` | Secrets management |
| `whilly/workflow/`, `whilly/sources/`, `whilly/sinks/post_complete_pr_hook.py` | `github-integration` | Workflow engine |
| `whilly/project_board.py`, `whilly/forge/intake.py` | `github-integration` | Project board |
| `whilly/adapters/filesystem/` | `plan-json-contract` | Plan file I/O |
| `whilly/core/models.py`, `whilly/core/task_id.py`, `whilly/core/governance.py`, `whilly/core/gates.py` | `orchestration-loop` | Core domain models |
| `whilly/core/notifications.py`, `whilly/core/scheduler.py` | `notifications` | Core notification/scheduler |
| `whilly/core/prompts.py` | `prd-generation` | Prompt building |

**Note:** `whilly/adapters/db/` has 34 files (adapters/db/pool.py,
adapters/db/repository.py + 32 more). These need individual module-level review;
many likely map to `state-persistence` or `web-status-ui` (web API data layer).

---

## Project Context Document (BASE-04)

### Where to Put It

[VERIFIED: openspec config.yaml `context:` field — read by the installed init template]
[CITED: openspec/config.yaml — currently empty context field]

The `spec-driven` schema supports two locations:

1. **`openspec/config.yaml` `context:` field** — inline YAML multiline string.
   Shown to the AI when creating artifacts. Suitable for concise context (< 1 page).
2. **`openspec/project.md`** — a standalone file referenced by convention in
   OpenSpec projects. The REQUIREMENTS.md BASE-04 explicitly names `openspec/project.md`.

**Recommendation:** Use `openspec/project.md` as the primary document, AND
populate the `context:` field in `config.yaml` with a 3-5 line summary pointing
to `project.md`. This gives the AI context inline (via config) while the full
glossary is in an auditable file.

### What `openspec/project.md` Should Contain

[CITED: CLAUDE.md, AGENTS.md, REQUIREMENTS.md — existing project conventions]
[ASSUMED — specific content selection for project.md]

```markdown
# Whilly Project Context for OpenSpec

## Tech Stack
- Language: Python 3.10+ (targets 3.10/3.11/3.12 via CI matrix)
- Entry point: `whilly.cli:main` (console script `whilly`)
- Key library: Rich (TUI), FastAPI (web API), SQLAlchemy (DB), pytest, ruff
- Claude CLI integration: shelled out via `CLAUDE_BIN` / `claude` on PATH
- Test runner: pytest (async via pytest-asyncio)
- Lint/format: ruff (line length 120, target py310)

## Conventions
- Config: `WhillyConfig.from_env()` in `whilly/config.py`, prefix `WHILLY_`
- Task status: pending | in_progress | done | failed | skipped
- Completion signal: `<promise>COMPLETE</promise>` in agent output
- Exit codes: 0=ok, 1=some failed, 2=budget exceeded, 3=timeout
- Plan format: JSON with `{project, prd_file, tasks: [...]}` envelope
- Task fields: id, status, dependencies, key_files, priority, description,
  acceptance_criteria, test_steps

## Domain Glossary
- **capability**: A named subsystem-level behavior cluster (maps to one spec.md)
- **plan**: A JSON file describing a set of tasks for one execution run
- **task**: A unit of agent work with a status, acceptance criteria, and test steps
- **agent**: A Claude CLI process dispatched per task
- **workspace**: Optional git worktree isolating a plan's execution
- **worktree**: Per-task git worktree for parallel isolation
- **tmux session**: Named tmux session (`whilly-{task_id}`) hosting one agent
- **PRD**: Product Requirements Document — input to task generation
- **Decision Gate**: The pre-execution filter that refuses nonsense tasks via TRIZ
- **TRIZ**: Inventive principles used by the Decision Gate to identify contradictions
- **StateStore**: Persists iteration/cost/task status/tmux sessions for `--resume`
- **opsx**: The `openspec` change proposal workflow (propose → apply → archive)
- **delta spec**: A spec fragment under `openspec/changes/<name>/specs/` that
  describes additions/modifications to a main capability spec
- **coverage matrix**: The `openspec/COVERAGE-MATRIX.md` mapping every whilly/
  module to exactly one capability slug

## Normative Language Convention
Capability specs use RFC 2119 normative language:
- **SHALL / MUST**: behavior the system unconditionally guarantees
- Avoid: should, may, might, can (these are not testable)

## Spec Location Pattern
openspec/specs/<capability-slug>/spec.md
```

---

## Authoring Conventions Document (BASE-03)

### Where to Put It

[ASSUMED — OpenSpec provides no prescribed location for authoring conventions;
this is a project artifact]

**Recommendation: `openspec/AUTHORING.md`**

This keeps the conventions adjacent to the specs (in `openspec/`) but separate
from the capability specs themselves. Reference it from `openspec/project.md` and
from `openspec/TAXONOMY.md`.

### Required Content for AUTHORING.md

Based on verification of OpenSpec 1.4.1 validation rules:

```markdown
# Whilly Spec Authoring Conventions

## Spec File Location
openspec/specs/<capability-slug>/spec.md

Slug = directory name = spec ID. Use kebab-case.

## Required Sections (both MUST be present)

### ## Purpose
≥ 50 characters. Plain prose describing what the capability covers.

### ## Requirements
One or more ### Requirement: blocks.

## Requirement Block

### Requirement: <human-readable name>
<First line: normative statement containing SHALL or MUST.>

#### Scenario: <scenario name>
- **WHEN** <condition or trigger>
- **THEN** <expected outcome>
- **AND** <additional step> (optional)

## Normative Keyword Rules
- Body line (immediately after ### Requirement:) MUST contain SHALL or MUST.
- SHALL = unconditional guarantee. MUST = same strength as SHALL.
- Avoid: should, may, might, might not.

## Scenario Format Rules
- Scenario headers MUST use exactly #### (4 hashtags).
- Using ### or bullet lists means the scenario is NOT parsed.
- Every requirement MUST have at least one scenario.
- WHEN/THEN are required. AND is optional.

## Strict Validation Checklist
Run: openspec validate <slug> --strict --json

Fails if any WARNING or ERROR:
- ERROR: missing ## Purpose or ## Requirements section
- ERROR: 0 requirements
- ERROR: requirement body has no SHALL or MUST
- ERROR: requirement has 0 scenarios
- ERROR: delta header (## ADDED/MODIFIED/...) in a main spec
- WARNING→failure: ## Purpose content < 50 characters
- WARNING→failure: requirement body > 500 characters (split it)

## Anti-Patterns
- NEVER use ## ADDED Requirements in openspec/specs/*/spec.md (delta headers
  are only for openspec/changes/*/specs/*/spec.md)
- NEVER write SHALL/MUST in the ### Requirement: header only — write it in the body
- NEVER use a bullet list for scenarios — use #### Scenario: header
- NEVER leave a requirement with no scenario
- NEVER write descriptive prose ("this module does X") — write normative contracts
  ("the system SHALL do X when Y")
```

---

## Reference Exemplar Recommendation (Success Criterion 5)

### Recommended Capability: `task-model-fsm`

[CITED: REQUIREMENTS.md ORCH-02, CLAUDE.md "Architecture big picture" section]
[VERIFIED: live probe test — multi-requirement FSM spec passes `openspec validate --strict`]

**Why `task-model-fsm` is the best exemplar:**

1. **Self-contained**: The FSM is defined entirely in `whilly/task_manager.py` and
   `whilly/core/state_machine.py`. Reading two files is sufficient to write the spec.
2. **Clear contract**: 5 states, explicit transition rules, stale-reset-on-startup
   behavior. These are testable, normative, and unambiguous.
3. **Load-bearing**: Every other orchestration capability (ORCH-01, ORCH-03..07)
   references the task FSM. Getting this spec right first validates the format for
   the most-referenced concept in the milestone.
4. **Boundary is clear**: Does NOT include batch-planning, agent dispatch, or PRD.
   This keeps the exemplar small enough to write in one pass.
5. **Already verified**: A representative `task-model-fsm` spec was written and
   validated during research (3 requirements, 4 scenarios, all pass `--strict`).

**Alternative: `plan-json-contract`** — also self-contained, but requires reading
`task_manager.py` carefully for the round-trip tolerance behavior. Slightly more
subtle than `task-model-fsm`. Use as the second spec in Phase 22.

### Exemplar Spec Structure (Phase 21 deliverable)

The Phase 21 exemplar spec for `task-model-fsm` should include:

- `## Purpose`: 2-3 sentences covering the FSM's scope
- Requirement: Legal status values (SHALL restrict to 5)
- Requirement: Startup stale reset (in_progress → pending on startup)
- Requirement: Terminal state immutability (done/failed/skipped not re-run)
- Requirement: Status transitions (legal transition table)
- ≥ 1 scenario per requirement (verified format passing `--strict`)

---

## Common Pitfalls

### Pitfall 1: Delta Header Contamination in Main Specs

**What goes wrong:** Writing `## ADDED Requirements` instead of `## Requirements`
in a main spec file causes `"Main spec contains delta header"` ERROR and the
`## Requirements` section is truncated at that point.
**Why it happens:** Authors familiar with writing change/delta specs for opsx
workflows accidentally copy the delta format into the main spec.
**How to avoid:** Main specs only ever have `## Purpose` and `## Requirements`.
`## ADDED Requirements` belongs exclusively in `openspec/changes/*/specs/*/spec.md`.
**Warning signs:** `openspec validate --strict` reports "delta header" error.

### Pitfall 2: Scenario as Bullet List Instead of `#### Scenario:`

**What goes wrong:** Writing `- **WHEN** ... / **THEN** ...` without an
`#### Scenario:` header above it means zero scenarios are parsed. The validator
then reports ERROR: "Requirement must have at least one scenario." This is called
out as a "silent fail" in the schema instruction: using bullets won't add a
scenario but also won't warn until the whole spec is validated.
**Why it happens:** Copy-paste from requirements documents or design docs that
use bullet-list format.
**How to avoid:** Always precede WHEN/THEN bullets with `#### Scenario: <name>`.
**Warning signs:** Spec looks right in a text editor but `openspec validate`
reports 0 scenarios for a requirement.

### Pitfall 3: Descriptive vs Normative Language

**What goes wrong:** Writing "The dashboard shows task status" instead of "The
system SHALL display task status in the dashboard." The former fails schema
validation (no SHALL/MUST). The latter passes.
**Why it happens:** Natural language habit; reverse-speccing from code tends to
produce descriptive sentences.
**How to avoid:** Every requirement body line starts with "The system SHALL..." or
"The `<component>` MUST...". Never use "is", "does", "provides" as the main verb
without a SHALL/MUST.
**Warning signs:** `"Requirement must contain SHALL or MUST keyword"` ERROR.

### Pitfall 4: Purpose Section Too Short

**What goes wrong:** A one-sentence Purpose under 50 characters causes a WARNING,
which in `--strict` mode causes the spec to fail validation.
**Why it happens:** Authors write a short purpose thinking it's enough.
**How to avoid:** Purpose MUST be ≥ 50 characters. Two sentences is safe.
**Warning signs:** `"Purpose section is too brief (less than 50 characters)"` WARNING
becomes validation failure under `--strict`.

### Pitfall 5: Requirement Text > 500 Characters (INFO → WARNING Under Strict)

**What goes wrong:** A very long requirement body (> 500 chars) generates a
WARNING (INFO level in normal mode, WARNING in strict — from `constants.js`
`REQUIREMENT_TOO_LONG` mapped to `INFO` in `applySpecRules`). Under strict mode
this causes failure.
**CORRECTION from source:** The `applySpecRules` code pushes this as `INFO`, not
`WARNING`. The `createReport` strict check is `errors === 0 && warnings === 0`.
INFO items do not count as warnings. This pitfall is LOWER risk than initially
thought — but keeping requirements concise is still good practice.
[VERIFIED: validator.js line `level: 'INFO'` for `REQUIREMENT_TOO_LONG`]

### Pitfall 6: Double-Counting Modules in the Coverage Matrix

**What goes wrong:** A module is listed under two capability slugs, creating a
"double-mapped" row that violates the BASE-02 one-to-one constraint.
**Why it happens:** Large modules (e.g., `whilly/adapters/db/pool.py`) are used
by multiple capabilities. The temptation is to list it under both.
**How to avoid:** Assign each module to the capability it PRIMARILY serves.
Cross-cutting infrastructure modules (db pool, HTTP client) map to the
capability that consumes them most directly or to a platform capability.
If truly ambiguous, assign to `configuration` or `state-persistence` and note
it in the Notes column.

### Pitfall 7: Treating `__init__.py` as Non-Modules

**What goes wrong:** The planner excludes `__init__.py` files from the coverage
matrix, creating silent unmapped rows.
**Why it happens:** `__init__.py` files look like boilerplate.
**How to avoid:** Include all `.py` files in the matrix. Mark `__init__.py`
files with `package initializer` in the Notes column. They count toward the
total and must be explicitly mapped.

---

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| Spec validation | Custom Python validator | `openspec validate --strict` | Already installed, tested, correct parser |
| Spec format discovery | Reading README.md/docs | Source: `dist/core/validation/validator.js`, `dist/core/parsers/markdown-parser.js` | Ground truth is the installed source |
| Coverage matrix tool | Custom Python counter | Markdown table + `find whilly/ -name "*.py"` | Auditable, diff-friendly, no extra tooling |
| Taxonomy doc format | Novel XML/YAML schema | `openspec/TAXONOMY.md` plain Markdown table | Human-readable, diff-friendly, no tool dependency |
| Project context format | JSON schema for context | `openspec/project.md` plain Markdown | OpenSpec reads it as prose context, not structured |

---

## Code Examples

### Passing Minimal Spec (verified `openspec validate --strict`)

```markdown
## Purpose

The <capability> subsystem <what it does and for whom>.
This capability covers <scope boundary>.

## Requirements

### Requirement: <Primary guarantee name>
The system SHALL <normative statement here — first body line>.

#### Scenario: <Descriptive name>
- **WHEN** <condition or trigger>
- **THEN** <expected system behavior>
```

[VERIFIED: live probe test on OpenSpec 1.4.1 — `shall-in-body` spec, exit 0]

### Multi-Scenario, Multi-Requirement Spec (verified)

```markdown
## Purpose

The task-model-fsm capability defines the legal state machine for Whilly task
status. A task moves through a strict set of states: pending, in_progress, done,
failed, and skipped. This capability governs which transitions are legal, how
stale in-progress tasks are reset on startup, and what each terminal state means.

## Requirements

### Requirement: Legal status values
The system SHALL restrict task status to exactly five values: pending, in_progress,
done, failed, and skipped.

#### Scenario: Invalid status rejected
- **WHEN** a task JSON contains a status value outside the five legal values
- **THEN** the TaskManager SHALL reject the plan with a schema validation error
  before beginning any execution

### Requirement: Startup stale reset
The system SHALL reset any task found in in_progress status at startup to pending
before dispatching any agents.

#### Scenario: Stale in-progress reset
- **WHEN** the orchestrator starts and loads a plan with tasks in in_progress status
- **THEN** those tasks SHALL be set back to pending
- **AND** no agent SHALL be dispatched for them until they are re-scheduled normally

### Requirement: Terminal state immutability
The system SHALL NOT transition a task out of done, failed, or skipped status
once it has reached that terminal state.

#### Scenario: Done task not re-run
- **WHEN** the plan loop re-evaluates ready tasks
- **THEN** tasks with status done SHALL be excluded from the candidate set

#### Scenario: Failed task not auto-retried indefinitely
- **WHEN** a task reaches the failed terminal state
- **THEN** the orchestrator SHALL NOT retry that task in subsequent iterations
```

[VERIFIED: live probe test on OpenSpec 1.4.1 — `task-model-fsm` spec, exit 0, 0 issues]

### Running Validation

```bash
# Validate a single spec
openspec validate task-model-fsm --strict

# Validate all specs
openspec validate --specs --strict

# JSON output for scripting
openspec validate --specs --strict --json
```

### Coverage Matrix Shell Command (to enumerate all modules)

```bash
find whilly/ -name "*.py" \
  -not -path "*/__pycache__/*" \
  | sort
```

Produces 244 lines. Each becomes one row in `openspec/COVERAGE-MATRIX.md`.

---

## Environment Availability

| Dependency | Required By | Available | Version | Fallback |
|------------|------------|-----------|---------|----------|
| `openspec` CLI | Spec validation | YES | 1.4.1 | None — required |
| Node.js (via nvm) | openspec runtime | YES | v20.19.6 | None — required |
| Python 3.10+ | Module enumeration | YES | system Python | None |
| `find` / shell | Coverage matrix generation | YES | system | None |
| `pytest` | Existing test suite | YES | installed | None |

[VERIFIED: `openspec --version` → 1.4.1; `node --version` → v20.19.6]

**No missing dependencies.**

**Important:** `openspec` lives in the nvm-managed node bin path:
`~/.reflex/.nvm/versions/node/v20.19.6/bin/openspec`. If a shell session does not
source nvm, `openspec` may not be on `PATH`. Planner tasks should use the full
path or source nvm before running validation.

---

## Validation Architecture

`nyquist_validation: true` in `.planning/config.json` — include this section.

This phase is a documentation/scaffolding phase. There is no application code to
unit-test. Validation is done by the tool itself.

### Test Framework

| Property | Value |
|----------|-------|
| Framework | `openspec validate --strict` (CLI tool, not pytest) |
| Config file | `openspec/config.yaml` (schema: spec-driven) |
| Quick run command | `openspec validate task-model-fsm --strict` |
| Full suite command | `openspec validate --specs --strict --json` |

### Phase Requirements → Test Map

| Req ID | Behavior | Test Type | Automated Command | File Exists? |
|--------|----------|-----------|-------------------|-------------|
| BASE-01 | Taxonomy index at `openspec/TAXONOMY.md` | manual | `ls openspec/TAXONOMY.md && wc -l openspec/TAXONOMY.md` | No — Wave 0 |
| BASE-01 | 32 capability directories exist | smoke | `ls openspec/specs/ \| wc -l` | No — Wave 0 |
| BASE-02 | Coverage matrix has 244 rows | smoke | `grep -c "whilly/" openspec/COVERAGE-MATRIX.md` | No — Wave 0 |
| BASE-02 | Zero UNMAPPED rows remain | smoke | `grep -c "UNMAPPED" openspec/COVERAGE-MATRIX.md` | No — Wave 0 |
| BASE-03 | Authoring conventions doc exists | manual | `ls openspec/AUTHORING.md` | No — Wave 0 |
| BASE-04 | project.md exists with stack/glossary | manual | `ls openspec/project.md` | No — Wave 0 |
| SC-5 | Reference exemplar passes strict validation | automated | `openspec validate task-model-fsm --strict --json` | No — Wave 0 |

### Wave 0 Gaps

- [ ] `openspec/specs/task-model-fsm/spec.md` — reference exemplar (covers SC-5)
- [ ] `openspec/TAXONOMY.md` — capability index (covers BASE-01)
- [ ] `openspec/COVERAGE-MATRIX.md` — module mapping scaffold (covers BASE-02)
- [ ] `openspec/AUTHORING.md` — conventions document (covers BASE-03)
- [ ] `openspec/project.md` — project context (covers BASE-04)
- [ ] Stub directories for all 32 capability slugs under `openspec/specs/`

---

## Security Domain

> `security_enforcement` not explicitly set to `false` in config.json — include.

### Applicable ASVS Categories

| ASVS Category | Applies | Standard Control |
|---------------|---------|-----------------|
| V2 Authentication | No | Phase 21 produces only documentation files |
| V3 Session Management | No | No runtime code added |
| V4 Access Control | No | No runtime code added |
| V5 Input Validation | Partial | Coverage matrix enumerates modules; spec content is not user-supplied at runtime |
| V6 Cryptography | No | No crypto introduced |

**Security note for Phase 21:** The only security-adjacent work is ensuring
`openspec/project.md` and `openspec/COVERAGE-MATRIX.md` do not accidentally
include secrets, tokens, or internal URLs. The project conventions in AGENTS.md
and CLAUDE.md explicitly prohibit committing real secrets. No additional
controls needed for this documentation phase.

---

## State of the Art

| Old Approach | Current Approach | When Changed | Impact |
|--------------|------------------|--------------|--------|
| One spec per module (242 files) | ~30 subsystem capabilities + coverage matrix | 2026-06-13 decision gate | Maintainable, normative specs vs unwieldy descriptive dump |
| Descriptive spec snapshot | Normative + testable (SHALL/MUST + scenarios) | 2026-06-13 decision gate | Machine-checkable, auditable contracts |
| Ad-hoc spec format | OpenSpec 1.4.1 spec-driven schema | 2026-06-13 (OpenSpec initialized) | Validated format, `opsx` forward-delta workflow |

---

## Assumptions Log

| # | Claim | Section | Risk if Wrong |
|---|-------|---------|---------------|
| A1 | The "242 modules" count in REQUIREMENTS.md is a prior manual count excluding `__init__` files; the actual `find` count is 244 | Coverage Matrix | If wrong, the matrix row count target is 244 not 242 — minor, easily corrected |
| A2 | `openspec/COVERAGE-MATRIX.md` is the right location for the matrix (not `.planning/`) | Coverage Matrix | If wrong, the file may not be found by Phase 28 auditors; easily moved |
| A3 | `openspec/AUTHORING.md` is the right name/location for conventions | Authoring Conventions | If wrong, rename the file; no functional impact |
| A4 | `openspec/TAXONOMY.md` is the right name/location for the capability index | Taxonomy | If wrong, rename; no functional impact |
| A5 | Module-to-capability pre-assignments for `adapters/db/`, `classifier/`, `workflow/`, `adapters/confluence/`, `llm_ops.py` are approximate | Coverage Matrix pre-assignment | If wrong, specific rows in the matrix are miscategorized; Phase 28 COV-01 catches this |
| A6 | `whilly/compliance/__init__.py` is the only file in `whilly/compliance/` | Coverage Matrix | If wrong, additional compliance module(s) exist and need mapping |

---

## Open Questions

1. **"242" vs "244" module count**
   - What we know: `find whilly/ -name "*.py" | grep -v __pycache__ | wc -l` = 244
   - What's unclear: REQUIREMENTS.md says "242" — which 2 files were excluded?
   - Recommendation: Phase 21 plan tasks should count programmatically and use the
     actual count; update REQUIREMENTS.md if needed. Zero silent gaps is the goal.

2. **`openspec/project.md` vs `openspec/config.yaml context:`**
   - What we know: Both locations are supported; `config.yaml context:` is inline
   - What's unclear: Does OpenSpec AI tooling read `project.md` automatically or
     only `config.yaml context:`?
   - Recommendation: Populate both. Use `project.md` for the full document; add
     a 3-line summary in `config.yaml context:` pointing at `project.md`.

3. **Confluence adapter ownership**
   - `whilly/adapters/confluence/` has 2 modules but no corresponding integration
     in the v1.3 capability taxonomy
   - Recommendation: Map to `github-integration` (documentation publishing) or
     create a note in the matrix; do NOT create a new capability for it without
     raising with the operator first.

---

## Sources

### Primary (HIGH confidence)

- OpenSpec 1.4.1 installed source:
  `~/.reflex/.nvm/versions/node/v20.19.6/lib/node_modules/@fission-ai/openspec/dist/`
  Topics checked: validator.js, markdown-parser.js, spec-structure.js,
  base.schema.js, spec.schema.js, constants.js, schema.yaml
- Live probe tests: 12 spec permutations created and validated in
  `/tmp/test-openspec-probe/` — all results verified against `openspec validate --strict --json`
- `.planning/REQUIREMENTS.md` — locked decisions, requirement IDs, capability list
- `.planning/ROADMAP.md` — phase descriptions, success criteria
- `CLAUDE.md`, `AGENTS.md` — project conventions, module architecture

### Secondary (MEDIUM confidence)

- `find whilly/ -name "*.py"` enumeration — actual module count and paths
- `openspec --help`, `openspec validate --help`, `openspec templates` — CLI surface

### Tertiary (LOW confidence)

- None — all claims in this research are HIGH or MEDIUM confidence, or tagged [ASSUMED]

---

## Metadata

**Confidence breakdown:**

- OpenSpec spec format: HIGH — read from installed source + live probe tests
- Validation rules (`--strict` behavior): HIGH — read from validator.js source +
  confirmed by probe tests
- Capability taxonomy: HIGH — copied from locked decisions in REQUIREMENTS.md
- Module → capability pre-assignment: MEDIUM/LOW — based on module naming patterns,
  not code reading; Phase 21 tasks must verify
- Coverage matrix format: MEDIUM — recommended, not mandated by OpenSpec
- `openspec/project.md` / `AUTHORING.md` / `TAXONOMY.md` locations: MEDIUM —
  recommended convention, OpenSpec has no prescribed location

**Research date:** 2026-06-13
**Valid until:** 2026-12-13 (stable CLI; only changes if OpenSpec upgrades)
