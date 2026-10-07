# Whilly Product Adoption Completion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the recovered product-adoption branch internally consistent with current main and complete the guarded multi-repository merge, compensation, and stage-boundary domain workflow without contacting real providers.

**Architecture:** Keep GitLab I/O behind the origin-pinned transport and persist every external effect through the change-set store. A coordinator revalidates registry, approval, source SHA, target SHA, MR identity, and exact-SHA CI for every mandatory repository before the first merge; after the first merge, failures enter durable compensation rather than ordinary failure.

**Tech Stack:** Python 3.12, pytest, asyncpg/Alembic, httpx MockTransport, guarded Git executor, OpenSpec.

**Spec:** `openspec/specs/orchestration-loop/spec.md`, `openspec/changes/add-durable-product-change-sets/`, and the recovered multi-repo merge requirements in the Codex session evidence.

## Global Constraints

- No real push, MR, merge, deploy, GitLab, or model-provider call during implementation or verification.
- Every external effect is idempotent and has a durable receipt.
- No first merge until all mandatory repositories have exact-source-SHA green CI and unchanged target SHAs.
- A failure after any merge enters compensation and never reports DONE.
- Public tracked files contain only neutral example hosts, projects, identities, and credentials.

## Review Focus

- A pipeline for an older SHA must not open the merge barrier.
- A target SHA change between barrier and merge must stop before the next merge.
- Retry or restart must not repeat merge, revert, or delivery effects.
- GitLab unavailability after a partial merge must preserve PARTIAL_MERGE or ROLLBACK_FAILED.
- Stage acceptance must require immutable artifact and deployment evidence for every mandatory repository.

---

### Task 1: Establish the real recovered baseline

**Files:**
- Modify only files proven incompatible by a normal project test invocation.
- Test: `tests/unit/`

**Interfaces:**
- Consumes: recovered commit `57ea8928c8bce071a85e40d5aafb148abe970f86`.
- Produces: a categorized, reproducible list of genuine failures with fixture loading enabled.

- [x] Run the unit suite with the project `conftest.py` enabled and record exact failures.
- [x] For each failure family, trace the changed contract against current-main code and a working neighboring test.
- [x] Add or adjust the smallest regression test only after identifying the production incompatibility.
- [x] Apply one root-cause fix at a time and rerun the owning test family.
- [x] Commit the compatibility fixes with explicit paths.

### Task 2: Close durable change-set OpenSpec work

**Files:**
- Modify: `openspec/changes/add-durable-product-change-sets/tasks.md`
- Modify: `openspec/specs/state-persistence/spec.md`
- Test: `tests/unit/test_change_set.py`
- Test: `tests/integration/test_product_change_set_store.py`

**Interfaces:**
- Consumes: `ProductChangeSet`, `ProductChangeSetStore`, migration 041.
- Produces: archived, strictly validated canonical change-set requirements.

- [ ] Reproduce purity/canonical-spec gaps with focused checks.
- [ ] Complete the proportional regression and real disposable PostgreSQL checks.
- [ ] Archive the implemented change only after strict validation passes.
- [ ] Commit the completed OpenSpec lifecycle with explicit paths.

### Task 3: Add the product-wide exact-SHA merge barrier

**Files:**
- Create: `whilly/swarm/product_merge.py`
- Test: `tests/unit/test_product_merge.py`
- Modify: `openspec/specs/orchestration-loop/spec.md`

**Interfaces:**
- Consumes: `ProductRegistrySnapshot`, `ProductChangeSetStore`, `RepoPublicationReceipt`.
- Produces: `ProductMergeCoordinator.verify_barrier(change_id: str) -> MergeBarrierReceipt`.

- [ ] Write tests proving every mandatory repository, exact source SHA, required job, approval digest, and target SHA are required together.
- [ ] Verify the tests fail because the coordinator does not exist.
- [ ] Implement the immutable barrier receipt and fail-closed coordinator.
- [ ] Verify focused tests and existing publication tests pass.
- [ ] Commit the barrier with explicit paths.

### Task 4: Add sequential merge and durable compensation

**Files:**
- Modify: `whilly/swarm/gitlab_change_transport.py`
- Modify: `whilly/swarm/product_merge.py`
- Test: `tests/unit/test_product_merge.py`
- Test: `tests/integration/test_product_merge_guarded.py`

**Interfaces:**
- Consumes: `MergeBarrierReceipt` and durable effect receipts.
- Produces: idempotent merge/revert operations and durable MERGED, PARTIAL_MERGE, ROLLING_BACK, ROLLED_BACK, or ROLLBACK_FAILED transitions.

- [ ] Write failing tests for target drift, restart replay, partial merge, unavailable GitLab, and revert replay.
- [ ] Add only allowlisted GitLab merge/revert-MR endpoints with strict response identity checks.
- [ ] Merge in dependency order while rechecking the target immediately before each effect.
- [ ] Compensate merged repositories in reverse order through revert MRs.
- [ ] Verify no force-push, direct target update, merge-before-barrier, or repeated effect is possible.
- [ ] Commit the merge and compensation path with explicit paths.

### Task 5: Add stage delivery and acceptance boundaries

**Files:**
- Create: `whilly/swarm/product_delivery.py`
- Test: `tests/unit/test_product_delivery.py`
- Test: `tests/integration/test_product_delivery_guarded.py`
- Modify: `openspec/specs/orchestration-loop/spec.md`

**Interfaces:**
- Consumes: merged change-set, immutable artifact digests, declarative delivery policy.
- Produces: durable stage delivery and acceptance receipts without embedding a deployment provider.

- [ ] Write failing tests for missing artifacts, prod approval boundary, stale deployment SHA, unavailable probe, and replay.
- [ ] Implement an injected delivery port with durable effect receipts.
- [ ] Require passed stage acceptance for every mandatory artifact before DONE.
- [ ] Verify prod remains a separate explicit approval boundary.
- [ ] Commit the stage-boundary implementation with explicit paths.

### Task 6: Whole-branch verification and review

**Files:**
- Modify only defects found by verification or review.

**Interfaces:**
- Consumes: Tasks 1-5.
- Produces: evidence-backed local completion status.

- [ ] Run Ruff, import checks, strict OpenSpec validation, focused PostgreSQL checks, and the normal unit suite.
- [ ] Run guarded integration tests with real network access blocked.
- [ ] Request an independent whole-branch review.
- [ ] Fix every Critical or Important finding through a new red-green cycle.
- [ ] Record remaining environmental or externally authorized acceptance separately.
