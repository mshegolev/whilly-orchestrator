---
phase: 17-jira-work-classification-and-code-readiness-routing
plan: "01"
subsystem: jira
tags: [jira, classification, routing]
requirements-completed: [JIRA-01, JIRA-02]
completed: 2026-05-11
---

# Phase 17 Plan 01: Work Classification Summary

Added `whilly/jira_work.py` with `JiraWorkClassification` and classification rules for
`feature`, `bug`, `task`, and `devops`, with `normal`/`hotfix` urgency.

## Verification

- Focused Jira work and CLI tests passed.
- Ruff passed for the new classifier and touched CLI/tests.
