# Swarm recovery verification — 2026-10-07

Base: `origin/main` at `43948b0752c1810f34571d99b9f50583aab7b70b`.

## Accepted evidence

- Baseline focused suite before edits: 44 passed.
- Recovery unit suite: 3,761 passed, 3 skipped, 61 warnings in 173.36 seconds. The skips and warnings were reported explicitly; no failure was hidden.
- Fake/offline and local sandbox acceptance selection: 26 passed. This included `tests/local/test_swarm_execution_acceptance.py`, the guarded-executor integration tests, and product-workflow unit contracts.
- Ruff: 770 files already formatted; all checks passed when `make lint` used the project Python 3.12 environment.
- Import Linter: 4 contracts kept, 0 broken across 450 files and 2,783 dependencies.
- OpenSpec: the guarded-execution change completed propose/apply/archive; strict validation passed for all 35 remaining specs/changes after archive.
- Mutation pilot: mutmut 3.8.0 generated 24 mutants for `whilly/core/gates.py`; 20 were killed and 4 survived. Each survivor is classified in `docs/testing/mutation-testing.md`; no unmeasured score threshold was introduced.
- Lock and patch checks: `uv lock --check --offline --python 3.12` and `git diff --check` passed.

The first plain `make lint` invocation selected the host's Python 3.8, where Ruff was not installed, and exited 2. Re-running the unchanged Make target with `.venv/bin` first in `PATH` used Python 3.12 and passed. This is retained as environment evidence, not reported as a source failure.

## Live-canary decision and later evidence

At the time of this recovery run, the bounded real-model canary was not
started. Docker had no reachable daemon, `pg_isready` reported no response,
and the installed client package did not include the `postgres` server
executable. The recovery checkout had no trusted private product registry.
That earlier blocker is historical; it is not the current canary result.

A later disposable first-use canary completed one documentation-only task with
one worker. It used three model calls (planner, worker, reviewer), completed in
60.386 seconds, and recorded cost as unknown. Task, test, lint, and architecture
verification passed. The feature reached `review`, and publication stopped
with `publication_unavailable` before transport provisioning. Stop returned
`drained: true`. The redacted receipt and operator procedure are in
[`first-use.md`](first-use.md).

The independent reviewer model returned `approve` from the exact
coordinator-produced diff and byte-for-byte host verification evidence, but
its explanation states that direct workspace inspection was blocked by the
execution sandbox. This proves an independent evidence review, not independent
workspace inspection. Do not claim production readiness, human-quality review,
or hard Codex cost enforcement from this canary. Before relying on Swarm for
non-disposable engineering changes, confirm the reviewer can inspect the
candidate under the actual configured sandbox.
