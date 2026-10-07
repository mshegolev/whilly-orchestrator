# Durable Collaboration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox syntax for tracking.

**Goal:** Extend existing mailbox delivery across feature sessions without new execution authority.

**Architecture:** Extend existing Whilly product workflow through pure policies and explicit adapter ports. PostgreSQL is canonical; derived search and external observability never authorize work.

**Tech Stack:** Existing Python 3.12 environment, asyncpg/PostgreSQL, Alembic, FastAPI, pytest and existing vanilla JS UI. No new model or vector database required.

**Spec:** ../specs/2026-09-29-swarm-learning-design.md

**Acceptance:** completed in the safe, policy-disabled mode on 2026-09-29.
Detailed task check evidence and review verdicts are recorded in
`.superpowers/sdd/2026-09-29-swarm-messages/progress.md`; live schema038,
admin/CSRF/browser acceptance verified. Policy activation is not included.

## Global Constraints

- Initial release is observe-and-propose. No autonomous policy changes, protected configuration edits, merges, deployments or production-data access.
- All model calls share the existing global cap of five. Missing profiles fail closed; no expensive fallback.
- Private product knowledge stays in private configured storage. Public source code contains neutral schemas/examples only.
- Preserve existing uncommitted changes in /opt/develop/whilly-swarm. No mixed commits; task checkpoints are reports/diffs until the owner approves a clean integration boundary.
- Use cheap explicitly selected subagent models only; at most five simultaneous subagents. Re-check available model identifiers at execution time.
- This is increment 2/5; requires acceptance of increment 1 before integration.
- Additive migrations numbered against the current head at execution time; numbers below assume head035 and must be adjusted if another agent added a revision.

## Review Focus

- Access-filtered data must never leak through indexes or diagnostics (L1).
- Crash/retry and timezone boundaries must not duplicate side effects (L2/L4).
- Changed plans or policies must invalidate previously valid authority (L3/L5).
- Malicious sources or DNS changes must not turn research into privileged execution (L4).
- Missing evidence or costs must remain unknown rather than passing evaluation (L1/L5).

## File ownership

Proposed files: whilly/swarm/learning/messages.py; whilly/adapters/db/learning_messages.py; whilly/swarm/mailbox.py; whilly/api/swarm_messages.py; migrations/037_learning_messages.py. Shared runtime/API/UI integration belongs to the controller; do not dispatch simultaneous edits to those files. Store adapters implement domain ports, never the reverse. Cross-increment result DTOs are frozen dataclasses with explicit fields named below; use strings only for documented finite states.

### Task 2.1: Envelope and durable delivery

**Files:** messages.py; learning_messages.py; 037_learning_messages.py. Test: tests/unit/test_learning_messages_1.py; real DB cases: tests/integration/test_learning_messages.py.

**Interfaces:** MessageEnvelope (id, product_id, feature_id, task_id, sender_id, recipient_project, recipient_role, kind, correlation_id, causation_id, idempotency_key, expires_at, hop_count, payload, evidence_refs). MessageService.send(principal: Principal, envelope: MessageEnvelope) -> DeliveryReceipt; MessageService.ack(principal: Principal, message_id: str) -> DeliveryReceipt. Receipt states persisted/delivered/acknowledged/rejected/expired. Producer creation and outbox insert share a transaction; delivery uses unique inbox key.

- [ ] Write failing tests: test_sender_is_host_pinned; test_cross_product_message_denied; test_duplicate_delivery_one_inbox; test_ack_does_not_complete_task. Required assertions: Retry after a crash between persistence and acknowledgment produces one logical message and no duplicate side effect.
- [ ] Run the new unit file with `.venv/bin/python -m pytest -q <exact test file above>`; confirm the named missing behavior fails, not imports/dependency setup unrelated to the task.
- [ ] Implement only this task's interfaces and minimum supporting DTOs; expose no alternate authorization path.
- [ ] Run the same tests to green, then affected existing tests. DB tests use the private check.py wrapper against whilly_swarm_test only, serially; never reset the operational database.
- [ ] Fresh reviewer checks spec compliance, failure cases and diff scope. Record changed files, commands/results and blockers; preserve unrelated work without committing it.

### Task 2.2: Mailbox and UI/API integration

**Files:** mailbox.py; swarm_messages.py; adapters/transport/server.py; api/static/product-swarm.js; api/templates/product_swarm.html.j2. Test: tests/unit/test_learning_messages_2.py; real DB cases: tests/integration/test_learning_messages.py.

**Interfaces:** Retain legacy task mailbox behavior. New API is admin/CSRF protected; agents reach it only through coordinator-scoped delivery. DeliveryPolicy(max_payload_bytes: int, max_hops: int, max_fanout: int) must be configured before send. Missing recipients remain queued until expiry. Never interpret messages as commands.

- [ ] Write failing tests: test_offline_recipient_expiry; test_hop_limit_rejects_loop; test_restart_preserves_receipts; test_forged_message_cannot_dispatch. Required assertions: Browser exposes recipient, delivery state and evidence; no message body executes HTML or initiates model calls.
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
