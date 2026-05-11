---
phase: 17-jira-work-classification-and-code-readiness-routing
status: passed
verified_at: 2026-05-11
requirements: [JIRA-01, JIRA-02, JIRA-03, JIRA-04, JIRA-05, JIRA-06, JIRA-07]
---

# Phase 17 Verification

## Result

status: passed

## Evidence

- JIRA-01/JIRA-02: `whilly.jira_work.classify_jira_work()` returns kind, urgency, confidence,
  signals, missing context, and recommended flow; tests cover feature and hotfix bug paths.
- JIRA-03/JIRA-06: `whilly jira poll` can reread issue fields, comments, changelog ids, linked
  issues, remote links, and repo hints once; Alembic 016 and `TaskRepository` provide durable Jira
  work session/event state; comment commands parse for classify, urgency, PRD, plan, run,
  continue, replan, and cancel.
- JIRA-04: `release_context_repo_targets()` reuses `whilly.qa_release` repo hints for GitLab/GitHub
  link-derived repo targets.
- JIRA-05: `probe_code_readiness()` detects common test commands and unit tests; `jira intake`
  blocks `action=run` on missing test evidence when a readiness path is provided.
- JIRA-07: Focused unit/static tests cover classifier, command parsing, hashes, repo hints,
  readiness verdicts, and the no-unit-tests run gate.

## Commands

- `python3 -m pytest -q tests/unit/test_jira_work.py tests/unit/test_jira_watch.py tests/unit/test_jira_cli.py tests/integration/test_alembic_016_jira_work_sessions.py tests/integration/test_alembic_full_chain.py::test_expected_chain_files_exist_on_disk --maxfail=1` - 33 passed, 2 warnings.
- `python3 -m ruff check whilly/jira_work.py whilly/jira_watch.py whilly/cli/jira.py whilly/adapters/db/repository.py whilly/adapters/db/migrations/versions/016_jira_work_sessions.py tests/unit/test_jira_work.py tests/unit/test_jira_watch.py tests/unit/test_jira_cli.py tests/integration/test_alembic_016_jira_work_sessions.py tests/integration/test_alembic_015_plan_verification_commands.py tests/integration/test_alembic_full_chain.py` - All checks passed.
- `git diff --check` - passed.
