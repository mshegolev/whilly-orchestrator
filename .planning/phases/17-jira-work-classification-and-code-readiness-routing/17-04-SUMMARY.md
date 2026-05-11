---
phase: 17-jira-work-classification-and-code-readiness-routing
plan: "04"
subsystem: jira
tags: [readiness, tests, cli]
requirements-completed: [JIRA-05]
completed: 2026-05-11
---

# Phase 17 Plan 04: Code Readiness Summary

Added `probe_code_readiness()` plus `whilly jira readiness`. `whilly jira intake --action run`
can now inspect a local checkout through `--readiness-repo-path` and stop before worker execution
when unit tests or test commands are missing.

## Verification

- Unit tests cover ready Python repos, missing unit tests, and the intake run gate.
