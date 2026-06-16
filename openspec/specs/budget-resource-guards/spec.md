## Purpose

The budget-resource-guards capability governs how Whilly protects the host
system from resource exhaustion and how it records when a plan crosses its
spend budget. It covers the `ResourceMonitor` library in
`whilly/resource_monitor.py` (CPU, memory, disk, process-count and log-directory
thresholds, throttle decisions and resource-wait polling), the secret-free
smoke-report foundation in `whilly/cli/smoke.py` (its `EXIT_OK` / `EXIT_CHECK_FAILED`
/ `EXIT_CONFIG_MISSING` exit codes and URL redaction), and the v4 budget contract
which is a Postgres audit sentinel (`plan.budget_exceeded`) defined in
`whilly/adapters/db/repository.py`. This capability deliberately supersedes the
legacy v3 lore in which exceeding the budget killed all tmux sessions and forced
process exit 2: that path is NOT the live v4 contract. For the general process
exit-code contract this spec references the `cli-surface` capability rather than
re-asserting a budget-specific exit code.

## Requirements

### Requirement: ResourceLimits default thresholds
The system SHALL default `ResourceLimits` to max_cpu_percent 80, max_process_cpu 50, max_memory_percent 75, max_process_memory_mb 2048, min_free_space_gb 5, max_log_dir_size_gb 2, max_concurrent_processes 5, process_timeout_minutes 30, and monitor_interval_seconds 10, and `ResourceMonitor.check_limits` SHALL return a violations dict whose entries each carry a per-resource severity.

#### Scenario: Default limits used when none supplied
- **WHEN** a `ResourceMonitor` is constructed without an explicit `ResourceLimits`
- **THEN** the monitor SHALL apply the documented default thresholds (cpu 80, memory 75, disk 5GB free, 5 processes, 2GB log dir, 30-minute process timeout)

#### Scenario: check_limits flags an exceeded resource with severity
- **WHEN** `check_limits` evaluates usage where CPU is above max_cpu_percent
- **THEN** the returned violations dict SHALL contain a `cpu` entry with `current`, `limit`, and a `severity` of `high` when CPU exceeds 90 percent, otherwise `medium`
- **AND** a disk shortfall SHALL be marked `high` when free space is below 1GB and the log-directory overage SHALL be marked `low`

### Requirement: Throttle decision rule
The system SHALL return `True` from `ResourceMonitor.should_throttle` when there is any high-severity violation, or when there are at least two medium-severity violations, and otherwise SHALL return `False`.

#### Scenario: Single high-severity violation throttles
- **WHEN** `should_throttle` evaluates usage containing one high-severity violation
- **THEN** the system SHALL return `True`

#### Scenario: Two medium-severity violations throttle
- **WHEN** `should_throttle` evaluates usage containing two or more medium-severity violations and no high-severity violation
- **THEN** the system SHALL return `True`

#### Scenario: A single medium violation does not throttle
- **WHEN** `should_throttle` evaluates usage containing exactly one medium-severity violation and no high-severity violation
- **THEN** the system SHALL return `False`

### Requirement: Resource-wait polling and environment overrides
The system SHALL poll in `ResourceMonitor.wait_for_resources` until usage is no longer throttled or `max_wait_seconds` (default 300) elapses, emitting at most one throttled warning per 60-second cooldown, and `create_monitor_from_env` SHALL apply the WHILLY_MAX_CPU_PERCENT, WHILLY_MAX_MEMORY_PERCENT, WHILLY_MIN_FREE_SPACE_GB and WHILLY_PROCESS_TIMEOUT_MINUTES environment overrides.

#### Scenario: wait returns true once resources recover
- **WHEN** `wait_for_resources` is polling and `should_throttle` becomes `False` before `max_wait_seconds`
- **THEN** the system SHALL return `True`

#### Scenario: wait times out while still throttled
- **WHEN** `wait_for_resources` keeps observing a throttle condition until `max_wait_seconds` elapses
- **THEN** the system SHALL return `False`
- **AND** while waiting it SHALL log a violations warning no more often than once per 60-second cooldown

#### Scenario: environment overrides applied at construction
- **WHEN** `create_monitor_from_env` runs with WHILLY_MAX_CPU_PERCENT set
- **THEN** the constructed monitor's `ResourceLimits.max_cpu_percent` SHALL equal the supplied value

### Requirement: Budget-exceeded Postgres sentinel
The system SHALL emit exactly one `plan.budget_exceeded` audit event (event type `BUDGET_EXCEEDED_EVENT_TYPE`, reason `budget_threshold`, threshold_pct 100) on the single cost-application call that moves `plans.spent_usd` from strictly below `budget_usd` to greater than or equal to `budget_usd`, and a plan whose `budget_usd` is 0 or NULL SHALL be treated as unlimited and SHALL NOT emit the sentinel.

#### Scenario: sentinel emitted once on budget crossing
- **WHEN** a cost application raises `plans.spent_usd` from below `budget_usd` to at or above `budget_usd`
- **THEN** the system SHALL write a single `plan.budget_exceeded` event row carrying reason `budget_threshold` and threshold_pct 100

#### Scenario: no sentinel after the crossing call
- **WHEN** a later cost application observes `spent_usd` already at or above `budget_usd`
- **THEN** the system SHALL NOT write another `plan.budget_exceeded` event for that plan

#### Scenario: unlimited budget never emits sentinel
- **WHEN** a plan has `budget_usd` of 0 or NULL and any cost is applied
- **THEN** the system SHALL NOT emit a `plan.budget_exceeded` event
- **AND** the system SHALL NOT kill agent sessions or force a budget-specific process exit code

### Requirement: Secret-free smoke-report exit codes
The system SHALL expose the smoke-report exit-code constants `EXIT_OK` (0), `EXIT_CHECK_FAILED` (1) and `EXIT_CONFIG_MISSING` (2), SHALL report `SmokeReport.all_passed` as `True` only when every recorded check passed, and SHALL strip any `user:pass@` authority from URLs via `_redact_url` before they are written into a timestamped report; the general process exit-code contract is defined by the `cli-surface` capability.

#### Scenario: all checks pass yields EXIT_OK
- **WHEN** every check recorded in a `SmokeReport` has `passed is True`
- **THEN** `all_passed` SHALL be `True` and the command SHALL surface `EXIT_OK` (0)

#### Scenario: a failed check yields EXIT_CHECK_FAILED
- **WHEN** at least one recorded check has `passed is False` while configuration is present
- **THEN** `all_passed` SHALL be `False` and the command SHALL surface `EXIT_CHECK_FAILED` (1)

#### Scenario: report redacts credentials in URLs
- **WHEN** `write_smoke_report` serialises a report that references a URL containing a `user:pass@` authority
- **THEN** the written JSON SHALL contain only the host-and-path form with credentials removed by `_redact_url`

### Requirement: ResourceMonitor wiring status
The system SHALL treat `whilly/resource_monitor.py` as a standalone protection library that is NOT currently wired into the v4 Postgres worker-claim dispatch path, and the live spend-limit contract SHALL be the `plan.budget_exceeded` sentinel rather than any in-loop ResourceMonitor throttle.

#### Scenario: ResourceMonitor is not invoked by the worker-claim loop
- **WHEN** the v4 worker-claim dispatch path runs
- **THEN** the system SHALL NOT depend on `ResourceMonitor.should_throttle` to gate task dispatch

#### Scenario: budget enforcement is the sentinel, not the monitor
- **WHEN** spend limits are evaluated for a plan
- **THEN** the authoritative signal SHALL be the `plan.budget_exceeded` audit sentinel and NOT a ResourceMonitor decision
