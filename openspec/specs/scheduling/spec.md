## Purpose

The scheduling capability governs Whilly's continuous, JQL-driven intake of Jira
issues: it defines how scheduler rules describe recurring poll cycles, how the
asynchronous `SchedulerWorker` evaluates due rules and executes their JQL against
Jira, how discovered issues are deduplicated before dispatch, how event-driven
webhooks complement polling, how external calls are rate-limited and retried,
how rules and poll cycles persist to the Postgres-backed scheduler repository,
and the `whilly scheduler` CLI surface for managing rules. This capability
covers the `whilly/scheduler/*` subsystem, `whilly/core/scheduler.py` DAG
primitives, and `whilly/cli/scheduler.py`; it does not govern the per-task
orchestration loop, which is defined by the orchestration-loop and
task-model-fsm capabilities.
## Requirements
### Requirement: Scheduler rule definition
The system SHALL model a scheduler rule as an immutable `SchedulerRule` carrying an `id`, `name`, `jira_project_key`, `jql_filter`, `enabled` flag, `poll_interval_seconds`, `max_results_per_poll`, `deduplication_fields`, and per-rule `plan_config` and `custom_metadata`.

#### Scenario: Rule loaded with defaults
- **WHEN** a rule object is constructed without overriding optional fields
- **THEN** `enabled` SHALL default to `True`, `poll_interval_seconds` SHALL default to 300, `max_results_per_poll` SHALL default to 50, and `deduplication_fields` SHALL default to `("key", "summary")`

#### Scenario: Rule serialized for persistence
- **WHEN** `SchedulerRule.to_dict` is called
- **THEN** the system SHALL return a JSON-serializable mapping of every rule field
- **AND** `deduplication_fields` SHALL be emitted as a list and timestamp fields as ISO-8601 strings or `None`

### Requirement: Scheduler configuration loading and validation
The system SHALL load scheduler rules from a JSON or TOML configuration file via `load_scheduler_config`, rejecting any file whose suffix, structure, or required rule fields are invalid by raising `SchedulerConfigError`.

#### Scenario: Valid config produces rules
- **WHEN** `load_scheduler_config` reads a file whose top-level object contains a `rules` list of well-formed entries
- **THEN** the system SHALL return one `SchedulerRule` per entry

#### Scenario: Missing required field rejected
- **WHEN** a rule entry omits `id`, `name`, `jira_project_key`, or `jql_filter`, or supplies a non-positive `poll_interval_seconds` or `max_results_per_poll`
- **THEN** the system SHALL raise `SchedulerConfigError` identifying the offending source

#### Scenario: Unsupported file type rejected
- **WHEN** the configuration path has a suffix other than `.json`, `.toml`, or `.tml`
- **THEN** the system SHALL raise `SchedulerConfigError` reporting that a JSON or TOML config was expected

### Requirement: Worker due-rule selection
The system SHALL run an asynchronous `SchedulerWorker` that, on each iteration within its configured duration, selects only the rules whose `poll_interval_seconds` have elapsed since their last poll and polls them concurrently.

#### Scenario: Disabled rules excluded at construction
- **WHEN** a `SchedulerWorker` is constructed from a list of rules
- **THEN** rules whose `enabled` flag is `False` SHALL be filtered out and never polled

#### Scenario: Only due rules are polled
- **WHEN** the worker evaluates an iteration and a rule has been polled more recently than its `poll_interval_seconds`
- **THEN** that rule SHALL be excluded from the current gather batch
- **AND** a rule never polled before SHALL be treated as due

#### Scenario: Due rules polled concurrently
- **WHEN** more than one rule is due on the same iteration
- **THEN** the worker SHALL dispatch their poll cycles concurrently rather than strictly sequentially

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

### Requirement: JQL execution against Jira
The system SHALL execute a rule's JQL filter against Jira through `execute_jql`, returning the matching issue dicts and raising `JQLExecutionError` when credentials, transport, or the response shape are invalid.

#### Scenario: Query returns issues
- **WHEN** `execute_jql` runs with valid credentials and the Jira response contains an `issues` list
- **THEN** the system SHALL return that list of issue dicts

#### Scenario: Credential or transport failure raised
- **WHEN** Jira credentials cannot be loaded or the search request fails
- **THEN** the system SHALL raise `JQLExecutionError`

#### Scenario: Syntax validation via dry run
- **WHEN** `validate_jql` is called for a candidate filter
- **THEN** the system SHALL return `True` when a minimal dry-run query succeeds and `False` when it raises `JQLExecutionError`

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

### Requirement: Webhook event handling
The system SHALL parse Jira webhook payloads into `JiraWebhookEvent` objects and dispatch them through a `WebhookEventHandler` to callbacks registered per event type, enabling event-driven intake alongside scheduled polling.

#### Scenario: Registered callback invoked
- **WHEN** `WebhookEventHandler.handle_event` receives a payload whose `webhookEvent` matches a registered event type
- **THEN** each callback registered for that event type SHALL be invoked with the parsed event, awaiting any awaitable result

#### Scenario: Invalid payload rejected
- **WHEN** a webhook payload lacks an issue key or project key
- **THEN** parsing SHALL raise `ValueError` and the handler SHALL log the error without raising to the caller

#### Scenario: Callback error isolated
- **WHEN** one registered callback raises during dispatch
- **THEN** the handler SHALL log that callback's error and continue invoking the remaining callbacks

### Requirement: Rate limiting and backoff of external calls
The system SHALL limit scheduled external calls through `RateLimiter` retry-with-backoff and `PollRateLimiter` poll pacing, capping delays at the configured maximum and respecting per-minute request limits.

#### Scenario: Retry with bounded backoff
- **WHEN** a call wrapped by `RateLimiter.call_with_retry` fails fewer than `max_retries` times
- **THEN** the limiter SHALL retry after a strategy-derived delay that never exceeds `max_delay`
- **AND** SHALL re-raise the final exception once retries are exhausted

#### Scenario: Minimum poll interval enforced
- **WHEN** `PollRateLimiter.wait_until_ready` is called before `min_interval_seconds` have elapsed since the previous poll
- **THEN** the limiter SHALL wait until the minimum interval has passed before allowing the poll

#### Scenario: Per-minute cap enforced
- **WHEN** the number of polls recorded in the trailing minute reaches `max_requests_per_minute`
- **THEN** the limiter SHALL wait until an earlier poll ages out of the window before allowing another poll

### Requirement: Postgres-backed scheduler repository
The system SHALL persist scheduler rules and poll cycles through the `SchedulerRepository` interface, with `SQLSchedulerRepository` providing the Postgres-backed implementation consistent with the v4 SQL state layer and `InMemorySchedulerRepository` serving development and tests.

#### Scenario: Rule persisted and retrieved
- **WHEN** a rule is created via `SQLSchedulerRepository.create_rule` and later fetched by id
- **THEN** the repository SHALL return an equivalent `SchedulerRule`, serializing `deduplication_fields` as JSON for storage

#### Scenario: Duplicate rule creation rejected
- **WHEN** a rule is created whose id already exists in the backing store
- **THEN** the repository SHALL raise `SchedulerRepositoryError` after rolling back the transaction

#### Scenario: Poll cycle recorded with assigned id
- **WHEN** a completed `SchedulerPollCycle` is recorded via `record_poll_cycle`
- **THEN** the repository SHALL persist the cycle and return its assigned identifier

#### Scenario: Last successful poll queried
- **WHEN** `get_last_successful_poll` is called for a rule id
- **THEN** the repository SHALL return the most recent cycle whose `poll_status` is `completed`, or `None` if the rule has never polled successfully

### Requirement: Scheduler CLI surface
The system SHALL expose a `whilly scheduler` CLI providing `run`, `validate`, `list`, `status`, `enable`, and `disable` actions for managing and operating scheduler rules.

#### Scenario: Run loads config and starts the worker
- **WHEN** `whilly scheduler run <config> --duration <n>` is invoked with an existing config
- **THEN** the CLI SHALL load the rules, construct a `SchedulerWorker`, and run it for the requested duration, returning exit code 0 on clean completion

#### Scenario: Validate reports config validity
- **WHEN** `whilly scheduler validate <config>` is invoked
- **THEN** the CLI SHALL return exit code 0 and report the rule count when the config is valid, and a non-zero exit code with an error message when it is invalid

#### Scenario: Enable requires database
- **WHEN** `whilly scheduler enable <rule_id>` is invoked without `WHILLY_DATABASE_URL` set
- **THEN** the CLI SHALL not toggle any rule and SHALL return a non-zero exit code reporting that the database URL is required

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

