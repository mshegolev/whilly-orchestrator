# Business Requirements Document — Whilly Orchestrator

## Executive Summary

Whilly Orchestrator is a control plane for AI-assisted engineering workflows.

It takes structured engineering tasks from JSON plans, GitHub Issues, GitHub Projects, Jira, and PRD/Forge intake, validates them, stores them in Postgres, queues them for execution, assigns them to local or remote workers, passes controlled prompts to AI agent runners, tracks task state transitions, records audit events, exposes observability endpoints, and supports human-in-the-loop review at critical stages.

The project should not be described as a fully autonomous AI developer. Its current value is controlled acceleration: making AI-assisted software delivery more deterministic, observable, recoverable, auditable, and safe.

## Business Goal

Reduce the coordination cost and operational risk of using AI agents for engineering work.

Whilly should help teams safely delegate well-scoped engineering tasks to AI agents while preserving:

- human oversight;
- traceability;
- deterministic task state;
- auditability;
- security boundaries;
- operational visibility;
- recoverability after failures;
- integration with existing engineering systems.

## Business Problem

Raw agent execution creates risks:

- tasks are executed without consistent validation;
- agent actions are hard to audit;
- task state is scattered across issues, terminals, local files and PRs;
- failures are difficult to recover from;
- agents may work on vague or unsafe tasks;
- humans lack clear control points;
- there is no consistent execution model across project types;
- automation may overreach into release or production actions.

Whilly addresses this by acting as a controlled orchestration layer between task sources, AI agents, repositories, verification systems and human reviewers.

## Target Users

### Primary users

- Engineering teams using AI agents for coding tasks.
- Staff/principal engineers responsible for AI-assisted engineering architecture.
- Platform teams building internal developer automation.
- QA and release engineers responsible for verification workflows.

### Secondary users

- Product managers defining feature tasks or PRDs.
- Engineering managers monitoring task progress.
- Security reviewers validating agent execution boundaries.
- Developer productivity teams measuring AI-assisted delivery efficiency.

## Business Outcomes

Whilly should enable:

1. Faster execution of well-scoped engineering tasks.
2. Lower manual coordination cost across issue trackers and code workspaces.
3. More reliable AI-agent usage through validation gates and controlled prompts.
4. Better visibility into task lifecycle and worker execution.
5. Safer human-reviewed delivery instead of uncontrolled autonomy.
6. Reusable orchestration patterns across project types.
7. Clear evidence for compliance, debugging and operational review.

## Success Metrics

### Delivery metrics

- Percentage of imported tasks that pass validation gates.
- Percentage of claimed tasks that reach DONE.
- Percentage of failed tasks with actionable failure reason.
- Median time from task import to DONE.
- Median time from DONE to PR review or human handoff.

### Quality metrics

- Percentage of tasks with acceptance criteria.
- Percentage of tasks with test steps.
- Percentage of tasks with verification stage configured.
- Number of tasks incorrectly marked DONE.
- Number of tasks requiring human clarification after execution started.

### Safety metrics

- Number of blocked prompt-injection attempts.
- Number of blocked dangerous command patterns.
- Number of tasks skipped by strict gates.
- Number of worker failures recovered through release/reclaim.
- Number of tasks executed without audit events.

## Scope

### In scope

- AI-assisted coding task orchestration.
- Task intake from structured sources.
- Task validation and decision gates.
- Postgres-backed state management.
- Worker claiming and execution lifecycle.
- Agent runner abstraction.
- Human review and handoff checkpoints.
- Observability and audit events.
- Configurable project-aware pipelines.

### Out of scope for current phase

- Fully autonomous production release.
- Automatic merge without human approval.
- Full multi-repository transactional execution.
- General-purpose autonomous project planning without structured input.
- Unrestricted shell or infrastructure operations by agents.
- Semantic long-term memory unless explicitly scoped later.
- Guaranteed correctness of AI-generated code.
