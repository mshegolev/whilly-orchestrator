# Whilly Swarm Recovery Implementation Plan

> **Execution:** use subagent-driven development in an isolated recovery worktree; every implementation task receives a fresh reviewer before integration.

**Goal:** finish the smallest evidence-backed recovery needed to begin using the guarded Whilly swarm, add measured mutation testing, and close the guarded-execution capability lifecycle.

**Architecture:** preserve the existing guarded executor and product workflow. Add one narrow exception translation at the coordinator boundary, a scoped mutmut configuration, durable audit documentation, and canary evidence. Do not recover optional prototypes or superseded UI.

**Tech stack:** Python 3.12, pytest, Ruff, import-linter, OpenSpec, uv, mutmut 3.8.x.

---

### Task 1: Preserve named executor blockers

**Files:**
- Modify: `whilly/swarm/runtime.py`
- Test: `tests/unit/test_swarm_execution_integration.py`
- Modify: `openspec/changes/add-guarded-swarm-execution/specs/orchestration-loop/spec.md`
- Modify: `openspec/changes/add-guarded-swarm-execution/tasks.md`

1. Add a failing coordinator-level test proving an `ExecutionBlocked("execution_isolation_unavailable")` becomes that durable task outcome.
2. Run the focused test and observe the current `coordinator_error` failure.
3. Add a specific `except ExecutionBlocked` before the catch-all and translate its reason without changing cancellation/version-conflict paths.
4. Update the active OpenSpec requirement and mark launcher review complete only when its inventory document exists.
5. Run the focused integration tests, Ruff, and strict OpenSpec validation.

### Task 2: Add the scoped mutation pilot

**Files:**
- Modify: `pyproject.toml`
- Modify: `uv.lock`
- Modify: `.gitignore`
- Create: `docs/testing/mutation-testing.md`
- Test: `tests/unit/core/test_gates.py`

1. Pin a project-local compatible mutmut 3.8.x development dependency.
2. Configure `source_paths = ["whilly/core/gates.py"]`, select `tests/unit/core/test_gates.py`, and keep execution serial and deterministic.
3. Ignore only generated mutmut state.
4. Run the exact baseline test, then the mutation pilot.
5. Record the environment, commands, counts, duration, and survivor dispositions. Do not invent a score gate.

### Task 3: Publish the recovery and launcher inventory

**Files:**
- Create: `docs/swarm/recovery-manifest-2026-10-07.md`
- Modify: `docs/CODEX-MISSION.md`

1. Record every old candidate file group as merged, superseded, optional, or rejected, with current evidence.
2. Record every swarm launcher and every intentional helper/legacy subprocess outside the boundary.
3. State the remaining live-use gates and exact rollback/stop behavior.
4. Review the document against source paths and remove internal-only identifiers.

### Task 4: Integrate and verify offline

1. Review each task diff independently and apply only accepted patches.
2. Run focused tests, all unit tests, `make lint`, import-linter, and `openspec validate --all --strict`.
3. Run the existing fake/offline swarm acceptance path with no provider/network dependency.
4. Archive `add-guarded-swarm-execution` only if all capability requirements and its checklist are satisfied; re-run strict validation.

### Task 5: Run one bounded live canary

1. Confirm the configured provider/toolchain is available without printing credentials.
2. Prove from the effective runtime configuration that automatic `publish_feature()` will fail closed before transport construction; if it cannot be proved, record the blocker and do not run.
3. Create one disposable feature with one small documentation-only task, one worker, and the configured cost/time limit.
4. Observe discussion/plan/approval/run/review/verification and capture redacted evidence.
5. Stop immediately on auth, isolation, budget, publication, or policy failure and record the named blocker.
6. Commit focused changes, push the feature branch, open a PR, wait for required checks, merge only when green, and remove only the recovery worktree.
