---
title: Project Config
layout: default
nav_order: 4
description: "Universal configuration for domain-adaptive Whilly orchestration pipelines."
permalink: /Project-Config
---

# Universal Project Configuration

Whilly can generate a domain-adaptive v4 plan from a project config. Use this
when one orchestrator installation must run different workflows for Python
backend work, ETL QA, GraphQL API tests, documentation, or a custom pipeline.

This is the first step toward Whilly's target state: a configurable
project-aware orchestrator where each project type can define its own sources,
pipeline stages, gates, verification steps, runners, sinks, and
human-in-the-loop checkpoints.

Project config does not make agent output correct by itself. It scopes the
workflow, makes the expected stages explicit, and turns domain-specific work
into the same auditable Whilly task model used by the core queue.

## Commands

```bash
python3 -m whilly project-config validate examples/project-config-etl.toml
python3 -m whilly project-config plan examples/project-config-etl.toml --out out/etl-plan.json
```

`validate` loads the config, applies any built-in preset, validates repository
roles and dependencies, and checks the generated plan for cycles. `plan` writes
canonical Whilly JSON that can be imported or run by the normal v4 worker flow.

When a generated task reaches the worker, configured project-step metadata is
recorded as audit events (`pipeline.stage.started`, `pipeline.stage.succeeded`,
or `pipeline.stage.failed`) without adding new task statuses. Required
verification can be enforced for a run with repeatable `whilly run` flags:

```bash
python3 -m whilly run --plan "$PLAN_ID" \
  --verify-command "unit=python3 -m pytest -q tests/unit" \
  --optional-verify-command "lint=ruff check whilly tests"
```

Required verification failures mark the task `FAILED` with
`reason=verification_failed`; optional failures record `verification.warning`
and leave the normal terminal path intact.

## Project Types

- `python_backend`: decomposes a feature, implements code, generates tests,
  runs quality gates, and waits for review/release approval.
- `etl_pipeline`: QA/STLC release verification from Jira and linked artifacts; includes
  release context, test planning, autotest generation, STAGE deploy gate,
  functional/regression execution, audit, and release decision.
- `graphql_api`: collects API requirements, inspects schema/operations, creates
  contract/integration autotests, runs API tests, and gates human review.
- `documentation`: takes PRD/manual input, drafts or updates docs, runs
  consistency checks, and waits for review approval.
- `generic`: minimal intake, execute, verify flow.

Compatibility aliases remain accepted: `etl` maps to `etl_pipeline`, and
`feature_development` maps to `python_backend`.

These presets are intentionally conservative. They describe orchestration
stages and review points; they do not imply full sandbox isolation, automatic
production release, or mandatory CI/lint verification unless a project pipeline
explicitly configures those steps.

## Config Shape

Project configs are JSON or TOML. The main fields are:

- `project_type`: selects a built-in preset unless `pipeline` is provided.
- `task_sources`: Jira, GitHub, PRD, or manual references.
- `repositories`: code, tests, deployment, or docs locations. Pipeline steps
  bind to these by `repo_role`.
- `pipeline`: optional explicit steps with dependencies, commands, outputs, and
  human gates.
- `human_loop`: approval channel and required checkpoint steps.

For Jira Data Center environments that use company settings, run with:

```bash
WHILLY_COMPANY_SETTINGS_FILE=/path/to/company_settings.yml \
JIRA_AUTH_SCHEME=bearer \
JIRA_API_VERSION=2 \
python3 -m whilly qa-release collect PROJ-123 --out out/release-context.json
```

Keep `JIRA_VERIFY_SSL=true` by default. Use `JIRA_CA_FILE=/path/to/ca.pem` for
corporate CAs; `JIRA_VERIFY_SSL=false` is only a local diagnostic escape hatch.
