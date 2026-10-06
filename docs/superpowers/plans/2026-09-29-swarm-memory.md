# Shared Memory Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox syntax for tracking.

**Goal:** Deliver versioned, access-filtered knowledge and bounded task context.

**Architecture:** Extend existing Whilly product workflow through pure policies and explicit adapter ports. PostgreSQL is canonical; derived search and external observability never authorize work.

**Tech Stack:** Existing Python 3.12 environment, asyncpg/PostgreSQL, Alembic, FastAPI, pytest and existing vanilla JS UI. No new model or vector database required.

**Spec:** ../specs/2026-09-29-swarm-learning-design.md

## Global Constraints

- Initial release is observe-and-propose. No autonomous policy changes, protected configuration edits, merges, deployments or production-data access.
- All model calls share the existing global cap of five. Missing profiles fail closed; no expensive fallback.
- Private product knowledge stays in private configured storage. Public source code contains neutral schemas/examples only.
- Preserve existing uncommitted changes in /opt/develop/whilly-swarm. No mixed commits; task checkpoints are reports/diffs until the owner approves a clean integration boundary.
- Use cheap explicitly selected subagent models only; at most five simultaneous subagents. Re-check available model identifiers at execution time.
- This is increment 1/5; establish shared types/ports before later increments.
- Additive migrations numbered against the current head at execution time; numbers below assume head035 and must be adjusted if another agent added a revision.

## Review Focus

- Access-filtered data must never leak through indexes or diagnostics (L1).
- Crash/retry and timezone boundaries must not duplicate side effects (L2/L4).
- Changed plans or policies must invalidate previously valid authority (L3/L5).
- Malicious sources or DNS changes must not turn research into privileged execution (L4).
- Missing evidence or costs must remain unknown rather than passing evaluation (L1/L5).

## File ownership

Proposed files: whilly/swarm/learning/domain.py, ports.py, memory.py; whilly/adapters/db/learning_memory.py; whilly/adapters/filesystem/knowledge_sources.py; whilly/api/swarm_memory.py; migrations/036_learning_memory.py. Shared runtime/API/UI integration belongs to the controller; do not dispatch simultaneous edits to those files. Store adapters implement domain ports, never the reverse. Cross-increment result DTOs are frozen dataclasses with explicit fields named below; use strings only for documented finite states.

### Task 1.1: Domain and immutable storage

**Files:** domain.py; ports.py; learning_memory.py; 036_learning_memory.py. Test: tests/unit/test_learning_memory_1.py; real DB cases: tests/integration/test_learning_memory.py.

**Interfaces:** KnowledgeRevision (id, product_id, project_id, kind, body, source_uri, source_sha, evidence_hash, observed_at, verified_at, expires_at, classification, status, supersedes, conflicts); Principal (actor_id, product_ids, project_ids, classifications); ContextPackage (items, omissions, conflicts, revision_manifest). MemoryStore.append(revision: KnowledgeRevision) -> KnowledgeRevision; MemoryStore.visible(principal: Principal, product_id: str, project_ids: tuple[str, ...]) -> list[KnowledgeRevision]. All timestamps UTC-aware; append-only revisions; DB FK/unique constraints; do not accept actor identity from payload.

- [ ] Write failing tests: test_memory_revision_is_immutable; test_other_product_cannot_read; test_visibility_filters_before_ranking; test_concurrent_revision_creation. Required assertions: Two simultaneous writes retain both revisions or produce an explicit revision conflict; hidden body never appears in retrieval output.
- [ ] Run the new unit file with `.venv/bin/python -m pytest -q <exact test file above>`; confirm the named missing behavior fails, not imports/dependency setup unrelated to the task.
- [ ] Implement only this task's interfaces and minimum supporting DTOs; expose no alternate authorization path.
- [ ] Run the same tests to green, then affected existing tests. DB tests use the private check.py wrapper against whilly_swarm_test only, serially; never reset the operational database.
- [ ] Fresh reviewer checks spec compliance, failure cases and diff scope. Record changed files, commands/results and blockers; preserve unrelated work without committing it.

### Task 1.2: Freshness and bounded retrieval

**Files:** memory.py; knowledge_sources.py; product_workflow.py. Test: tests/unit/test_learning_memory_2.py; real DB cases: tests/integration/test_learning_memory.py.

**Interfaces:** SourceVerifier.check(item: KnowledgeRevision) -> SourceCheck, with SourceCheck(status: Literal['verified','changed','unavailable'], revision: str | None). build_context(items: list[KnowledgeRevision], *, max_chars: int, now: datetime) -> ContextPackage. Only authorized local Git paths are verified by the filesystem adapter. Retrieval is deterministic lexical ranking initially, with stable id tie-break; unavailable sources remain unverified, conflicting facts are not silently chosen.

- [ ] Write failing tests: test_changed_sha_is_stale; test_unavailable_source_not_current; test_context_budget_reports_omissions; test_conflicting_sources_preserved. Required assertions: context length <= max_chars; the package includes exact revision ids; changed contract context goes through existing approval revalidation.
- [ ] Run the new unit file with `.venv/bin/python -m pytest -q <exact test file above>`; confirm the named missing behavior fails, not imports/dependency setup unrelated to the task.
- [ ] Implement only this task's interfaces and minimum supporting DTOs; expose no alternate authorization path.
- [ ] Run the same tests to green, then affected existing tests. DB tests use the private check.py wrapper against whilly_swarm_test only, serially; never reset the operational database.
- [ ] Fresh reviewer checks spec compliance, failure cases and diff scope. Record changed files, commands/results and blockers; preserve unrelated work without committing it.

### Task 1.3: API, redaction and architecture contract

**Files:** swarm_memory.py; adapters/transport/server.py; .importlinter; tests/integration/test_learning_memory.py. Test: tests/unit/test_learning_memory_3.py; real DB cases: tests/integration/test_learning_memory.py.

**Interfaces:** Admin/CSRF-protected /api/v1/swarm/knowledge endpoints for scoped retrieval, candidate submission and redaction. Only trusted verifier promotes facts; authorized owners record decisions. Retention policy required before transcript ingestion. Redaction removes body/search copies and retains non-sensitive tombstone; explicit exception for legally held content is owner-managed, never inferred.

- [ ] Write failing tests: test_worker_token_denied_memory_api; test_redaction_removes_search_content; test_missing_retention_blocks_ingestion; test_domain_import_violation_rejected. Required assertions: Add forbidden-import contract for whilly.swarm.learning.domain and ports: no DB, HTTP, subprocess, API or concrete adapter imports. Run lint-imports and prove a temporary isolated mutation fails.
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
