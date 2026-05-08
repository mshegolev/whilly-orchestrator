# Phase 10: Rollback safety net - Validation

**Created:** 2026-05-08
**Phase Goal:** Add explicit backup-tag, branch-protection preflight, and smart rollback CLI behavior.
**Requirements:** ROLL-01, ROLL-02, ROLL-03

## Validation Scope

Phase 10 is complete only when rollback behavior is operator-visible, structured, and refusal-first. The verifier must not accept a hidden verifier reset helper as evidence for this phase.

## Requirement Map

| Requirement | Required Evidence | Minimum Automated Coverage |
|-------------|-------------------|----------------------------|
| ROLL-01: Operators can create backup tags before risky branch mutation. | `whilly rollback create` creates annotated `whilly/rollback/...` tags; `whilly rollback list` discovers them with target SHA and branch metadata. | Unit tests for deterministic tag naming/ref validation; integration CLI test creating and listing rollback points in a temp Git repo. |
| ROLL-02: Branch protection/preflight checks run before push, merge, or restore operations. | A structured preflight report includes operation, repo root, branch, HEAD, dirty state, upstream/protection status, backup-point status, blockers, and warnings; PR push path runs the preflight before `git push`. | Unit tests for report shape and blocker policy; PR sink tests for preflight failure returning `PRResult`/audit evidence instead of raising. |
| ROLL-03: Rollback restore is explicit, auditable, and confirmation-gated. | Restore refuses dirty worktrees by default, supports dry-run/JSON evidence, and requires an exact confirmation phrase before `git reset --hard`. | Unit and integration CLI tests for dirty refusal, dry-run report, non-TTY confirmation failure, exact confirmation success. |

## Required Test Files

- `tests/unit/test_rollback.py`
- `tests/integration/test_rollback_cli.py`
- `tests/test_github_pr_sink.py` or `tests/integration/test_post_complete_pr_hook.py`
- `tests/unit/test_compliance_report.py`

## Verification Commands

Run the smallest relevant tests after each implementation slice:

```bash
.venv/bin/python -m pytest -q tests/unit/test_rollback.py --maxfail=1
```

Run the phase integration set before verification:

```bash
.venv/bin/python -m pytest -q tests/unit/test_rollback.py tests/integration/test_rollback_cli.py tests/test_github_pr_sink.py tests/integration/test_post_complete_pr_hook.py tests/unit/test_compliance_report.py --maxfail=1
```

Run the phase gate:

```bash
make lint
.venv/bin/lint-imports --config .importlinter
```

Run `make test` when practical. If the full suite has unrelated failures, capture the exact failing test and prove Phase 10 coverage with the targeted commands above.

## Safety Invariants

- No rollback restore may call `git reset --hard` while `git status --porcelain=v1` reports tracked or untracked changes.
- No command may silently call `git clean`, `git stash`, `git checkout .`, or broad cleanup helpers.
- Missing or unavailable GitHub protection data must report `unknown`, not `unprotected`.
- Branch protection should block only when confirmed protected by the chosen local/optional remote evidence path.
- Rollback tags must be created without `-f`; existing rollback evidence must not be overwritten.
- Preflight output must redact credential-bearing remote URLs if any remote evidence is exposed.
- Compliance wording must stay scoped to rollback safety-net support, not automatic production recovery.

## Acceptance Checklist

- [ ] `whilly/rollback/` exists with typed models, Git adapter, and service logic outside `whilly.core`.
- [ ] `whilly/cli/rollback.py` exposes create/list/preflight/restore and JSON rendering.
- [ ] `whilly/cli/__init__.py` lazily dispatches `whilly rollback ...` without breaking legacy shims.
- [ ] Push mutation in the PR path runs rollback preflight before `git push`.
- [ ] Dirty restore refusal and exact confirmation are covered by tests.
- [ ] Compliance upgrades `Git rollback` only after CLI, preflight, restore, and push evidence exists.
- [ ] `make lint` and import-linter pass.
