# Cross-project Task Proposals Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox syntax for tracking.

**Goal:** Let specialists propose neighboring work with deduplication and approval-bound admission.

**Architecture:** Extend existing Whilly product workflow through pure policies and explicit adapter ports. PostgreSQL is canonical; derived search and external observability never authorize work.

**Tech Stack:** Existing Python 3.12 environment, asyncpg/PostgreSQL, Alembic, FastAPI, pytest and existing vanilla JS UI. No new model or vector database required.

**Spec:** ../specs/2026-09-29-swarm-learning-design.md

**Safe-mode acceptance (2026-09-29):** Tasks3.1/3.2 implemented and reviewed;
live migration039 and authenticated browser acceptance passed. Detailed evidence
and limitations are in `.superpowers/sdd/2026-09-29-swarm-proposals/progress.md`.
Contract-impact admission remains blocked without a host-bound producer/consumer
proof type; accepting a proposal changes only the specification, never execution.

## Global Constraints

- Initial release is observe-and-propose. No autonomous policy changes, protected configuration edits, merges, deployments or production-data access.
- All model calls share the existing global cap of five. Missing profiles fail closed; no expensive fallback.
- Private product knowledge stays in private configured storage. Public source code contains neutral schemas/examples only.
- Preserve existing uncommitted changes in /opt/develop/whilly-swarm. No mixed commits; task checkpoints are reports/diffs until the owner approves a clean integration boundary.
- Use cheap explicitly selected subagent models only; at most five simultaneous subagents. Re-check available model identifiers at execution time.
- This is increment 3/5; requires acceptance of increment 2 before integration.
- Additive migrations numbered against the current head at execution time; numbers below assume head035 and must be adjusted if another agent added a revision.

## Review Focus

- Access-filtered data must never leak through indexes or diagnostics (L1).
- Crash/retry and timezone boundaries must not duplicate side effects (L2/L4).
- Changed plans or policies must invalidate previously valid authority (L3/L5).
- Malicious sources or DNS changes must not turn research into privileged execution (L4).
- Missing evidence or costs must remain unknown rather than passing evaluation (L1/L5).

## File ownership

Proposed files: whilly/swarm/learning/proposals.py; whilly/adapters/db/learning_proposals.py; whilly/api/swarm_proposals.py; migrations/038_learning_proposals.py. Shared runtime/API/UI integration belongs to the controller; do not dispatch simultaneous edits to those files. Store adapters implement domain ports, never the reverse. Cross-increment result DTOs are frozen dataclasses with explicit fields named below; use strings only for documented finite states.

### Task 3.1: Proposal lifecycle

**Files:** proposals.py; learning_proposals.py; 038_learning_proposals.py. Test: tests/unit/test_learning_proposals_1.py; real DB cases: tests/integration/test_learning_proposals.py.

**Interfaces:** TaskProposal (id, origin_feature_id, origin_task_id, target_project, target_module, evidence_refs, outcome, contract_impact, acceptance, dependencies, resource_class, fingerprint). ProposalService.submit(principal: Principal, proposal: TaskProposal) -> ProposalResult; ProposalResult(id: str, status: str, duplicate_of: str | None, reason: str | None). Dedup scoped by product/target/fingerprint, not just similar prose.

- [ ] Write failing tests: test_duplicate_submission_one_proposal; test_unknown_project_rejected; test_evidence_required; test_proposal_is_not_execution. Required assertions: Store proposed/triaged/awaiting_approval/eligible/queued/running/verified/blocked/rejected/cancelled with explicit legal transitions and immutable event history.
- [ ] Run the new unit file with `.venv/bin/python -m pytest -q <exact test file above>`; confirm the named missing behavior fails, not imports/dependency setup unrelated to the task.
- [ ] Implement only this task's interfaces and minimum supporting DTOs; expose no alternate authorization path.
- [ ] Run the same tests to green, then affected existing tests. DB tests use the private check.py wrapper against whilly_swarm_test only, serially; never reset the operational database.
- [ ] Fresh reviewer checks spec compliance, failure cases and diff scope. Record changed files, commands/results and blockers; preserve unrelated work without committing it.

### Task 3.2: Admission and feature integration

**Files:** proposals.py; product_workflow.py; swarm_proposals.py; adapters/transport/server.py; api/static/product-swarm.js; api/templates/product_swarm.html.j2. Test: tests/unit/test_learning_proposals_2.py; real DB cases: tests/integration/test_learning_proposals.py.

**Interfaces:** ProposalService.evaluate(principal: Principal, proposal_id: str) -> ProposalResult checks current registry, ownership, dependencies, protected paths, existing work and approval budget. Creating canonical task changes the plan revision and requires existing ProductWorkflow approval. Do not add an alternative approval API or dispatch queue. Producer/consumer contract tests are required for contract-impacting proposals.

- [ ] Write failing tests: test_changed_plan_requires_approval; test_dependency_cycle_blocks; test_specialist_cannot_approve_scope; test_cross_repo_contract_requires_consumer_check; test_budget_exhaustion_blocks. Required assertions: UI offers accept-for-planning/reject with reason; accepting for planning does not launch execution. Existing Gantt shows linked dependencies after approved canonical planning.
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
