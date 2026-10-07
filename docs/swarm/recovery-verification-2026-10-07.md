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

## Live-canary decision

The bounded real-model canary was not started. Publication was verified fail-closed for the normal API composition: `ProductWorkflow` is constructed without a publication backend, and `publish_feature()` returns `publication_unavailable` before constructing transport. However, no disposable PostgreSQL server was available: Docker had no reachable daemon, `pg_isready` reported no response, and the installed client package did not include the `postgres` server executable. No trusted private product registry was present in the recovery checkout either.

The named activation blocker is `live_canary_postgres_unavailable` (with registry/toolchain configuration still required after PostgreSQL is supplied). A unit or fake-adapter pass is not substituted for real provider evidence. The next operator action is to provision an ephemeral PostgreSQL instance and a disposable registry/toolchain, then follow the one-feature/one-worker procedure in the recovery manifest. Do not weaken sandbox, approval, publication, or budget gates to make that canary run.
