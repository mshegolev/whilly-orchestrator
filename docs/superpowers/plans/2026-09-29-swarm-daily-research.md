# Daily Research and Retrospectives Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox syntax for tracking.

**Goal:** Provide disabled-by-default bounded research runs and session analysis producing evidence-backed proposals.

**Architecture:** Extend existing Whilly product workflow through pure policies and explicit adapter ports. PostgreSQL is canonical; derived search and external observability never authorize work.

**Tech Stack:** Existing Python 3.12 environment, asyncpg/PostgreSQL, Alembic, FastAPI, pytest and existing vanilla JS UI. No new model or vector database required.

**Spec:** ../specs/2026-09-29-swarm-learning-design.md

## Global Constraints

- Initial release is observe-and-propose. No autonomous policy changes, protected configuration edits, merges, deployments or production-data access.
- All model calls share the existing global cap of five. Missing profiles fail closed; no expensive fallback.
- Private product knowledge stays in private configured storage. Public source code contains neutral schemas/examples only.
- Preserve existing uncommitted changes in /opt/develop/whilly-swarm. No mixed commits; task checkpoints are reports/diffs until the owner approves a clean integration boundary.
- Use cheap explicitly selected subagent models only; at most five simultaneous subagents. Re-check available model identifiers at execution time.
- This is increment 4/5; requires acceptance of increment 3 before integration.
- Additive migrations numbered against the current head at execution time; numbers below assume head035 and must be adjusted if another agent added a revision.

## Review Focus

- Access-filtered data must never leak through indexes or diagnostics (L1).
- Crash/retry and timezone boundaries must not duplicate side effects (L2/L4).
- Changed plans or policies must invalidate previously valid authority (L3/L5).
- Malicious sources or DNS changes must not turn research into privileged execution (L4).
- Missing evidence or costs must remain unknown rather than passing evaluation (L1/L5).

## File ownership

Proposed files: whilly/swarm/learning/research.py, schedules.py; whilly/adapters/http/research_fetch.py; whilly/adapters/db/learning_runs.py; whilly/api/swarm_research.py; migrations/039_learning_runs.py. Shared runtime/API/UI integration belongs to the controller; do not dispatch simultaneous edits to those files. Store adapters implement domain ports, never the reverse. Cross-increment result DTOs are frozen dataclasses with explicit fields named below; use strings only for documented finite states.

### Task 4.1: Scheduling and atomic budgets

**Files:** schedules.py; learning_runs.py; 039_learning_runs.py; swarm/admission.py. Test: tests/unit/test_learning_daily_research_1.py; real DB cases: tests/integration/test_learning_daily_research.py.

**Interfaces:** Schedule (id, timezone, run_window, enabled, model_profiles, limits, source_policy, retention_policy); RunLimits (max_calls, max_seconds, max_documents, max_bytes). Scheduler.claim_due(now: datetime) -> ResearchRun | None uses unique schedule/window plus durable lease. Missing explicit config blocks activation. Skip older missed windows with named outcomes; never replay a backlog automatically. Use existing global model admission cap5, with background work admitted only when interactive reservations allow.

- [ ] Write failing tests: test_two_schedulers_one_run; test_restart_skips_backlog; test_dst_window_unique; test_ambiguous_orphan_not_reclaimed; test_background_cannot_consume_interactive_budget. Required assertions: Atomic reservation across processes prevents exceeding call/time/concurrency budgets. Schedules remain disabled after installation/migration.
- [ ] Run the new unit file with `.venv/bin/python -m pytest -q <exact test file above>`; confirm the named missing behavior fails, not imports/dependency setup unrelated to the task.
- [ ] Implement only this task's interfaces and minimum supporting DTOs; expose no alternate authorization path.
- [ ] Run the same tests to green, then affected existing tests. DB tests use the private check.py wrapper against whilly_swarm_test only, serially; never reset the operational database.
- [ ] Fresh reviewer checks spec compliance, failure cases and diff scope. Record changed files, commands/results and blockers; preserve unrelated work without committing it.

### Task 4.2: Restricted research and retrospective

**Files:** research.py; research_fetch.py; learning_runs.py. Test: tests/unit/test_learning_daily_research_2.py; real DB cases: tests/integration/test_learning_daily_research.py.

**Interfaces:** ResearchFetcher.fetch(url: str, policy: FetchPolicy) -> ResearchDocument. FetchPolicy explicitly bounds destinations, redirects, bytes and timeout. Resolve and validate public HTTPS destinations at each hop; pin the vetted connection destination or use an equally strong egress boundary. Never use a separate DNS precheck followed by an unvalidated reconnect. Documents remain untrusted and cannot execute tools. RetrospectiveService.analyze(run_id: str, events: list[dict]) -> DailyReport uses authorized redacted structured events, not hidden reasoning.

- [ ] Write failing tests: test_private_and_metadata_destinations_denied; test_redirect_and_dns_rebinding_denied; test_oversize_document_stops; test_malicious_page_cannot_change_policy; test_no_data_is_not_success. Required assertions: DailyReport includes sources/dates, observations, sample size, missing data, hypotheses, proposals, latency and known/unknown costs. Do not classify confidence as factual verification.
- [ ] Run the new unit file with `.venv/bin/python -m pytest -q <exact test file above>`; confirm the named missing behavior fails, not imports/dependency setup unrelated to the task.
- [ ] Implement only this task's interfaces and minimum supporting DTOs; expose no alternate authorization path.
- [ ] Run the same tests to green, then affected existing tests. DB tests use the private check.py wrapper against whilly_swarm_test only, serially; never reset the operational database.
- [ ] Fresh reviewer checks spec compliance, failure cases and diff scope. Record changed files, commands/results and blockers; preserve unrelated work without committing it.

### Task 4.3: Manual dry run and chief report

**Files:** swarm_research.py; cli/swarm.py; adapters/transport/server.py; api/static/product-swarm.js; api/templates/product_swarm.html.j2. Test: tests/unit/test_learning_daily_research_3.py; real DB cases: tests/integration/test_learning_daily_research.py.

**Interfaces:** Add authenticated run/status/report/stop controls. Manual dry run uses fixture fetch/model adapters only. Real run requires enabled trusted configuration and an explicit selected model. Save report/proposals atomically or record partial failure; integrate with L1 candidate knowledge and L3 proposals, never direct execution.

- [ ] Write failing tests: test_report_survives_restart; test_stop_blocks_new_calls; test_partial_failure_visible; test_unredacted_query_denied. Required assertions: Browser displays latest run, schedule-disabled status, evidence, costs and blockers. Provider outage has bounded retry and no expensive fallback.
- [ ] Run the new unit file with `.venv/bin/python -m pytest -q <exact test file above>`; confirm the named missing behavior fails, not imports/dependency setup unrelated to the task.
- [ ] Implement only this task's interfaces and minimum supporting DTOs; expose no alternate authorization path.
- [ ] Run the same tests to green, then affected existing tests. DB tests use the private check.py wrapper against whilly_swarm_test only, serially; never reset the operational database.
- [ ] Fresh reviewer checks spec compliance, failure cases and diff scope. Record changed files, commands/results and blockers; preserve unrelated work without committing it.

## Acceptance and handoff

- [ ] Run `.venv/bin/lint-imports --no-cache`, focused Ruff and `git diff --check`.
- [ ] Verify named boundary failures with isolated negative/mutation fixtures; never mutate a live repository to test a guard.
- [ ] If UI changed, verify authenticated browser flows, CSRF, desktop/mobile, persistence after safe restart and no model calls from view-only actions.
- [ ] Update docs/Product-Swarm.md and OpenSpec capability delta with the actual supported increment and explicit limitations.
- [ ] Controller self-review maps results to the spec; hand off working code with schedule/export disabled. Report mocked versus live evidence separately.
