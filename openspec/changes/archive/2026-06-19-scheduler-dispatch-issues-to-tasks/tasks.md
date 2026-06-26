# Tasks

- [x] Author ADDED + MODIFIED delta for `scheduling`
- [x] Add pure `whilly/scheduler/intake.py` (issue→Plan/Task builder, repo-target
      resolver) with unit tests (no DB)
- [x] Record `on_issues_found` return value into `cycle.created_plans`
      (`whilly/scheduler/worker.py`) with a unit test
- [x] Wire `whilly/cli/scheduler.py::on_issues_found` to persist via
      `_async_import` when `WHILLY_DATABASE_URL` is set, else log-only
- [x] Fix `compute_issue_hash` to resolve nested Jira `fields` (default
      `("key","summary")` was dropping every raw `execute_jql` issue)
- [x] Add `whilly/scheduler/intake.py` row to `openspec/COVERAGE-MATRIX.md`
      (+ bump Counts 275→276)
- [x] `pytest -k "scheduler or intake"` green (58 passed)
- [x] `openspec validate scheduler-dispatch-issues-to-tasks --strict` passes
- [x] `make spec-check` green (276/276 modules, 33 specs)
- [ ] Archive: `openspec archive scheduler-dispatch-issues-to-tasks`
- [ ] Confirm `openspec validate --all --strict` still passes after archive
