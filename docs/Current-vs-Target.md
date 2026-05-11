---
title: Current vs Target
layout: default
nav_order: 5
description: "Honest alignment status between the current Whilly implementation and the target documentation pack."
permalink: /Current-vs-Target
---

# Current vs Target

The target documentation pack lives in [`docs/target/`](target/). It describes
Whilly as a configurable control plane for AI-assisted engineering workflows,
not as a fully autonomous developer.

Current Whilly is between Level 1 and Level 2 of the target roadmap:

- **Implemented:** deterministic task state, Postgres queueing, plan import,
  local and remote workers, GitHub/Jira/Forge intake, decision gates, prompt and
  shell guards, secret linting, runner env allowlists, audit events, metrics,
  SSE, web dashboard, PR feedback polling, repo-target metadata, project
  profiles, built-in profile vocabulary, project config plan generation,
  audit-event pipeline stage lifecycle, configured verification commands that
  block `DONE` on required failure, profile-native verification commands feed
  runtime verification, human-review approval/rejection/change-request controls
  in the web dashboard and TUI, operator-triggered rollback, explicit configured
  CI polling, bounded repair attempts, deterministic governance risk policy, and
  env-gated GitHub PR sink stages for project-config plans.
- **Partial or limited:** non-PR configured sinks, multi-repo execution,
  sandbox/VM isolation with improved guards but no full per-task VM/container
  isolation, and PR-review repair loops that are not continuous autonomous
  repair.
- **Explicitly deferred:** Semantic memory is explicitly deferred from current
  scope; deterministic events, task history, PR evidence, and verification logs
  remain authoritative.
- **Target:** profile-native runtime pipeline stage execution, broader configured
  sinks, continuous PR review feedback handling, semantic-memory retrieval, full
  per-task sandbox/VM isolation, multi-repo orchestration, and release-candidate
  automation with human approval.

Current scope wording: profile-native verification commands feed runtime verification; operator-triggered rollback; explicit configured CI polling; bounded repair attempts; deterministic governance risk policy.

Semantic memory is explicitly deferred from current scope; deterministic events, task history, PR evidence, and verification logs remain authoritative.

Do not describe current Whilly as providing full autonomous multi-repo
execution, mandatory CI/lint verification unless verification commands are
configured, full sandbox or VM isolation, autonomous rollback/recovery,
autonomous production release, default auto-merge, or a continuous PR-review
repair loop. No continuous polling, auto-merge, production recovery, or unbounded repair is claimed.

Use the compliance report command to produce a current, auditable snapshot:

```bash
python3 -m whilly compliance report --format markdown --out out/compliance-report.md
```
