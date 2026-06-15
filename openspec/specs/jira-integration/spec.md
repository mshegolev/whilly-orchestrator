## Purpose

The jira-integration capability governs how Whilly authenticates to a Jira
server, reads a single issue or work snapshot into the JSON plan, and drives a
Jira ticket through its workflow as the matching Whilly task changes status.
This capability covers credential resolution across the `[jira]` toml section,
environment variables, and an optional company-settings YAML file; the
read-only issue/snapshot fetch path; and the single mutating board-sync path
that POSTs a workflow transition. It draws an explicit boundary between
read-only collection and the one mutating surface so future changes are tracked
as deltas against an accurate baseline.

## Requirements

### Requirement: Layered Jira credential resolution
The system SHALL resolve Jira server URL and credentials by reading the
`[jira]` toml section first, then the `JIRA_SERVER_URL` / `JIRA_USERNAME` /
`JIRA_API_TOKEN` environment variables for any unset value, and finally an
optional company-settings YAML file enabled via `WHILLY_COMPANY_SETTINGS_FILE`
(or `COMPANY_SETTINGS_FILE`), via `JiraAuth.from_config`.

#### Scenario: Toml section supplies primary credentials
- **WHEN** `JiraAuth.from_config` is called and the `[jira]` toml section
  contains `server_url`, `username`, and a resolvable `token`
- **THEN** the system SHALL build a `JiraAuth` from those toml values
- **AND** the trailing slash SHALL be stripped from the resolved `server_url`

#### Scenario: Environment variables fill missing toml values
- **WHEN** a credential field is absent from the `[jira]` toml section but the
  corresponding `JIRA_SERVER_URL` / `JIRA_USERNAME` / `JIRA_API_TOKEN`
  environment variable is set
- **THEN** the system SHALL use the environment value for that field

#### Scenario: Company-settings YAML fills remaining gaps
- **WHEN** `WHILLY_COMPANY_SETTINGS_FILE` (or `COMPANY_SETTINGS_FILE`) names a
  readable flat-YAML file and a credential is still unset after toml and env
- **THEN** the system SHALL read `JIRA_URL` / `JIRA_USERNAME` / `JIRA_TOKEN`
  (and related keys) from that file to fill the remaining values

### Requirement: Basic versus bearer authentication scheme
The system SHALL support a `basic` auth scheme that sends an HTTP
`Authorization: Basic` header of base64-encoded `username:token` and a `bearer`
scheme that sends `Authorization: Bearer <token>` for Personal Access Tokens,
selecting the scheme from `auth_scheme` (toml), `JIRA_AUTH_SCHEME` /
`JIRA_TOKEN_TYPE` (env), or company settings, defaulting to `basic`.

#### Scenario: Basic scheme encodes user and token
- **WHEN** the resolved auth scheme is `basic` and a GET request is issued
- **THEN** the request SHALL carry an `Authorization` header of `Basic` plus the
  base64 encoding of `username:token`

#### Scenario: Bearer scheme sends a raw PAT
- **WHEN** `JIRA_AUTH_SCHEME` resolves to `bearer` (also accepting `pat`,
  `token`, or `personal_access_token`) and a GET request is issued
- **THEN** the request SHALL carry an `Authorization` header of `Bearer` plus
  the token, and the `username` field SHALL NOT be required to configure auth

### Requirement: TLS verification and CA configuration honored
The system SHALL honor `verify_ssl` and `ca_file` settings from toml, env
(`JIRA_VERIFY_SSL` / `JIRA_SSL_VERIFY`, `JIRA_CA_FILE` / `JIRA_SSL_CA_FILE`), or
company settings when opening HTTPS connections to Jira.

#### Scenario: SSL verification disabled by configuration
- **WHEN** the resolved `verify_ssl` value is false
- **THEN** the system SHALL use an unverified SSL context for Jira requests
- **AND** the system SHALL emit a warning that TLS verification is disabled

#### Scenario: Custom CA file used when verification is enabled
- **WHEN** `verify_ssl` is true and a non-empty `ca_file` is configured
- **THEN** the system SHALL build an SSL context loading that CA file for Jira
  requests

### Requirement: Unconfigured auth raises naming the missing fields
The system SHALL raise a `RuntimeError` that names the missing required fields
when `JiraAuth.from_config` cannot resolve `server_url`, `token`, and (for the
`basic` scheme) `username`.

#### Scenario: Missing token reported by name
- **WHEN** `JiraAuth.from_config` is called and `token` cannot be resolved from
  any configuration layer
- **THEN** the system SHALL raise a `RuntimeError` whose message lists `token`
  among the missing fields and points to the `[jira]` section and env vars

#### Scenario: Username required only under basic scheme
- **WHEN** the auth scheme is `basic` and `username` is unresolved
- **THEN** the system SHALL include `username` in the missing-fields list
- **AND** under the `bearer` scheme an unset `username` SHALL NOT be reported as
  missing

### Requirement: Read-only single-issue fetch into the plan
The system SHALL fetch a single Jira issue through `fetch_single_jira_issue`
using only HTTP GET requests, flatten the Atlassian Document Format description
to text, extract Acceptance and Test bullets, sanitize externally-sourced text,
and idempotently merge exactly one `JIRA-<key>` task into the target plan.

#### Scenario: Fetch performs only read requests
- **WHEN** `fetch_single_jira_issue` is called with a valid Jira key or browse
  URL
- **THEN** the system SHALL issue only GET requests to the Jira REST API
- **AND** the system SHALL NOT POST, PUT, or DELETE any Jira resource

#### Scenario: Issue merged idempotently as a JIRA-prefixed task
- **WHEN** `fetch_single_jira_issue` writes the issue into a plan
- **THEN** the resulting task id SHALL be `JIRA-<key>`
- **AND** re-fetching the same unchanged issue SHALL update rather than
  duplicate the existing task

#### Scenario: External text sanitized before storage
- **WHEN** the issue description, acceptance criteria, or test steps are stored
  on the task
- **THEN** the system SHALL pass that external text through the prompt
  sanitizer before writing it into the plan

### Requirement: Read-only work classification without network access
The system SHALL classify already-fetched Jira work, parse `/whilly` comment
commands, probe local code readiness, and build a work-metadata block in
`jira_work.py` without performing any Jira, GitLab, git, or database call.

#### Scenario: Classification uses caller-supplied data only
- **WHEN** `classify_jira_work` or `build_jira_work_metadata` is invoked with an
  issue mapping
- **THEN** the system SHALL derive the classification from the supplied data
  only and SHALL NOT open any network or database connection

#### Scenario: Readiness probe inspects a local checkout
- **WHEN** `probe_code_readiness` is called with a local repository path
- **THEN** the system SHALL return a verdict derived solely from files found on
  disk, with `blocked` when the path does not exist

### Requirement: Single mutating board-sync transition path
The system SHALL perform Jira ticket transitions only through
`JiraBoardClient.set_issue_status`, which MUST be the sole path that POSTs a
workflow transition, MUST match the target by `transitions[*].to.name`
case-insensitively, and MUST soft-fail by returning `False` (never raising into
the orchestrator loop) on any HTTP, auth, or unavailable-transition error.

#### Scenario: Matching transition is applied
- **WHEN** `set_issue_status` is called with a status name that matches an
  available transition's `to.name` ignoring case
- **THEN** the system SHALL POST that transition by id and return `True`

#### Scenario: Unavailable transition soft-fails
- **WHEN** no available transition's `to.name` matches the requested status name
- **THEN** the system SHALL return `False` without raising

#### Scenario: Transport error soft-fails
- **WHEN** fetching or posting a transition raises an HTTP, auth, or network
  error
- **THEN** the system SHALL log a warning and return `False` rather than
  propagating the exception into the loop

### Requirement: Overridable whilly-to-Jira status mapping
The system SHALL map Whilly internal statuses to target Jira status names using
`DEFAULT_JIRA_STATUS_MAPPING` and MUST allow per-project overrides through the
`[jira].status_mapping` section.

#### Scenario: Default mapping applied
- **WHEN** a Whilly task transitions to `in_progress` and no override is
  configured
- **THEN** the system SHALL target the default Jira status name `In Progress`

#### Scenario: Configured override replaces a default
- **WHEN** `[jira].status_mapping` supplies a different Jira status name for a
  Whilly status
- **THEN** the system SHALL use the overridden name for that status while
  retaining the defaults for unmapped statuses

### Requirement: Read-only versus mutating boundary
The system SHALL keep issue fetch, work-snapshot collection, classification,
readiness probing, and the import/intake/poll/smoke CLI flows strictly
read-only, and SHALL confine all Jira state mutation to the board-sync
transition path.

#### Scenario: Import and poll never mutate Jira
- **WHEN** an operator runs `whilly jira import`, `intake`, `poll`, or `smoke`
- **THEN** the system SHALL only read from Jira and write local plan files
- **AND** the system SHALL NOT transition or otherwise mutate the Jira ticket

#### Scenario: Status transitions are the only writes
- **WHEN** a Jira-sourced Whilly task changes status and board sync is enabled
- **THEN** the only Jira write the system performs SHALL be the workflow
  transition issued by `set_issue_status`

### Requirement: CLI credential gate before any fetch
The system SHALL enforce a credential gate in the `whilly jira` CLI layer that
verifies `server_url`, `token`, and (for basic auth) `username` are resolvable
before any Jira fetch runs, printing setup guidance or prompting interactively
when configuration is incomplete.

#### Scenario: Missing config blocks the fetch
- **WHEN** required Jira settings are missing and interactive config is not
  enabled
- **THEN** the system SHALL print the missing settings plus setup guidance and
  return a non-zero exit code without fetching

#### Scenario: Interactive prompt completes the config
- **WHEN** the operator runs with `--interactive-config` from a terminal and
  supplies the missing values
- **THEN** the system SHALL accept the entered values and proceed to fetch only
  when no required setting remains missing
