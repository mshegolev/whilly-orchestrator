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
  shell guards, audit events, metrics, SSE, web dashboard, PR feedback polling,
  repo-target metadata, project profiles, built-in profile vocabulary, project
  config plan generation, audit-event pipeline stage lifecycle, configured
  verification commands that block `DONE` on required failure, human-review
  approval/rejection/change-request controls in the web dashboard and TUI, and
  env-gated GitHub PR sink stages for project-config plans.
- **Partial:** profile-native verification command runtime wiring,
  non-PR configured sinks, multi-repo execution, sandbox/VM isolation,
  PR-review repair loops, semantic memory, and robust smart rollback.
- **Target:** profile-native runtime pipeline stages, configured sinks, bounded
  repair loops, CI polling, and governance policy.

Do not describe current Whilly as providing full autonomous multi-repo
execution, mandatory CI/lint verification unless verification commands are
configured, full sandbox or VM isolation, semantic long-term memory, reliable
git rollback, autonomous production release, or an automatic PR-review repair
loop. PR review feedback remains manual one-shot polling until bounded repair is
implemented.

Use the compliance report command to produce a current, auditable snapshot:

```bash
python3 -m whilly compliance report --format markdown --out out/compliance-report.md
```
