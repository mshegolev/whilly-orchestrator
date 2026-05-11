# Phase 10: Rollback safety net - Context

**Gathered:** 2026-05-08
**Status:** Ready for planning

<domain>
## Phase Boundary

Phase 10 adds an explicit operator rollback safety net: backup-tag creation/listing, branch/preflight checks before risky Git mutation, and confirmation-gated restore behavior. It should improve rollback evidence and operator control without adding automatic destructive cleanup.

</domain>

<decisions>
## Implementation Decisions

### Operator Safety
- Rollback commands must be explicit operator actions, not hidden automatic cleanup.
- Restore operations must be confirmation-gated and must not silently destroy unrelated working-tree changes.
- If the worktree is dirty, rollback restore should stop with a clear diagnostic unless the operator provides an explicit force/confirm path defined by the CLI contract.
- The restore contract should prioritize exact prior state for the requested artifact or branch and avoid collateral edits.

### Backup Points And Preflight
- Operators should be able to create rollback points before risky branch mutation.
- Rollback point names should be deterministic and discoverable, using a clear Whilly-specific prefix instead of ad hoc tag names.
- Push, merge, and restore preflight checks should report branch, HEAD SHA, dirty worktree state, upstream/protection signals available locally, and whether a backup point exists.
- Preflight should be auditable through machine-readable output or structured data that tests can inspect.

### CLI Shape
- Add rollback behavior as a first-class Whilly CLI surface, for example `whilly rollback ...`.
- Prefer dry-run and list/status commands that are safe by default.
- Confirmation should be explicit for destructive restore behavior; no default `git reset --hard` style behavior.
- Existing v3/v4 CLI compatibility should not regress.

### Compliance And Documentation
- Compliance should distinguish general rollback safety-net support from older verifier-helper rollback behavior.
- Wording must not claim full autonomous rollback or automatic production recovery.
- Documentation updates should stay scoped to current-vs-target and command evidence if needed.

### Claude's Discretion
- The exact internal module layout is at Claude's discretion, but a small dedicated rollback module is preferred over burying Git safety logic in the top-level CLI dispatcher.

</decisions>

<code_context>
## Existing Code Insights

### Reusable Assets
- `whilly/cli/__init__.py` is the lazy top-level CLI dispatcher and should add `rollback` without pulling server dependencies into help paths.
- `whilly/compliance/__init__.py` currently reports `Git rollback` as partial because rollback is tied to verifier helper behavior.
- `whilly/cli/plan.py` has examples of confirmation-gated destructive commands, such as `plan reset`.
- `whilly/workspaces.py` has Git workspace safety checks that may inform dirty-worktree handling.

### Established Patterns
- Keep mutation commands explicit and operator-visible.
- Favor pure helper modules with subprocess boundaries contained outside `whilly.core`.
- Preserve Conventional Commit and focused-test workflow.
- Do not introduce hidden background repair loops; CI/repair is Phase 11.

### Integration Points
- CLI dispatcher: `whilly/cli/__init__.py`.
- New rollback command module: likely `whilly/cli/rollback.py` plus `whilly/rollback/` helpers.
- Compliance evidence: `whilly/compliance/__init__.py` and `tests/unit/test_compliance_report.py`.
- Tests: `tests/unit/test_rollback.py`, `tests/integration/test_rollback_cli.py`, and dispatcher tests if present.

</code_context>

<specifics>
## Specific Ideas

- Canonical backlog source: `docs/superpowers/plans/2026-05-07-doc-pack-alignment-roadmap.md`, Task 6.
- Roadmap success criteria:
  1. Operators can create and list rollback points before risky branch mutation.
  2. Push/merge/restore preflight checks are explicit and auditable.
  3. Restore operations are confirmation-gated and do not silently destroy unrelated work.
- User preference from prior rollback tasks: rollback/restore requests should restore the exact prior state of the requested artifact and must not touch unrelated working-tree changes.

</specifics>

<deferred>
## Deferred Ideas

- CI polling and bounded repair loops belong to Phase 11.
- Governance policy and semantic-memory scope belong to Phase 12.
- Full automatic production recovery remains out of current scope.

</deferred>
