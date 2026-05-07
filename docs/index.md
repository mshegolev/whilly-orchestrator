---
title: Home
layout: default
nav_order: 1
description: "Whilly Orchestrator — control plane for observable AI-assisted engineering workflows."
permalink: /
---

# Whilly Orchestrator
{: .fs-9 }

Control plane for safe, observable AI-assisted engineering workflows.
{: .fs-5 .fw-300 }

[Getting Started]({{ site.baseurl }}/Getting-Started){: .btn .btn-primary .fs-5 .mb-4 .mb-md-0 .mr-2 }
[View on GitHub](https://github.com/mshegolev/whilly-orchestrator){: .btn .fs-5 .mb-4 .mb-md-0 }

---

## What Whilly Does

Whilly turns structured engineering work into a deterministic, auditable task
execution pipeline. It accepts tasks from JSON plans, GitHub Issues, GitHub
Projects, Jira, and PRD/Forge intake, normalizes them into one task model, and
stores execution state in Postgres.

The orchestrator owns task selection and state transitions. Agents receive a
prepared prompt through a runner or handoff backend; they do not pick arbitrary
tasks or take over the whole project plan.

Whilly tracks dependencies, priorities, budgets, decision gates, worker claims,
events, errors, health, metrics, and human review points. It is built for
issue-driven coding work: bug fixes, features, refactoring, test generation,
and documentation updates.

## Boundaries

Whilly orchestrates agents; it does not guarantee that every agent output is
correct. The value is controlled acceleration: limiting the work scope,
validating inputs, managing the queue, recording state, making execution
observable, and keeping humans in control at critical points.

The current core should not be described as full autonomous multi-repo
execution, automatic PR-review feedback handling, mandatory CI/lint
verification unless verification commands are configured, full sandbox or VM
isolation, semantic long-term memory, reliable git rollback, or autonomous
production release.

## One-liner demo

```bash
pipx install whilly-orchestrator
whilly --config path            # where to drop your config
whilly --from-issue you/repo/42 --go
```

That fetches issue 42, generates a one-task plan, imports it into the
orchestration flow, runs a worker, and exits `0` only when the runner reports a
successful completion.

## Read next

| Page | When to read |
|---|---|
| **[Getting Started]({{ site.baseurl }}/Getting-Started)** | First time here — eight practical walkthroughs |
| **[Full Usage Reference]({{ site.baseurl }}/Whilly-Usage)** | Every CLI flag, env var, and config field |
| **[GitHub Integration Guide]({{ site.baseurl }}/GitHub-Integration-Guide)** | Setting up Projects v2 + board sync |
| **[Current vs Target]({{ site.baseurl }}/Current-vs-Target)** | Alignment status against the target documentation pack |
| **[Interfaces & Tasks]({{ site.baseurl }}/Whilly-Interfaces-and-Tasks)** | Module contracts + the JSON plan schema |
| **[Architecture Decisions]({{ site.baseurl }}/workshop/adr/)** | Why things are the way they are (if published) |

## Under the hood

```
Sources ──▶ Plan/task model ──▶ Postgres queue ──▶ Worker claim ──▶ Runner/backend
                         │              │                 │
                         ▼              ▼                 ▼
                 Decision gates    Audit events      Human review
                         │              │                 │
                         └──── Dashboard / SSE / metrics / health
```

Full module map lives in [`Whilly-Interfaces-and-Tasks`]({{ site.baseurl }}/Whilly-Interfaces-and-Tasks).

## Current status

- Focused pytest suites, Ruff formatting/linting, and import-boundary checks.
- Layered config — `whilly.toml` + OS keyring, migrates from legacy `.env` with one command.
- Target documentation imported under [`docs/target/`]({{ site.baseurl }}/target/)
  with current-vs-target status tracked separately.
- [Latest release](https://github.com/mshegolev/whilly-orchestrator/releases/latest) · [Open issues](https://github.com/mshegolev/whilly-orchestrator/issues) · [Changelog](https://github.com/mshegolev/whilly-orchestrator/commits/main)
