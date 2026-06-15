## Purpose

The notifications capability governs Whilly's outbound dispatch surfaces: the
run-completed Slack notifier port (`whilly/core/notifications.py`,
`whilly/adapters/notifications/*`), the per-task demo Slack webhook
(`whilly/slack_task_notify.py`), the SMTP magic-link `Mailer`
(`whilly/api/mailer.py`), the Confluence release-doc publisher
(`whilly/adapters/confluence/publisher.py`), and local macOS `say` voice
announcements (`whilly/notifications.py`). This capability covers each channel's
auth/config gate and the cross-cutting rule that outbound notification dispatch
is best-effort and MUST NOT block or break the orchestration loop. It references
— but does not duplicate — the events/audit layer those channels feed.

## Requirements

### Requirement: Slack notifier factory gates on full config
The system SHALL satisfy the `NotificationPort` with a `SlackNotifier` only when `SLACK_ENABLED`, `SLACK_ACCESS_TOKEN`, and `SLACK_CHANNEL` are all configured, and `make_notifier` SHALL otherwise return a no-op `NullNotifier`.

#### Scenario: Full config produces a SlackNotifier
- **WHEN** `make_notifier` is called with a config where `SLACK_ENABLED` is true and both `SLACK_ACCESS_TOKEN` and `SLACK_CHANNEL` are non-empty
- **THEN** the system SHALL return a `SlackNotifier` constructed with the token, channel, API base URL, timeout, and message template

#### Scenario: Missing config produces a NullNotifier
- **WHEN** `make_notifier` is called and `SLACK_ENABLED` is false, or the access token is empty, or the channel is empty
- **THEN** the system SHALL return a `NullNotifier` whose `notify_run_completed` discards the event

#### Scenario: Adapters perform no env reads
- **WHEN** a `SlackNotifier` is constructed
- **THEN** every deployment-varying value (token, channel, API base URL, timeout, template) SHALL be injected through the constructor by the factory
- **AND** the adapter SHALL NOT read any environment variable itself

### Requirement: Slack dispatch is best-effort
The system SHALL treat every Slack send — whether the `chat.postMessage` token API or the per-task incoming webhook — as best-effort, and a transport failure MUST be logged at WARNING and swallowed rather than raised into the run loop.

#### Scenario: Transport error does not propagate
- **WHEN** a Slack `http_post` raises `URLError`, `HTTPError`, `TimeoutError`, or `OSError`
- **THEN** the notifier SHALL log a WARNING and return normally without raising
- **AND** the orchestrator exit code SHALL NOT change because Slack is unavailable

#### Scenario: Logical API error is logged, not raised
- **WHEN** a Slack token-API response body has `ok` falsey (or returns an `error`)
- **THEN** the system SHALL log the API error at WARNING and SHALL NOT raise

### Requirement: Mailer attempts SMTP only when host set and falls back to the event log
The system SHALL attempt SMTP delivery via the `Mailer` only when `WHILLY_SMTP_HOST` is set, MUST fall back to appending an `auth.magic_link.sent` event to `whilly_events.jsonl` when SMTP is unconfigured or fails, and MUST never raise out of `send_magic_link`.

#### Scenario: No SMTP host uses the event-log path
- **WHEN** `Mailer.send_magic_link` is awaited and `WHILLY_SMTP_HOST` is empty or unset
- **THEN** the system SHALL write the magic-link event to the configured event log and return the transport mode `"event_log"`

#### Scenario: SMTP failure falls back without raising
- **WHEN** an SMTP send raises (connection refused, auth failure, missing `aiosmtplib`, etc.)
- **THEN** the system SHALL log a WARNING, append the magic-link event to the event log, and return `"event_log"`
- **AND** `send_magic_link` SHALL NOT raise

### Requirement: Confluence publisher is the credentialed release-doc surface
The system SHALL publish release docs through the `ConfluencePublisher`, which MUST require a non-empty `server_url` and `token` at construction and authenticate each REST call with HTTP Basic (`username:token`) or Bearer depending on the configured `auth_scheme`.

#### Scenario: Missing credentials rejected at construction
- **WHEN** a `ConfluencePublisher` is constructed with an empty `server_url` or empty `token`
- **THEN** the system SHALL raise a `ValueError` before any REST call is made

#### Scenario: Auth header chosen by scheme
- **WHEN** the publisher issues a Confluence REST request
- **THEN** it SHALL send a `Bearer <token>` Authorization header when `auth_scheme` is `bearer`
- **AND** it SHALL send a base64 `Basic <username:token>` Authorization header otherwise

### Requirement: Local voice notifications are a no-op when unavailable
The system SHALL emit macOS `say` voice notifications only when `WHILLY_VOICE` is enabled and the `say` binary is present, and MUST be a silent no-op otherwise.

#### Scenario: Voice disabled or binary absent
- **WHEN** `notify` is called while `WHILLY_VOICE` is disabled or `shutil.which("say")` resolved to nothing
- **THEN** the system SHALL return without speaking and without raising

#### Scenario: say invocation failure is swallowed
- **WHEN** the `say` subprocess launch raises `OSError`
- **THEN** the system SHALL swallow the error so voice output never blocks or breaks the run loop

### Requirement: Outbound dispatch never gates orchestration
The system SHALL treat all notification channels as observability side effects, and no channel MUST ever block, gate, or change the outcome of the orchestration loop on transport or delivery failure.

#### Scenario: Per-task Slack notify never blocks workers
- **WHEN** `notify_slack_task_started` or `notify_slack_task_terminal` runs with no webhook URL and no access token configured
- **THEN** the system SHALL return without sending and without raising

#### Scenario: Disabled or unselected events emit nothing
- **WHEN** the per-task notifier evaluates `_should_emit` for an event that is disabled or not in the configured event set
- **THEN** the system SHALL skip the Slack send entirely
