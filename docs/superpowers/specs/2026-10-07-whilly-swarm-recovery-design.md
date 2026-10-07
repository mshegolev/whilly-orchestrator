# Whilly Swarm Recovery — BMAD Design

## Product brief

Whilly must be safe and useful enough to run a bounded product-development swarm from the product cockpit. The recovery effort is complete when current `main`, rather than any abandoned checkout, contains the best verified implementation; operators can distinguish a denied launch from a crashed coordinator; and a repeatable mutation pilot measures whether the pure decision gates are meaningfully tested.

The primary user is an engineer who asks Whilly to plan and implement one narrowly scoped change. Their success signal is an honest, inspectable result: approved plan, bounded worker count, named blockers, verification evidence, and no hidden fallback outside the guarded executor.

## Evidence and scope decisions

Read-only comparison of the abandoned swarm checkout snapshots against `origin/main` found no UI capability that should be recovered. Current `main` already contains the legacy session UI plus the newer product cockpit, approval digest, stop semantics, Gantt, responsive layout, and theme/navigation integration.

Current `main` also already contains the Clean Architecture verification gate and semantic-drift checker. The old JEV and automatic drift-remediation prototypes have no approved capability requirement; JEV is explicitly optional in the learning design. They remain historical evidence, not recovery inputs.

The launcher inventory confirms that discussion, planning, escalation, worker, review, verification, candidate Git, BMAD host scripts, API/CLI run, resume, and retry enter `GuardedExecutor`. Separate legacy agent commands and read-only metadata/auth helpers are not part of the swarm candidate boundary. The concrete defect is loss of a specific `ExecutionBlocked` reason at the coordinator catch-all.

## Architecture

1. Keep `GuardedExecutor` as the single swarm execution boundary.
2. At the coordinator boundary, translate `ExecutionBlocked.reason` into the durable task outcome instead of `coordinator_error`; retain the exception text as bounded detail.
3. Configure mutmut 3.8.x in the Python 3.12 dev environment for one deterministic pure module, `whilly/core/gates.py`, and its dedicated unit tests. The first run is evidence collection, not a quality threshold.
4. Record launcher inventory and recovery decisions in repository documentation and complete the active guarded-execution OpenSpec checklist only after tests and canaries pass.
5. Run the existing fake/offline swarm tests before any bounded real-model canary. A live canary may use at most one feature, one worker, one planner/reviewer sequence, and the configured budget/time ceilings. Because successful product execution invokes `publish_feature()` automatically, first prove that guarded publication fails closed before constructing any transport; otherwise do not run the canary.

## Acceptance criteria

- A synthetic executor denial persists its specific machine-readable reason and never becomes `coordinator_error`.
- Existing success, task-failure, cancellation, and claim-loss behavior remains unchanged.
- `uv run mutmut run` works from a Python 3.12 project environment for the selected gate module, and its results are recorded with killed, survived, timeout, suspicious, and error counts.
- No threshold is introduced without measured evidence.
- The complete launcher inventory is reviewed, with out-of-bound legacy/helper subprocesses explicitly named rather than hidden.
- OpenSpec strict validation, focused tests, full unit tests, lint, import-linter, fake canary, and bounded live canary either pass or produce an explicit blocker.

## Risks and controls

- Mutation tools copy source and can leave `mutants/`; keep the scope narrow and ignore generated state.
- Full-suite mutation would invoke unrelated process/DB fixtures; select only the pure gate test.
- A real model can consume money or expose environment data; use existing guarded provisioning, one worker, a confirmed fail-closed publication path, and stop on the first infrastructure/auth blocker.
- Broken old worktrees are read-only evidence. Never repair them in place or copy them wholesale.
