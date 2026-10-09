# Approval-bound task paths for Swarm

## Context and problem

Whilly currently rejects every candidate that changes a path matched by
`DEFAULT_PROTECTED_PATHS`. This is correct for control-plane files such as CI,
dependency locks, agent instructions, and verification configuration, but the
same immutable list also contains `tests/**` and `architecture/**`.

The first practical IC Swarm run exposed the contradiction. The owner approved
a TDD task whose acceptance criteria required new tests. A Codex worker finished
the task, committed the candidate, and returned structured evidence, but the
coordinator stopped before host verification with `policy_changed` because the
candidate contained the approved test files. Removing test protection globally
would let a worker weaken acceptance evidence without a distinct owner decision.

The desired outcome is that an owner can approve exact test and architecture
changes for a task while undeclared protected changes remain fail-closed. The
approval must be machine-verifiable and bound to the same immutable revision as
the task description, base SHA, and verification policy.

## Selected design

Add an optional `allowed_paths` field to each Swarm plan task. It contains a
non-empty, bounded list of repository-relative path patterns describing the
candidate files that the owner is approving for that task. The normalized list
is persisted inside the plan revision and therefore participates in the
existing revision/application approval boundary.

This design separates protected paths into two classes:

1. **Hard-protected paths** cannot be authorized by a task plan. They include
   CI definitions, dependency manifests and locks, agent instructions, security
   and verification-policy configuration, and repository governance files.
2. **Approval-protected paths** remain denied by default but may be changed when
   the exact path is covered by the applied task's `allowed_paths`. Initially
   this class contains `tests/**`, `test/**`, `architecture/**`, and
   `**/architecture/**`.

Ordinary source files are also constrained by `allowed_paths` when the field is
present. This makes the field a positive task scope, not merely an exemption
from the protected-path gate. Plans without the field retain current behavior
for compatibility: ordinary files are allowed and every protected path is
denied.

## Path contract

`allowed_paths` follows the existing safe relative-pattern rules already used
by product registry `allowed_paths`:

- paths are repository-relative and use `/` separators;
- absolute paths, `..`, home expansion, drive prefixes, NULs, and empty entries
  are invalid;
- the list is deduplicated while preserving order;
- the number of entries and entry length are bounded;
- matching uses normalized candidate paths and does not follow filesystem
  symlinks;
- an empty supplied list is invalid; omission means legacy behavior.

The planner prompt describes the field and requires it for tasks that modify
tests or architecture evidence. Validation rejects a plan whose
`allowed_paths` intersects hard-protected paths, so owner approval cannot turn a
worker-authored plan into authority to rewrite its own policy or toolchain.

## Execution flow

For every candidate, the coordinator obtains the changed-file list from the
committed candidate and evaluates it before verification:

1. A hard-protected change yields `policy_changed` regardless of
   `allowed_paths`.
2. When `allowed_paths` is present, any changed file outside it yields
   `path_not_approved` with the offending paths.
3. An approval-protected change not covered by `allowed_paths` yields
   `policy_changed`.
4. Covered test or architecture changes proceed to baseline/candidate host
   verification and independent review. Coverage is permission to evaluate the
   candidate, never proof that the change is correct.

The coordinator records normalized `allowed_paths` in task context and in the
attempt result/report. Resume and rerun reuse the applied revision's stored
scope; they cannot widen it. Any scope change requires a new revision and fresh
owner approval.

## Compatibility and existing product policy

The task field complements, and never widens, product registry policy. For a
Product Workflow task, the effective scope is the intersection of:

- product/project policy `allowed_paths`; and
- task `allowed_paths`, when present.

If either layer rejects a path, execution fails closed. Legacy Swarm sessions
gain task-level scoping without needing a product registry. Existing plans that
omit the field continue to parse and execute with their current restrictions.

## Error handling and evidence

The following outcomes are distinct and durable:

- `plan_invalid`: unsafe or malformed `allowed_paths`;
- `policy_changed`: hard-protected or unapproved approval-protected change;
- `path_not_approved`: an otherwise ordinary changed path exceeds the approved
  task scope;
- normal verification outcomes after the path gate passes.

Reports include the approved patterns, changed files, and rejected paths. They
must not collapse these outcomes into generic worker failure.

## Specification lifecycle

This changes runtime behavior and therefore requires an OpenSpec delta for the
`orchestration-loop` capability. Implementation follows propose → apply →
archive and updates the baseline only through `openspec archive`.

## Test strategy

TDD covers the contract at four levels:

1. Plan parsing and normalization accept safe relative patterns and reject
   traversal, absolute paths, hard-protected intersections, empty lists, and
   excessive input.
2. Pure path evaluation proves hard protection, approval protection, positive
   scoping, omission compatibility, and product/task intersection.
3. Coordinator integration proves an explicitly approved test file reaches
   host verification, while an undeclared sibling test, source file, CI file,
   or verification-policy file stops before verification.
4. Persistence/resume tests prove rerun cannot mutate the approved scope and a
   new revision is required to widen it.

After the implementation gates pass, the retained incident-signal candidate is
not merged directly. A new clean revision declares exact source, test, fixture,
architecture, and ignore-file paths, runs with one Codex worker, and must reach
coordinator verification plus independent review before integration.

## Non-goals

- Allowing arbitrary protected-file changes.
- Treating owner approval as verification or review.
- Recovering or auto-accepting partial/failed worker attempts.
- Changing publication, merge, deploy, or production-data authority.
- Removing existing sandbox, budget, base-SHA, or verification-policy bindings.
