---
phase: 17-jira-work-classification-and-code-readiness-routing
plan: "03"
subsystem: jira
tags: [jira, gitlab, repo-targets]
requirements-completed: [JIRA-04]
completed: 2026-05-11
---

# Phase 17 Plan 03: Link Hint Reuse Summary

Added `release_context_repo_targets()` so Jira refresh/watch flows can reuse
`whilly.qa_release.collector` GitLab/GitHub hints and turn them into Whilly repo targets.

## Verification

- Unit coverage confirms GitLab hints produce plan-compatible `repo_targets`.
