# Whilly Swarm first-use design

## Product brief

Tomorrow-ready means an operator can follow one documented sequence to run one disposable
documentation-only feature through planning, digest-bound approval, one worker, host
verification, an independent reviewer model, and stop/drain. Discussion is an optional,
separately budgeted discovery step rather than part of this three-call execution receipt. The
run must use a real configured model, a disposable PostgreSQL database and Git repository,
finite call/time limits, and no publication remote. Monetary cost must be provider-capped when
the backend supports it; otherwise the receipt must say `unknown`. Fake acceptance remains
separate evidence.

## Current state

The product cockpit, coordinator, guarded executor, independent candidate worktrees, approval
bindings, named blockers, publication fail-closed behavior, and scoped mutation pilot already
exist. Recovery PR #327 is merged. Local commit `58f330a` adds a post-lint-and-unit mutation CI
job. The last live-canary attempt stopped before model execution because no disposable
PostgreSQL server or private disposable registry was ready.

## Architecture decision

Reuse the production product-workflow composition. Provision only disposable external inputs:
an ephemeral PostgreSQL cluster, one local Git repository, and a private generated registry.
Do not add a second launcher, bypass the API/domain services, weaken sandbox policy, or enable
publication. Prefer operational/runbook changes; change `whilly/` behavior only for a proven
gap and then use the full OpenSpec propose/apply/archive lifecycle.

The controller owns decisions, integration, commits, remote actions, and final acceptance.
Cheap agents perform isolated read-only audits first; any implementation later receives unique
file ownership and an independent reviewer.

## Acceptance criteria

1. Fresh fake/offline lifecycle, deny-probe, named-blocker, stop, and publication fail-closed
   checks pass.
2. Migrations complete against disposable PostgreSQL and the registry resolves one project and
   explicit planner/worker/reviewer profiles without secrets in tracked files.
3. One real-model canary completes one docs-only task with one worker and finite call/time
   limits; no
   remote, push, MR, deploy, or publication transport is configured.
4. A redacted receipt records identifiers, bindings, SHAs, profiles/models, calls, elapsed time,
   cost or `unknown`, verification, review, publication blocker, status, and drained state.
5. The narrow mutmut pilot is rerun and survivors are explicitly classified; CI executes it only
   after lint and unit tests and preserves failure status.
6. Ruff, formatting, import boundaries, unit tests, strict OpenSpec validation, patch checks,
   and relevant integration tests pass; the branch receives independent review. The reviewer
   must receive the exact coordinator-produced diff and verification evidence; direct shell
   inspection availability is recorded separately.
7. The feature branch is pushed, reviewed by GitHub CI, and merged only when all required checks
   are green.

## Risks, stop, and rollback

- Stop before a real call on database, sandbox deny-probe, authentication, toolchain, approval
  binding, publication, or budget failure. Never switch provider or relax a gate implicitly.
- Stop during execution on the first named blocker. Require `drained: true`; otherwise retain the
  worktree and evidence as pending shutdown.
- Disposable database and repository paths must be explicit and recoverable. Cleanup is optional
  after acceptance and must never target the source worktree or protected artifacts.
- Candidate commits are local evidence, not rollback. No remote is configured, so publication
  must fail before transport construction.

## READY / BLOCKED rule

Declare READY only with both fake and real receipts. Otherwise declare exactly one external
named blocker after safe local alternatives are exhausted, plus the minimum operator action that
removes it. BLOCKED is not readiness.
