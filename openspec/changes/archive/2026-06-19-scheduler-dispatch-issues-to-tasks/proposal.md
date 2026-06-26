# Proposal: Scheduler dispatches discovered Jira issues as claimable tasks

## Why

The `scheduling` capability promises continuous JQL-driven intake of Jira
issues, but the intake dead-ends: `whilly/cli/scheduler.py::on_issues_found` is
a logging stub. It prints up to three issue keys and returns. No `Plan` or
`Task` row is written, so the `created_plans` column on `scheduler_poll_cycles`
(migration 017) is never populated and no worker can ever claim a
scheduler-discovered issue.

The practical consequence: a rule like `assignee = currentUser() AND status =
'To Do'` polls Jira correctly and deduplicates correctly, then throws the
results away. The end-to-end "my assigned Jira tasks get executed by the worker
cluster" flow is broken at exactly this seam — every other link (JQL execution,
deduplication, plan import idempotency, claim/run loop, the opencode-devbox
remote-worker adapter) already works.

## What Changes

- **ADDED** `scheduling` → "Scheduler issue dispatch to claimable tasks": when a
  database URL is configured, each unique JQL-matched issue is converted into a
  `PENDING` v4 `Task` and persisted under one plan per rule (plan id = the
  rule's `custom_metadata.plan_id` or its `id`), idempotently via the existing
  `ON CONFLICT (id) DO NOTHING` import path. Tasks carry id `JIRA-<key>`, the
  issue's sanitized description / acceptance criteria / test steps, mapped
  priority, and an optional repo target resolved from rule metadata. With no
  database URL the system logs discovered issues without persisting (unchanged
  dev behavior).
- **MODIFIED** `scheduling` → "Poll cycle execution and recording": the
  issues-found callback MAY return the list of plan ids it wrote, and the cycle
  SHALL record them in `created_plans`.
- **MODIFIED** `scheduling` → "Issue deduplication": `compute_issue_hash`
  resolves a configured field from the issue's nested `fields` object when it is
  absent at the top level. Raw `execute_jql` results nest `summary` under
  `fields`, so without this the default `("key", "summary")` raised
  `DeduplicationError` and silently dropped *every* real issue — the latent bug
  the log-only stub hid. The DB-level `ON CONFLICT (id) DO NOTHING` remains the
  real idempotency guarantee; dedup is the upstream optimization.

New pure module `whilly/scheduler/intake.py` (issue → `Plan`/`Task` builder +
repo-target resolver, no I/O) reuses `whilly.sources.jira.issue_to_task_dict`
(ADF flattening + `sanitize_external_text`) and the canonical
`whilly.core.models`. Persistence reuses `whilly.cli.plan._async_import` so the
scheduler and `whilly plan import` share one insert + idempotency path. The
`SchedulerWorker` records a list returned by `on_issues_found` into
`cycle.created_plans` (backward-compatible: existing callbacks return `None`).

## Impact

- Specs: `scheduling` (1 requirement added, 1 modified).
- Code: `whilly/scheduler/intake.py` (new), `whilly/scheduler/worker.py`
  (record callback return into `created_plans`), `whilly/cli/scheduler.py`
  (`on_issues_found` persists when `WHILLY_DATABASE_URL` is set).
- Coverage matrix: add `whilly/scheduler/intake.py` → `scheduling`.
- No schema change — `created_plans` column already exists (migration 017).
- Backward compatible — no DB URL ⇒ prior log-only behavior.
