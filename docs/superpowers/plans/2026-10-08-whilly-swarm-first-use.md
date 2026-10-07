# Whilly Swarm first-use implementation plan

**Base:** `58f330aba34904e9837fd39ba48133b0f834cf3d`

**Worktree / branch:** `/private/tmp/whilly-swarm-first-use` / `feat/swarm-first-use`

**Goal:** prove READY for one bounded real task or one precise external BLOCKED state, then ship
the mutation CI commit and first-use evidence through review and green CI.

## Work ledger

| ID | Task | Owner / files | Depends on | State / result |
| --- | --- | --- | --- | --- |
| A1 | PostgreSQL alternatives audit | `postgres_readiness`; read-only report | none | done: disposable container selected |
| A2 | Registry/toolchain/auth audit | `registry_readiness`; read-only report | none | done: local Codex path ready |
| A3 | Fake acceptance inventory | `offline_canary`; read-only report | none | done: stop/drain gap identified |
| A4 | Real canary and stop trace | `real_canary_path`; read-only report | none | done: bounded path and stop gates mapped |
| A5 | Mutation CI/PR audit | `mutation_pr_audit`; read-only report | none | done: gate preserves failure status |
| A6 | BMAD current-state gap audit | `bmad_gap_audit`; read-only report | none | done: evidence/runbook were the P0 gap |
| I1 | Provision disposable DB/repo/registry | controller; `/private/tmp` runtime artifacts only | A1,A2,A4 | done: 56 tables at migration head; sandbox ready |
| V1 | Fresh offline acceptance | controller; no source edits | A3 | done: 27 passed |
| C1 | One bounded real canary | controller; disposable artifacts only | I1,V1 | done: 3 calls, 1 accepted task, drained |
| M1 | Rerun mutation pilot | controller; no source edits expected | A5 | done: 20 killed, 4 classified survivors |
| D1 | First-use runbook and redacted receipt | docs implementer; `docs/swarm/first-use.md` and status docs | C1 | done: receipt redacted; limitation retained |
| R1 | Independent task and whole-branch review | fresh reviewers; read-only | D1,M1 | done: source READY after two review/fix cycles |
| G1 | Full verification, scoped commit, push, PR, CI, merge | controller | R1 | running |

## Controller rulings

- Preserve `graphify-out/`, `out/`, distributed audit artifacts, broken snapshots, and the
  `feat/swarm-learning-l4-l5` worktree.
- Carry local mutation commit `58f330a`; never rewrite or drop it.
- Use uv with Python 3.12. Keep credentials out of reports, child environments, and Git.
- Publication stays absent and must be proven fail-closed before the first model call.
- No source behavior change is authorized merely to make the canary pass.

## Execution gates

1. Integrate audit facts and choose the least invasive disposable PostgreSQL path.
2. Run migrations; build and validate a one-project registry and disposable repository.
3. Prove sandbox readiness, auth/toolchains, limits, approval bindings, and publication failure.
4. Run fresh fake/offline acceptance and inspect side effects/process state.
5. Run the real one-feature/one-worker canary; capture redacted evidence and stop/drain.
6. Rerun focused mutmut baseline; classify every result without inventing a score threshold.
7. Add only the runbook/evidence or proven fixes, each with tests and independent review.
8. Run full quality gates, stage explicit files, commit, push, open PR, wait for CI, merge green.

## Commit and blocker log

- Existing carried commit: `58f330a ci: run mutation checks after unit tests`.
- Active blockers at start: PostgreSQL unavailable; disposable registry not yet materialized.
- Disposable acceptance: 27 offline checks passed; real canary used one worker and three calls,
  accepted one task, stopped publication as unavailable, and drained cleanly. The reviewer
  approved from supplied verification evidence but reported that independent diff inspection
  was blocked by the execution sandbox; the runbook preserves this limitation.
- Mutation acceptance: 24 mutants, 20 killed, 4 previously classified survivors, with no
  timeout, suspicious, no-test, or harness-error outcomes.
- Final integration state: independent source review is READY; commit, remote CI, and merge
  remain in progress.
