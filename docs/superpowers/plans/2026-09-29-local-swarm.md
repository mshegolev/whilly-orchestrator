# Local multi-project swarm implementation plan

> Execute through implementation subagents, followed by independent review. Preserve existing checkout and deployments.

**Goal:** One persistent conversation coordinates autonomous, bounded local work across a registered product ecosystem, including development, telemetry, evaluation, analysis and visualization.

**Architecture:** Add an opt-in `whilly swarm` CLI using Whilly's existing PostgreSQL task queue and optimistic transitions. Persist conversations, plan revisions, task context/results and addressed messages in additional tables. Use local Git worktrees, not shared working copies. A project-specific registry is private configuration, never public source.

**Stack:** Python 3.12, asyncpg, existing Whilly adapters, Claude Code and Codex CLIs through explicit engine configurations.

## Global constraints

- Public code/docs/tests contain only neutral demo projects. Local registry holds real paths and product data.
- Existing Whilly commands remain compatible. No replacement queue, automatic push, merge, deployment or production export.
- User explicitly authorized autonomous planning and local implementation via subagents; proceed without repeated planning approvals.
- Tool permissions are not OS isolation. Document this honestly; explicit tool sets and disabled inherited MCP configurations are required for read-only planners/reviewers.
- No claims of verified eval quality without a real dataset, versioned evaluators and observed results. Missing data and unavailable services are named outcomes.
- Every behavioral change carries an OpenSpec proposal, implementation tests, then archive.
- Shared board remains a compatibility/status surface, not a second swarm scheduler. Do not turn on two dispatchers for the same task.

## Task 1: Durable swarm command and runtime

Files: new `whilly/swarm/` package, `whilly/cli/swarm.py`, lazy dispatch in `whilly/cli/__init__.py`, migration after 028, unit/integration tests, `docs/Local-Swarm.md`, OpenSpec delta + coverage matrix.

- [x] Add JSON registry: projects keyed by ID with path, base ref, purpose, dependencies, context_files, verification commands; roles map to project IDs and purpose. Validate unknown dependencies, IDs, paths, cycles and bounded configuration. Git bare repositories supported via worktrees.
- [x] Add persistent conversation (`chat`), user messages and revisioned structured plans. Discussion does not silently execute work; explicit run command or `/run` starts approved local work. Support interactive chat and one-shot message with session ID. Ask agent for a JSON plan containing task IDs, project, role, description, dependencies and verification argv commands; validate before queue insertion. Invalid plans leave named error and conversation history, never partial tasks.
- [x] Reuse Whilly plans/tasks and TaskRepository claim/start/complete/fail with version guards; store swarm session metadata and per-task context separately. Atomic plan revisions; reject revision changes while workers run unless stopped. Existing Whilly dashboard should show imported task states.
- [x] Execute bounded parallel workers (default 2, max 8), each task in a new worktree from recorded base SHA. Include whole ecosystem overview, role, repo instructions and dependency result packets in prompt. Clean user checkouts are not a prerequisite because they are never edited. Retain worktrees and logs.
- [x] Use a configurable Claude executable (local wrapper calls `ch`). Explicit max turns, timeout and per-call budget. Heartbeat while agent runs; graceful stop/restart; refuse a second coordinator for a session; recover abandoned claimed tasks without duplicating live execution. No unbounded automatic retries.
- [x] Add `message` and `inbox` with session/task/sender/recipient validation, durable IDs and acknowledgement. Workers receive inbox and dependency handoffs at task start; agents can check/send while working via CLI. Do not pretend this is token-level interrupt injection into idle sessions.
- [x] Require structured result, touched files, base/head SHA and executable verification evidence. Run configured tests in worktree with timeout; use an independent read-only review before marking DONE. Store failure/review output and cost even when task fails. Missing tests means unverified, not successful acceptance.
- [x] Add `status`, `stop`, `resume`/rerun and result report with local artifacts and named blockers. Never auto-merge or push.
- [x] Tests: real temporary git/bare worktrees, invalid plan/dependency rejection, atomic revisions, duplicate coordinator, cancellation and restart, timeout/nonzero agent, invalid result, failed test/review, peer message isolation, dependent result hydration. PostgreSQL integration tests with real tables, injected agents permitted but not described as live-agent verification.

## Task 2: Local installation and private ecosystem profile

Files outside public source: a private installation directory containing registry, launch wrappers and state/logs; generic install instructions in public docs.

- [x] Inspect and verify all core and support repository paths, actual refs and local instructions; avoid reading credential-bearing remotes or production trace content.
- [x] Register core application/UI/MCPs; telemetry export and analysis; QA collection/ETL/visualization; discover evaluation-owning code and register a dedicated evaluator role separately from implementers.
- [x] Start local PostgreSQL with persistent volume and loopback binding, unique random credentials in chmod-600 config. Source-based Whilly environment, migrate only the dedicated database.
- [x] Install local commands and supervised local control plane without public tunnel or modifying existing services. Document start/stop/status and recovery.
- [x] Connect `ch` wrapper explicitly; strict planner/reviewer tools despite any permissions-bypass behavior inherited from the alias. Never print credentials.

## Task 3: Acceptance and handoff

- [x] Independent code review and fixes, focused tests plus existing regression suite.
- [x] Live database/API health and durable conversation after restart.
- [x] Real Claude planning and two-agent canary on disposable demo repositories, including dependency handoff and evidence-based acceptance. No edits to business repositories during canary.
- [x] Record real repository awareness without executing arbitrary backlog tasks or paid production evals. Separate installed functionality from credentials/permissions needed for production data.
- [x] Archive OpenSpec delta and record final commands, exact worktree/branch, test results and remaining limitations.

## Task 4: Dual-engine cooperation and token efficiency (owner addition)

- [x] Support `ch` (Claude Code) and `codex` as configurable task executors; explicit task override and role defaults. Preserve structured outcome, timeout, cancellation and cost/usage reporting across both adapters.
- [x] Independent review can use the other engine. Do not run duplicate implementations by default. Escalation/help uses addressed messages and narrow evidence packets, not the entire chat transcript.
- [x] Registry summaries are shared; repository details loaded on demand. Persist artifacts and include bounded excerpts/paths. Source is reread from SHA-pinned worktrees; no cross-session source cache was introduced, avoiding a stale-cache correctness risk.
- [x] Set bounded concurrency, max turns (where supported), wall-clock timeout, history/context length, retries and per-session call budgets. Unknown token or cost usage remains unknown, never zero.
- [x] Add adapter contract tests including missing executables, nonzero exits, malformed JSON, stdin prompt handling, token extraction and process-group cancellation. Run a real disposable-repository canary with both engines and a cross-engine handoff.

## Review focus

Stale revisions and duplicated workers; task output that claims success without tests; credential/trace leakage; bare repository and conflicting worktrees; stopped or crashed subprocesses continuing to edit.

## Task 5: Single browser conversation

- [x] Add opt-in authenticated `/swarm` UI within the existing Whilly server, bound to a server-configured registry, never a client-supplied filesystem path.
- [x] Reuse existing admin session authentication and CSRF protection. Anonymous/readonly users cannot launch local code. Show conversation, plan revisions, task states, results and blockers.
- [x] Discuss/plan, explicitly start a selected plan, stop, resume and inspect results from one page. Long planner/worker calls must not block status/stop requests. Bound duplicate operations and cancel live background jobs on server shutdown.
- [x] Verify route authorization, invalid input, errors shown in conversation, duplicate start and graceful shutdown. Test authenticated HTTP flow on the local installation; no claim of rendered UI acceptance without browser inspection.

Task 5 may be implemented in a separate worktree while Task 1 is underway: its owned files are `whilly/api/swarm_ui.py`, a new swarm template, its own tests and delta only. Core swarm runtime and registry remain Task 1/4 owned. Main coordinator integrates the router after independent review.

## Progress

- 2026-09-29: initial plan. Existing addressed-task implementation is reference for contracts, not enabled or merged. Local Docker runtime was stopped; starting it for dedicated installation.

- 2026-09-29: real two-engine canary accepted both synthetic tasks, with bidirectional addressed messages, coordinator-owned Codex commit, verification and independent review. First failed attempts remain visible as evidence.
- Browser acceptance covered login, creation, plan preview, resume, task/results, CSRF, desktop/mobile, real discussion and plan generation. Authenticated session and conversation survived service restart. No business plan was executed.
- Final review found two process-launch crash gaps; both reproduced by failing tests, fixed and independently rechecked. Operator confirmation is required for uncertain orphan identity; retries are never implicit.
- Source-cache implementation was deliberately omitted: bounded prompts, provider-reported cached usage, precise dependency packets and cheap explicit models provide the token controls without stale source reuse.
