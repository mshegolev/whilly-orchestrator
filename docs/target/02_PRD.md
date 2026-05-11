# Product Requirements Document — Whilly Orchestrator

## Product Vision

Whilly Orchestrator should become a configurable project-aware orchestration layer for AI-assisted engineering work.

Each project should be able to define:

- project type;
- task sources;
- task validation gates;
- pipeline stages;
- AI runner backend;
- verification steps;
- code/test output locations;
- human approval checkpoints;
- result sinks;
- observability requirements.

## Product Principles

1. **Control over autonomy** — Whilly orchestrates AI agents; it does not give them unrestricted autonomy.
2. **Deterministic state, non-deterministic execution** — state, queueing and events remain deterministic even when AI execution is probabilistic.
3. **Human review by default for risky steps** — merge, release, destructive changes and ambiguous resolution require human review.
4. **Configuration over code changes** — project behavior should be controlled through declarative profiles.
5. **Fail closed where safety matters** — unsafe commands, missing approval and missing verification should block or escalate.
6. **Observable by design** — every meaningful lifecycle event should be auditable and measurable.

## Functional Requirements

### FR-1 — Project profile configuration

The system must support declarative project profiles.

Minimum profile fields:

```yaml
project:
  id: string
  name: string
  type: python_backend | graphql_api | etl_pipeline | documentation | generic
  workspace: string
  default_runner: claude_cli | opencode | handoff

sources:
  - type: json_plan | github_issues | github_projects | jira | forge
    config: object

pipeline:
  stages:
    - id: string
      type: intake | gate | plan_apply | execute | verify | pr | human_review | sync
      required: boolean
      config: object

verification:
  commands:
    - name: string
      command: string
      required: boolean

human_review:
  required_before_done: boolean
  required_before_pr: boolean
  required_before_merge: boolean

sinks:
  - type: github_pr | github_issue_comment | jira_comment | jsonl | dashboard
    config: object
```

### FR-2 — Built-in project profiles

Provide built-in profile templates:

- generic coding project;
- Python backend;
- GraphQL API;
- ETL/data pipeline;
- documentation project.

### FR-3 — Profile validation

Validation should detect:

- missing project id;
- unsupported project type;
- missing workspace;
- unknown runner;
- missing required pipeline stage ids;
- unsafe verification command patterns;
- invalid source/sink type;
- contradictory human review settings.

### FR-4 — Configurable pipeline stages

The worker/control plane must resolve stages from project profile.

Initial implementation may keep the current task state machine and add hooks around:

- pre-execution gates;
- post-execution verification;
- human review handoff;
- PR/sink creation.

### FR-5 — Domain-specific verification

#### Python backend

- unit tests;
- lint/type checks if configured;
- changed-file-aware tests when available.

#### GraphQL API

- schema diff check;
- generated API tests;
- resolver tests;
- backward compatibility checks.

#### ETL pipeline

- data quality validation;
- sample run;
- source/target schema validation;
- QA/STLC sign-off.

#### Documentation

- link check if configured;
- consistency check;
- human review.

### FR-6 — Human-in-the-loop checkpoint model

A task/result should express:

- completed automatically;
- failed automatically;
- blocked and needs human input;
- completed but needs human review;
- partially completed and needs follow-up.

### FR-7 — Result sink integration

Initial sinks:

- JSONL event mirror;
- dashboard/SSE;
- GitHub issue comment;
- Jira comment;
- GitHub PR creation if configured;
- handoff file output.

PR creation is optional/configured, not an implicit DONE transition.

### FR-8 — Validation mode for current project

An AI agent must be able to validate the current repository against this specification.

It should inspect:

- task state machine;
- task claim logic;
- worker loop;
- runner result parser;
- gate implementation;
- prompt guard;
- dangerous command blocking;
- observability endpoints;
- project profile support;
- verification hooks;
- PR sink integration;
- human-in-the-loop integration;
- documentation accuracy.

### FR-9 — Compatibility

Existing behavior must continue working:

- JSON plan import;
- `whilly plan apply`;
- `whilly run --plan`;
- local and remote workers;
- events and metrics.

## Non-Functional Requirements

### Reliability

- Task state transitions must remain transactional.
- Worker claim must remain protected against double claim.
- Stale claims must be releasable.
- Failed verification must not silently mark task complete.

### Security

- Dangerous command detection remains before runner execution.
- Prompt injection guard remains before prompt execution.
- Project profile commands are validated before use.
- Secrets must not be logged in prompts, events or metrics.
- Human approval is required before destructive or release actions.

### Observability

- Every configured stage emits start/success/failure/skipped events.
- Metrics include stage failures and verification failures.
- Dashboard exposes tasks needing human input.

### Maintainability

- Project profiles use typed schemas.
- Built-in profiles are small and explicit.
- New project types can be added without rewriting the worker loop.
