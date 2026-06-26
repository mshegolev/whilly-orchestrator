## MODIFIED Requirements

### Requirement: Poll cycle execution and recording
The system SHALL execute each poll cycle as a `SchedulerPollCycle` that runs the rule's JQL, deduplicates the results, optionally invokes the issues-found callback, and records a terminal `poll_status` of `completed` or `failed`. When the issues-found callback returns a list of plan ids, the cycle SHALL record that list in `created_plans`.

#### Scenario: Successful cycle marked completed
- **WHEN** a rule's JQL executes and deduplication succeeds without raising
- **THEN** the cycle SHALL record `total_issues_found`, the deduplicated issues, the duplicate count, and a `poll_status` of `completed`

#### Scenario: JQL failure marked failed
- **WHEN** JQL execution raises a `JQLExecutionError` during a cycle
- **THEN** the cycle SHALL set `poll_status` to `failed` and capture the error message rather than aborting the worker

#### Scenario: Interval respected after failure
- **WHEN** a poll cycle completes or fails for a rule
- **THEN** the worker SHALL update that rule's last-polled timestamp regardless of outcome so the next poll honors the configured interval

#### Scenario: Created plan ids recorded from callback
- **WHEN** the issues-found callback returns a list of plan ids for a completed cycle
- **THEN** the cycle SHALL store that list in `created_plans`
- **AND** a callback that returns `None` SHALL leave `created_plans` empty

### Requirement: Issue deduplication
The system SHALL deduplicate discovered issues by hashing the configured `deduplication_fields`, suppressing any issue whose hash has already been seen from the unique set. When a configured field is absent at the top level of an issue dict, the system SHALL resolve it from the issue's nested `fields` object so that raw Jira search results (whose `summary` and other attributes are nested under `fields`) hash correctly under the default `("key", "summary")`.

#### Scenario: Duplicate issues suppressed
- **WHEN** `deduplicate_issues` processes two issues whose configured fields hash identically
- **THEN** only the first SHALL appear in the unique list and the later one's key SHALL appear in the duplicate-keys list

#### Scenario: Pre-seen hashes honored
- **WHEN** `deduplicate_issues` is given a non-empty `seen_hashes` set
- **THEN** any issue matching a pre-seen hash SHALL be treated as a duplicate and excluded from the unique list

#### Scenario: Unhashable issue skipped
- **WHEN** an issue is missing a field required for hashing at both the top level and within its nested `fields` object
- **THEN** that issue SHALL be skipped without aborting deduplication of the remaining issues

#### Scenario: Nested Jira field resolved
- **WHEN** an issue exposes a configured hash field (such as `summary`) only inside its nested `fields` object, as raw `execute_jql` results do
- **THEN** the system SHALL hash the nested value rather than treating the issue as unhashable

## ADDED Requirements

### Requirement: Scheduler issue dispatch to claimable tasks
The system SHALL convert each unique issue discovered by a scheduler poll into a `PENDING` `Task` persisted under a single plan per rule when a Postgres database URL (`WHILLY_DATABASE_URL`) is configured, so that workers can claim scheduler-discovered work. The plan id SHALL be the rule's `custom_metadata.plan_id` when present, otherwise the rule `id`. Each task id SHALL be `JIRA-<issue-key>`, carrying the issue's sanitized description, acceptance criteria, test steps, and a priority mapped from the Jira priority. Persistence SHALL be idempotent: re-dispatching an already-persisted issue SHALL NOT create a duplicate task. When no database URL is configured the system SHALL log the discovered issues and persist nothing.

#### Scenario: Discovered issue persisted as a pending task
- **WHEN** a poll discovers a unique issue with key `K` and a database URL is configured
- **THEN** the system SHALL persist a `Task` with id `JIRA-K` and status `PENDING` under the rule's plan

#### Scenario: Re-dispatch is idempotent
- **WHEN** the same issue key is dispatched in a later poll cycle for the same rule
- **THEN** the system SHALL NOT create a second task for that key (the import uses `ON CONFLICT (id) DO NOTHING`)

#### Scenario: Repo target resolved from rule metadata
- **WHEN** the rule's `custom_metadata` carries a `repo_target` of the form `provider:full_name`
- **THEN** the persisted plan SHALL include that repo target and each task SHALL reference its id

#### Scenario: No database configured logs only
- **WHEN** issues are discovered and `WHILLY_DATABASE_URL` is not set
- **THEN** the system SHALL log the discovered issues and SHALL NOT attempt persistence

#### Scenario: Plan id derived from rule
- **WHEN** a rule defines `custom_metadata.plan_id`
- **THEN** the dispatched tasks SHALL be persisted under that plan id rather than the rule id
