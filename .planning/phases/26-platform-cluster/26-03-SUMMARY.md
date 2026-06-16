---
phase: 26-platform-cluster
plan: 03
subsystem: scheduling
tags: [openspec, scheduling, scheduler, jql, webhooks, rate-limit, postgres, documentation]
requires:
  - whilly/scheduler/* (config, models, worker, jql_executor, deduplicator, webhooks, rate_limit, repository, sql_repository, metrics)
  - whilly/core/scheduler.py
  - whilly/cli/scheduler.py
provides:
  - openspec/specs/scheduling/spec.md (normative scheduling capability spec, PLAT-03)
affects:
  - .planning/REQUIREMENTS.md (PLAT-03 marked done)
  - .planning/STATE.md (Current Position advanced to 26-03 complete)
tech-stack:
  added: []
  patterns: [reverse-spec from v4 code, OpenSpec 1.4.1 strict format, SHALL/MUST first body line, WHEN/THEN scenarios]
key-files:
  created:
    - openspec/specs/scheduling/spec.md
  modified:
    - .planning/REQUIREMENTS.md
    - .planning/STATE.md
decisions:
  - "Specced the v4 scheduler at observed-behavior altitude: SchedulerRule defaults, worker due-rule + concurrent gather, poll-cycle terminal states, JQL execution/validation, hash dedup, webhook dispatch, rate limiting, Postgres-backed repository, and CLI surface."
  - "Described persistence as SQLSchedulerRepository (Postgres-backed, SQLAlchemy) as primary with InMemorySchedulerRepository as dev/test impl, consistent with the v4 SQL state layer — the abstract SchedulerRepository base raises NotImplementedError and is not the live backing."
  - "Did not over-claim: validate_jql is a dry-run executor (not Jira's /jql/validate endpoint); webhook matches_rule is a simplified check; these were specced as observed."
metrics:
  duration: ~9 min
  completed: 2026-06-16
---

# Phase 26 Plan 03: Scheduling Capability Spec Summary

Authored `openspec/specs/scheduling/spec.md` (PLAT-03) — a normative OpenSpec capability spec reverse-spec'd from the real v4 scheduler subsystem, passing `openspec validate scheduling --strict` (exit 0, valid).

## What was built

One file: `openspec/specs/scheduling/spec.md`. A `## Purpose` (>50 chars) plus `## Requirements` with 11 `### Requirement:` blocks, each with a SHALL/MUST first body line and one or more `#### Scenario:` (WHEN/THEN) blocks:

1. **Scheduler rule definition** — immutable `SchedulerRule` fields + defaults (enabled=True, 300s interval, 50 max results, `("key","summary")` dedup fields) + `to_dict` serialization.
2. **Configuration loading and validation** — `load_scheduler_config` JSON/TOML; `SchedulerConfigError` on missing required fields, non-positive intervals, unsupported suffix.
3. **Worker due-rule selection** — disabled-rule filtering at construction, per-rule interval gating, concurrent `asyncio.gather` of due rules.
4. **Poll cycle execution and recording** — `SchedulerPollCycle` terminal `completed`/`failed`, JQL failure isolated, last-polled timestamp updated after every cycle (success or failure).
5. **JQL execution against Jira** — `execute_jql` returns issue dicts / raises `JQLExecutionError`; `validate_jql` dry-run returns True/False.
6. **Issue deduplication** — `deduplicate_issues` hash-based suppression, pre-seen hashes honored, unhashable issues skipped.
7. **Webhook event handling** — `JiraWebhookEvent` parsing, `WebhookEventHandler` per-type dispatch, invalid payload `ValueError`, per-callback error isolation.
8. **Rate limiting and backoff** — `RateLimiter.call_with_retry` bounded backoff + final re-raise; `PollRateLimiter` min-interval + per-minute cap.
9. **Postgres-backed scheduler repository** — `SQLSchedulerRepository` persistence (create/get rule, duplicate rejection, record cycle with assigned id, last successful poll); `InMemorySchedulerRepository` dev/test impl.
10. **Scheduler CLI surface** — `whilly scheduler run/validate/list/status/enable/disable`; enable/disable require `WHILLY_DATABASE_URL`.

## How it was verified

`openspec validate scheduling --strict` → "Specification 'scheduling' is valid", exit 0.

## Coverage of mapped modules

All scheduling-mapped modules accounted for: `scheduler/{config,models,worker,jql_executor,deduplicator,webhooks,rate_limit,repository,sql_repository,metrics,docs}.py`, `core/scheduler.py` (DAG primitives referenced in Purpose scope), and `cli/scheduler.py`. `metrics.py` (PollMetrics/MetricsCollector) and `docs.py` (SchedulerDocumentation) are supporting/observability surface folded under the worker/CLI behavior at subsystem altitude rather than given dedicated requirements.

## Deviations from Plan

None - plan executed exactly as written. No whilly/ Python changes (documentation only).

## Self-Check: PASSED

- FOUND: openspec/specs/scheduling/spec.md
- `openspec validate scheduling --strict` → valid (exit 0)
- PLAT-03 marked `[x]` in REQUIREMENTS.md; traceability row updated to Done
- STATE.md Current Position advanced to 26-03 complete
