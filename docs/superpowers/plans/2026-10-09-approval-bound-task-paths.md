# Approval-Bound Task Paths Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan.

**Goal:** Let an approved Swarm task change its declared source, test, and architecture paths while hard-protected and undeclared paths still fail closed before host verification.

**Architecture:** Extend the immutable plan task contract with optional normalized `allowed_paths`, persist it in `swarm_task_context`, and evaluate every committed candidate with one pure path-decision function. The function always denies hard-protected files, treats test/architecture paths as approval-protected, positively scopes all files when task scope is present, and intersects task scope with product policy when both exist. The coordinator records the approved and rejected scope in durable attempt evidence before verification or review.

**Tech Stack:** Python 3.12, dataclasses, `fnmatch`, asyncpg/PostgreSQL, Alembic, pytest/pytest-asyncio, Ruff, OpenSpec 1.4.1.

**Spec:** `docs/superpowers/specs/2026-10-09-approval-bound-task-paths-design.md`

## Global Constraints

- Follow OpenSpec propose → apply → archive for the `orchestration-loop` behavior change.
- Preserve legacy semantics when `allowed_paths` is omitted: ordinary files remain allowed; every protected file remains denied.
- Never allow task approval to authorize CI, dependency/toolchain, agent instruction, verification-policy, or governance files.
- Treat path authorization as permission to run host verification, never as verification or review evidence.
- Bind scope to the applied revision in PostgreSQL; rerun and resume must reuse the stored value.
- Keep the public repository free of private project paths, identifiers, session IDs, and incident data.
- Use explicit file staging for every commit; do not stage unrelated worktree changes.

## Review Focus

- Fail-closed ordering: hard protection must win over all allowlists; product policy must only narrow task scope.
- Path safety and matching: normalized repository-relative paths, bounded inputs, no traversal/absolute/drive/home/NUL forms.
- Compatibility: omitted task scope behaves exactly as before.
- Durability: task scope survives apply, restart, report, and rerun without consulting a mutable proposed plan.
- Evidence: `policy_changed` and `path_not_approved` remain distinct and include bounded offending paths.

---

### Task 1: Propose and validate the OpenSpec behavior delta

**Files:**
- Create: `openspec/changes/add-approval-bound-task-paths/proposal.md`
- Create: `openspec/changes/add-approval-bound-task-paths/tasks.md`
- Create: `openspec/changes/add-approval-bound-task-paths/specs/orchestration-loop/spec.md`
- Reference: `openspec/specs/orchestration-loop/spec.md`
- Reference: `openspec/AUTHORING.md`

**Step 1: Write the proposal**

Describe the current false rejection of owner-approved tests, the task-level scope boundary, the hard/approval-protected split, compatibility, persistence, and named failure outcomes. State explicitly that publication, merge, deployment, secrets, and production authority do not change.

**Step 2: Write the delta scenarios**

Add normative requirements and scenarios covering:

- safe optional `allowed_paths` in an approved task revision;
- hard-protected files denied even when declared;
- declared approval-protected test/architecture files reaching host verification;
- omitted scope retaining legacy behavior;
- scoped tasks rejecting ordinary undeclared paths as `path_not_approved`;
- product and task scopes forming an intersection;
- rerun/resume using stored applied scope;
- reports exposing approved patterns, changed files, and rejected paths.

**Step 3: Validate the proposal**

Run: `uv run openspec validate add-approval-bound-task-paths --strict`

Expected: exit 0 with the change valid.

**Step 4: Commit the proposal**

```bash
git add openspec/changes/add-approval-bound-task-paths
git commit -m "spec(swarm): propose approval-bound task paths"
```

### Task 2: Add the pure plan and path-authorization contracts with TDD

**Files:**
- Modify: `whilly/swarm/plan.py`
- Modify: `whilly/swarm/prompts.py`
- Modify: `whilly/swarm/verification.py`
- Modify: `tests/unit/test_swarm_registry_plan.py`
- Modify: `tests/unit/test_swarm_verification.py`

**Step 1: Write failing plan-contract tests**

In `tests/unit/test_swarm_registry_plan.py`, add tests proving:

- safe patterns round-trip through `PlanTask.to_dict()` in stable order;
- omission becomes `None`, not an implicit unrestricted list;
- a supplied empty list is invalid;
- duplicates normalize once while preserving order;
- absolute, traversal, backslash, home, drive-prefix, NUL, empty, overlong, and over-count inputs fail as `PlanError`;
- a pattern that can match a hard-protected path is rejected at plan validation;
- the planner schema and instructions name `allowed_paths` and require it for planned test/architecture changes.

Run: `uv run pytest -q tests/unit/test_swarm_registry_plan.py`

Expected: FAIL because the field and validation do not exist.

**Step 2: Write failing pure authorization tests**

In `tests/unit/test_swarm_verification.py`, replace direct all-or-nothing protected matching assertions with a decision contract such as:

```python
decision = authorize_candidate_paths(
    changed_paths,
    policy,
    task_allowed_paths=("src/**", "tests/test_signal.py"),
    product_allowed_paths=None,
)
assert decision.outcome == "allowed"
assert decision.rejected_paths == ()
```

Cover hard-protected denial, declared and undeclared approval-protected files, positive task scoping, omitted-scope compatibility, normalized `./` and separators, and every product/task intersection edge. Assert a hard-protected denial is `policy_changed`; an ordinary out-of-scope file is `path_not_approved`.

Run: `uv run pytest -q tests/unit/test_swarm_verification.py`

Expected: FAIL because the split constants and decision function do not exist.

**Step 3: Implement the smallest pure contracts**

- Add `allowed_paths: tuple[str, ...] | None` to `PlanTask` and `_TASK_KEYS`.
- Add one reusable safe-pattern normalizer in `whilly/swarm/plan.py`; keep explicit bounds beside the plan contract.
- Export separate `HARD_PROTECTED_PATHS` and `APPROVAL_PROTECTED_PATHS` from `verification.py`; keep registry `protected_paths` hard-protected because they are host policy.
- Add a frozen `PathAuthorization` result containing `outcome`, normalized approved patterns, and rejected paths.
- Implement `authorize_candidate_paths(...)` as a pure function with the review ordering above.
- Update `PLAN_SCHEMA_HINT` and the planner contract text. Do not put task allowlists into `VerificationPolicy.digest()`; they belong to the applied task revision, not host policy.

**Step 4: Run focused tests and lint**

Run:

```bash
uv run pytest -q tests/unit/test_swarm_registry_plan.py tests/unit/test_swarm_verification.py
uv run ruff check whilly/swarm/plan.py whilly/swarm/prompts.py whilly/swarm/verification.py tests/unit/test_swarm_registry_plan.py tests/unit/test_swarm_verification.py
uv run ruff format --check whilly/swarm/plan.py whilly/swarm/prompts.py whilly/swarm/verification.py tests/unit/test_swarm_registry_plan.py tests/unit/test_swarm_verification.py
```

Expected: all pass.

**Step 5: Commit the pure contract**

```bash
git add whilly/swarm/plan.py whilly/swarm/prompts.py whilly/swarm/verification.py tests/unit/test_swarm_registry_plan.py tests/unit/test_swarm_verification.py
git commit -m "feat(swarm): define approval-bound task paths"
```

### Task 3: Persist the approved scope in applied task context

**Files:**
- Create: `whilly/adapters/db/migrations/versions/043_swarm_task_allowed_paths.py`
- Modify: `whilly/adapters/db/schema.sql`
- Modify: `openspec/COVERAGE-MATRIX.md`
- Modify: `whilly/swarm/store.py`
- Modify: `whilly/swarm/runtime.py`
- Modify: `tests/integration/test_alembic_full_chain.py`
- Modify: `tests/integration/test_swarm_runtime.py`
- Modify: `tests/integration/test_swarm_atomic.py`

**Step 1: Write failing persistence tests**

- Extend the Alembic full-chain contract to require revision `043_swarm_task_allowed_paths` and an `allowed_paths JSONB NULL` column with an array-or-null check constraint.
- Extend swarm runtime apply/status tests to assert an explicit list is copied into `swarm_task_context`, while omission remains SQL `NULL`.
- Extend atomic apply tests so a transaction failure cannot leave a partially persisted scope.
- Add a rerun test proving the same stored tuple is returned after failure/rerun and cannot be widened by changing an in-memory plan object.

Run:

```bash
uv run pytest -q tests/integration/test_alembic_full_chain.py tests/integration/test_swarm_runtime.py tests/integration/test_swarm_atomic.py
```

Expected: FAIL on the absent column and store field.

**Step 2: Add the migration and canonical schema**

- Create migration `043` with `down_revision = "042_learning_evaluations"` after confirming it is still the sole head.
- Add nullable `allowed_paths JSONB` to `swarm_task_context` with `jsonb_typeof(allowed_paths) = 'array'` when non-null.
- Mirror the column and constraint in `whilly/adapters/db/schema.sql` and map the migration in `openspec/COVERAGE-MATRIX.md`.

**Step 3: Thread scope through apply and reads**

- Add `allowed_paths` to `PreparedTask`.
- Populate it from the validated `PlanTask` in `SwarmService.apply_revision()`.
- Insert it atomically into `swarm_task_context` and include it in `_SESSION_TASKS_SQL` / `_JSON_COLUMNS` so status and `task_context()` return typed data.
- Preserve `None` versus a non-empty list exactly; do not synthesize a wildcard.

**Step 4: Run focused persistence verification**

Run:

```bash
uv run pytest -q tests/integration/test_alembic_full_chain.py tests/integration/test_swarm_runtime.py tests/integration/test_swarm_atomic.py
uv run ruff check whilly/swarm/store.py whilly/swarm/runtime.py whilly/adapters/db/migrations/versions/043_swarm_task_allowed_paths.py
```

Expected: all pass; Docker-backed tests may skip only with an explicit Docker-unavailable reason.

**Step 5: Commit persistence**

```bash
git add whilly/adapters/db/migrations/versions/043_swarm_task_allowed_paths.py whilly/adapters/db/schema.sql openspec/COVERAGE-MATRIX.md whilly/swarm/store.py whilly/swarm/runtime.py tests/integration/test_alembic_full_chain.py tests/integration/test_swarm_runtime.py tests/integration/test_swarm_atomic.py
git commit -m "feat(swarm): persist approved task paths"
```

### Task 4: Enforce scope before verification and make evidence durable

**Files:**
- Modify: `whilly/swarm/runtime.py`
- Modify: `whilly/swarm/prompts.py`
- Modify: `tests/unit/test_swarm_execution_integration.py`
- Modify: `tests/integration/test_guarded_swarm_runtime.py`
- Modify: `tests/integration/test_product_swarm.py`
- Modify: `tests/integration/test_swarm_runtime.py`

**Step 1: Write failing coordinator tests**

Add coordinator-level cases proving:

- exact declared source plus test files pass the path gate and call host verification;
- an undeclared sibling test stops before `_verify()` as `policy_changed`;
- an undeclared ordinary source file stops before `_verify()` as `path_not_approved`;
- `.gitlab-ci.yml`, `pyproject.toml`, `AGENTS.md`, and registry-added protected paths stop as `policy_changed` even if declared;
- a Product Workflow project rejects a path outside `project.product_policy.allowed_paths` even when the task declares it;
- omission preserves the current ordinary-file behavior and denies tests;
- both success and failure attempt results contain normalized `approved_paths`, `changed_files`, and `rejected_paths`.

Run:

```bash
uv run pytest -q tests/unit/test_swarm_execution_integration.py tests/integration/test_guarded_swarm_runtime.py tests/integration/test_product_swarm.py tests/integration/test_swarm_runtime.py
```

Expected: FAIL because runtime still calls `protected_changes()` and does not record scope evidence.

**Step 2: Put approved scope in the worker prompt**

Extend `build_worker_prompt()` with `allowed_paths: Sequence[str] | None`. Tell the worker that declared paths are a hard candidate boundary and that changing any other file will be rejected; render omission as legacy/unscoped ordinary paths without implying protected-file permission.

**Step 3: Replace the coordinator gate**

Immediately after `gitops.changed_files()` and before `_verify()`:

- read the immutable task scope from `ctx["allowed_paths"]`;
- read product scope from `project.product_policy.allowed_paths` when product policy exists;
- call `authorize_candidate_paths()` once;
- update the attempt `result` with approved patterns, changed files, and rejected paths before raising;
- raise `_TaskFailure(decision.outcome, ...)` for `policy_changed` or `path_not_approved`.

Do not consult the current proposal, registry file, worker result `touched_files`, or filesystem glob expansion for authority.

**Step 4: Preserve durable failure evidence and reports**

Confirm `_fail()` persists the enriched `result` for both named failures and that `SwarmService.report()` exposes it through full attempt views. Add only the minimal report code needed if current serialization does not preserve all fields.

**Step 5: Run focused coordinator verification**

Run:

```bash
uv run pytest -q tests/unit/test_swarm_execution_integration.py tests/integration/test_guarded_swarm_runtime.py tests/integration/test_product_swarm.py tests/integration/test_swarm_runtime.py
uv run ruff check whilly/swarm/runtime.py whilly/swarm/prompts.py tests/unit/test_swarm_execution_integration.py tests/integration/test_guarded_swarm_runtime.py tests/integration/test_product_swarm.py tests/integration/test_swarm_runtime.py
```

Expected: all pass and rejection tests prove `_verify()` was not called.

**Step 6: Commit enforcement**

```bash
git add whilly/swarm/runtime.py whilly/swarm/prompts.py tests/unit/test_swarm_execution_integration.py tests/integration/test_guarded_swarm_runtime.py tests/integration/test_product_swarm.py tests/integration/test_swarm_runtime.py
git commit -m "feat(swarm): enforce approved candidate paths"
```

### Task 5: Run regression gates and archive the OpenSpec change

**Files:**
- Modify: `openspec/specs/orchestration-loop/spec.md` (through `openspec archive`)
- Move/archive: `openspec/changes/add-approval-bound-task-paths/`

**Step 1: Run the focused complete slice**

Run:

```bash
uv run pytest -q tests/unit/test_swarm_registry_plan.py tests/unit/test_swarm_verification.py tests/unit/test_swarm_execution_integration.py tests/integration/test_swarm_runtime.py tests/integration/test_swarm_atomic.py tests/integration/test_guarded_swarm_runtime.py tests/integration/test_product_swarm.py tests/integration/test_alembic_full_chain.py
```

Expected: all pass, with only explicit environment-dependent skips.

**Step 2: Run repository gates**

Run:

```bash
make lint
make test
uv run openspec validate add-approval-bound-task-paths --strict
```

Expected: all pass. If `make test` cannot run because Docker or another external dependency is unavailable, record the exact named gap and do not claim full verification.

**Step 3: Archive and validate the living spec**

Run the repository-supported OpenSpec archive command for `add-approval-bound-task-paths`, then:

```bash
uv run openspec validate --all --strict
```

Expected: the change is archived, `openspec/specs/orchestration-loop/spec.md` contains the new guarantees, and all specs validate.

**Step 4: Commit the archive and final verification state**

```bash
git add openspec/specs/orchestration-loop/spec.md openspec/changes
git commit -m "spec(swarm): archive approval-bound task paths"
```

### Task 6: Prove first-use acceptance with a clean private revision

**Files:**
- No tracked public-repository files unless a neutral, redacted regression is discovered and separately approved.
- Local evidence only: configured private registry/session state and generated `report.json`.

**Step 1: Recheck operational preflight**

Confirm the intended private project registry, Codex implementation engine, clean base SHA, worktree health, database readiness, and absence of an active coordinator. Do not repair, merge, delete, or reattach retained failed worktrees.

**Step 2: Submit a new revision with exact scope**

Create a fresh plan revision that declares only the required source, test, fixture, architecture, and ignore-file patterns. Do not reuse or manually salvage the rejected candidate commit.

**Step 3: Run one Codex worker through the full coordinator flow**

Approve and run the new revision. Required evidence:

- worker completes within its configured profile;
- path authorization records no rejected paths;
- host baseline/candidate verification runs;
- independent read-only review approves;
- task reaches `DONE`;
- nothing is merged or pushed automatically.

**Step 4: Inspect the durable report**

Verify `report.json` includes the applied revision, approved patterns, changed files, empty rejected paths, verification evidence, review verdict, and retained worktree/branch. Redact private identifiers from any public handoff.

**Step 5: Record the operational result**

Post a concise board entry naming success or the exact blocker. If acceptance exposes a code defect, return to a new scoped TDD task rather than editing the retained candidate in place.
