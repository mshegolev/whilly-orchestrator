# Runtime Tasks

Implementation and local acceptance are verified. No production or merge claim.

## Complete or evidenced

- [x] Add dual-engine adapters and task/role engine selection.
- [x] Add durable engine-call reservations with nullable unknown costs.
- [x] Add task-local mailbox IPC with coordinator validation and receipts.
- [x] Make accepted evidence and `DONE` atomic through the completion callback.
- [x] Add same-repository predecessor-head selection and divergent-head
  `integration_required` handling.
- [x] Add coordinator-owned Codex commit helper with branch and literal-file
  guards, normal hooks, and post-commit head verification.
- [x] Correct the seeded-admin session-email lookup and retain the existing
  permission guard with fail-closed uniqueness behavior.

## Live validation

- [x] Re-run the Codex consumer canary after the `.git` metadata/network
  failure; confirm the worker edits only its worktree and mailbox.
- [x] Confirm coordinator commit, combined project-plus-task verification,
  independent review, and atomic acceptance on the rerun.
- [x] Complete runtime checks and record live evidence; preserve failed attempts.
- [x] Reproduce and fix PGID persistence failure and unknown-identity recovery;
  independently re-review the fixes.

## Release gate

- [x] Release the documentation and OpenSpec change after validation evidence
  is complete.
- [x] Archive the verified implementation change and update canonical specs.

Live synthetic two-engine and real browser acceptance passed. No production
evaluation, automatic merge, or deployment was performed.
