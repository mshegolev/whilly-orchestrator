## Purpose

The github-integration capability governs Whilly's entire GitHub subsystem —
the PR sink, GitHub Projects v2 sync, issue→plan conversion and Forge intake,
the issue/PR source adapters, the pluggable board-workflow engine, and the
one-shot CI poll adapter. This capability covers the auth contract every `gh`
CLI invocation shares, the read-only versus mutating boundary that separates
state reads from GitHub writes, and the structured-failure discipline that
keeps GitHub transport errors from crashing the orchestration loop. It is
specced at subsystem-contract altitude: it states what each sub-surface
guarantees, while per-module accounting lives in the coverage matrix.

## Requirements

### Requirement: Centralised gh CLI auth resolution
Every `gh` CLI subprocess across the GitHub subsystem SHALL build its
environment through `whilly.gh_utils.gh_subprocess_env`, which resolves the
token by first match: `WHILLY_GH_TOKEN` is copied into `GITHUB_TOKEN` (and
`GH_TOKEN` removed); else `WHILLY_GH_PREFER_KEYRING` strips ambient
`GITHUB_TOKEN`/`GH_TOKEN` to force keyring auth; else `[github].token` from
`whilly.toml` is resolved (optionally via `whilly.secrets`); otherwise ambient
`GITHUB_TOKEN`/`GH_TOKEN` pass through unchanged.

#### Scenario: WHILLY_GH_TOKEN overrides the ambient token
- **WHEN** `WHILLY_GH_TOKEN` is set in the environment and `gh_subprocess_env`
  is called to prepare a subprocess env
- **THEN** the returned env SHALL set `GITHUB_TOKEN` to the `WHILLY_GH_TOKEN`
  value
- **AND** the returned env SHALL NOT contain `GH_TOKEN`

#### Scenario: Prefer-keyring strips ambient tokens
- **WHEN** `WHILLY_GH_TOKEN` is unset and `WHILLY_GH_PREFER_KEYRING` is truthy
- **THEN** the returned env SHALL have both `GITHUB_TOKEN` and `GH_TOKEN`
  removed so `gh` falls back to its keyring auth

#### Scenario: Ambient passthrough is the default
- **WHEN** neither `WHILLY_GH_TOKEN` nor `WHILLY_GH_PREFER_KEYRING` is set and
  no `[github].token` is configured in `whilly.toml`
- **THEN** the returned env SHALL leave the ambient `GITHUB_TOKEN`/`GH_TOKEN`
  values unchanged

### Requirement: Read-only GitHub state reads
The read sub-surfaces SHALL only read GitHub state and MUST NOT create, edit,
or mutate any GitHub resource while reading. These read surfaces are the issue
sources (`gh issue list`/`gh issue view`), `github_converter.fetch_github_issues`,
GitHub Project item fetch (`fetch_project_items`), and the CI poll adapter
(`gh pr view`).

#### Scenario: Issue source fetch reads without writing
- **WHEN** `sources.github_issues.fetch_github_issues` runs against a repo and
  label
- **THEN** the system SHALL invoke `gh issue list` to read open issues only
- **AND** the system SHALL NOT create, edit, or close any GitHub issue as part
  of the fetch

#### Scenario: Project item fetch reads the board only
- **WHEN** `GitHubProjectsConverter.fetch_project_items` queries a Project v2
  board via the GraphQL read query
- **THEN** the system SHALL return parsed `ProjectItem` records without
  mutating any project item, status, or issue

### Requirement: Idempotent issue→plan merge with secret detection
Fetching GitHub issues into a plan SHALL merge issues idempotently by issue id
(`GH-{number}`) — re-fetching refreshes mutable task fields while preserving
each task's status, adds new tasks as `pending`, marks issues that vanished
from the source as `skipped` — and MUST scan issue bodies for secret patterns
and surface matches as warnings.

#### Scenario: Re-fetch preserves status and refreshes fields
- **WHEN** `merge_into_plan` runs over a plan that already contains a task
  whose id matches a fetched issue
- **THEN** the system SHALL preserve that task's existing status
- **AND** the system SHALL refresh its description, priority, key_files,
  acceptance_criteria, test_steps, prd_requirement, and dependencies

#### Scenario: Externally closed issue is skipped
- **WHEN** a task with an id starting `GH-` is still `pending` or `in_progress`
  but its issue is absent from the freshly fetched set
- **THEN** the system SHALL set that task's status to `skipped`

#### Scenario: Secret-like body content is flagged
- **WHEN** an issue body matches one of the shared secret patterns during
  conversion
- **THEN** the system SHALL record a secret warning for that task in the fetch
  stats rather than silently importing the body

### Requirement: Deterministic issue→task conversion and idempotent Forge intake
The system SHALL deterministically map a GitHub issue to a Whilly Task or plan
via `convert_issues_to_tasks` / `generate_tasks_from_github`, and `whilly forge
intake owner/repo/<N>` MUST be idempotent: a re-run for an already-ingested
issue ref returns the existing plan id and exits 0 without re-invoking the PRD
pipeline (no Claude tokens) and without re-flipping the issue label.

#### Scenario: Conversion is deterministic and skips closed issues
- **WHEN** `convert_issues_to_tasks` runs over a list of fetched issues
- **THEN** the system SHALL emit a Task only for issues whose state is `OPEN`
- **AND** the system SHALL derive each Task id deterministically from the issue
  number and title

#### Scenario: Re-running forge intake reuses the existing plan
- **WHEN** `whilly forge intake` is invoked for an `owner/repo/<N>` ref already
  present in `plans.github_issue_ref`
- **THEN** the system SHALL print the existing plan id and exit 0
- **AND** the system SHALL NOT invoke the PRD pipeline or burn Claude tokens
- **AND** the system SHALL NOT call `gh issue edit` to re-flip the label

#### Scenario: Label flip is the last intake step
- **WHEN** a fresh forge intake inserts a new plan row successfully
- **THEN** the system SHALL flip the issue label from `whilly-pending` to
  `whilly-in-progress` only after the database insert has committed

### Requirement: Mutating boundary confined to PR, project, and issue writes
The only GitHub-mutating operations SHALL be PR creation (`open_pr_for_task`:
force-with-lease push then `gh pr create` after a rollback preflight), project
status writes (`sync_status_changes` / `_update_project_item_status`), issue
creation (`_create_github_issue`), and the forge label flip; each mutating path
MUST surface a structured failure (a `PRResult.failure_mode`, a `False` return,
or a logged warning) rather than crashing the orchestration loop.

#### Scenario: PR sink never raises into the loop
- **WHEN** `open_pr_for_task` runs and the rollback preflight, the
  force-with-lease push, or `gh pr create` fails
- **THEN** the system SHALL return a `PRResult` with `ok=False` and a populated
  `failure_mode`
- **AND** the system SHALL NOT raise an exception into the calling worker loop

#### Scenario: Existing PR is treated as success
- **WHEN** `gh pr create` reports that a PR already exists for the head branch
- **THEN** the system SHALL look up the existing PR and return a `PRResult` with
  `ok=True`

#### Scenario: Project status write reports failure without crashing
- **WHEN** `sync_status_changes` cannot locate the project item or the GraphQL
  status mutation fails
- **THEN** the system SHALL return `False` rather than propagating an exception

### Requirement: Pluggable board-workflow contract
The workflow engine SHALL expose a `BoardSink` Protocol plus the `LifecycleEvent`
core vocabulary and `WorkflowMapping`, resolved through `get_board` over the
`available_boards` registry, so status sync is driven by a stable adapter
contract; `BoardSink.move_item` MUST NOT raise on transport errors and SHALL
return `False` on any failure.

#### Scenario: Board factory resolves a registered adapter
- **WHEN** `get_board` is called with a name present in `available_boards`
- **THEN** the system SHALL return a `BoardSink` instance bound to the supplied
  kwargs

#### Scenario: Unknown board name is rejected
- **WHEN** `get_board` is called with a name not in the registry
- **THEN** the system SHALL raise `ValueError` naming the available boards

#### Scenario: move_item never raises on transport failure
- **WHEN** a `BoardSink.move_item` call encounters a missing item, permission
  denial, or network error
- **THEN** the implementation SHALL return `False` rather than raising

### Requirement: CI poll adapter returns explicit evidence
The one-shot GitHub CI poll adapter (`GitHubCIPollAdapter`) SHALL run a single
bounded read-only `gh pr view` status probe and return an explicit
`CIPollResult` — distinguishing unauthenticated, unavailable, timed-out, and
status-rollup outcomes — and MUST NOT raise on probe failure.

#### Scenario: Authentication failure yields an unauthenticated result
- **WHEN** the `gh pr view` probe exits non-zero with auth/login-related output
- **THEN** the adapter SHALL return a `CIPollResult` flagged unauthenticated
  with reason `github_authentication_required`

#### Scenario: Probe timeout yields a timed-out result
- **WHEN** the bounded probe exceeds its timeout
- **THEN** the adapter SHALL return a `CIPollResult` flagged timed out rather
  than raising

#### Scenario: Unparseable target yields an unavailable result
- **WHEN** the poll target cannot be parsed into owner/repo/PR-number
- **THEN** the adapter SHALL return a `CIPollResult` flagged unavailable with a
  reason and SHALL NOT invoke `gh`
