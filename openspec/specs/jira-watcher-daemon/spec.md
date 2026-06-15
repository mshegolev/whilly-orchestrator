## Purpose

The jira-watcher-daemon capability governs the `whilly jira watch` synchronous
poll loop that wraps the one-shot Jira refresh cycle on a configurable interval.
This capability covers loop lifecycle (interval resolution, interruptible sleep,
signal-driven graceful stop, atomic status file, and failure backoff), the
single-instance PID lock, the global pause gate, the readiness gate guarding
dispatch, and the default-off dispatch path. It captures the Phase 20 shipped
behavior — including fail-closed handling of undeterminable readiness and live
or unsignalable PID locks — so the daemon's contract is machine-checkable.

## Requirements

### Requirement: Poll interval resolution
The system SHALL resolve the poll interval in seconds by preferring the explicit
`--interval` argument, then the `WHILLY_JIRA_WATCH_INTERVAL` environment
variable, and otherwise defaulting to 300 seconds.

#### Scenario: Explicit interval wins
- **WHEN** `--interval` is supplied as a valid integer
- **THEN** the system SHALL use that value as the poll interval regardless of the
  environment variable

#### Scenario: Environment fallback then default
- **WHEN** no `--interval` is given and `WHILLY_JIRA_WATCH_INTERVAL` is unset or
  not a valid integer
- **THEN** the system SHALL fall back to the 300-second default interval

### Requirement: Interruptible signal-driven lifecycle
The system SHALL sleep between cycles using `threading.Event.wait` rather than
`time.sleep`, and MUST install SIGTERM and SIGINT handlers that set the stop
event so the loop exits gracefully with `EXIT_OK`, writing a final stopped
status and releasing the PID lock.

#### Scenario: Signal stops the loop gracefully
- **WHEN** the process receives SIGTERM or SIGINT while the loop runs or sleeps
- **THEN** the system SHALL set the stop event, break out of the loop, and return
  `EXIT_OK`
- **AND** the system SHALL write a final status with state `stopped` and release
  the PID lock

#### Scenario: Sleep is interruptible
- **WHEN** the stop event is set during the between-cycle wait
- **THEN** the wait SHALL return immediately and the loop SHALL exit without
  starting another cycle

### Requirement: Atomic secret-free status file per cycle
The system SHALL write the watcher status as a JSON file each cycle using a
tempfile plus `os.replace` atomic overwrite, and the payload MUST be free of
secrets such as tokens or the database DSN value.

#### Scenario: Status written atomically each cycle
- **WHEN** a poll cycle completes
- **THEN** the system SHALL overwrite the status file atomically so a crash
  mid-write leaves the previous consistent version on disk

#### Scenario: Status payload excludes secrets
- **WHEN** the status file is written
- **THEN** the recorded fields SHALL NOT include any Jira token or database DSN
  value

### Requirement: Exponential backoff on cycle failure
The system SHALL apply a 5, 10, 20, 40, 60-second backoff to the NEXT cycle when
consecutive cycles fail, capping at 60 seconds, and MUST reset the backoff to
zero after a fully successful cycle.

#### Scenario: Backoff grows with consecutive failures
- **WHEN** a cycle fails for one or more issues on consecutive iterations
- **THEN** the system SHALL add the next backoff value from the 5/10/20/40/60
  sequence to the following interval wait

#### Scenario: Success resets backoff
- **WHEN** a cycle completes with no failed issues
- **THEN** the system SHALL reset the backoff seconds to zero

### Requirement: Single-instance PID lock fails closed
The system SHALL guard against concurrent watchers with a PID lock acquired via
`os.open` with `O_CREAT | O_EXCL`, probing any existing PID with `os.kill(pid, 0)`,
and MUST refuse to start (returning `EXIT_VALIDATION_ERROR`) when a live watcher
holds the lock; an `EPERM`/unsignalable or otherwise unprobeable PID MUST be
treated as alive and fail closed.

#### Scenario: Live watcher refuses second start
- **WHEN** startup finds an existing PID file whose process responds to the
  signal-0 liveness probe
- **THEN** the system SHALL refuse to start and return `EXIT_VALIDATION_ERROR`
  without writing the status file

#### Scenario: Unsignalable PID treated as alive
- **WHEN** probing the stored PID raises `PermissionError` (EPERM) or another
  `OSError`
- **THEN** the system SHALL treat the holder as alive and refuse to start
  (fail closed)

#### Scenario: Stale lock is reclaimed
- **WHEN** the stored PID's process no longer exists (`ProcessLookupError`) or
  the PID file is unparseable
- **THEN** the system SHALL treat the lock as stale and reclaim it

### Requirement: Pause gate suppresses dispatch only
The system SHALL continue read-only polling while the global pause
(`.whilly_pause` via `PauseControl`) is active but MUST suppress dispatch for
that cycle and emit a best-effort `watch.paused` audit event.

#### Scenario: Polling continues while paused
- **WHEN** the global pause is active at the dispatch check point of a cycle
- **THEN** the system SHALL have already performed read-only collection and
  SHALL skip dispatch for that cycle, recording the result as `paused`

#### Scenario: Pause audit event emitted best-effort
- **WHEN** the watcher is paused and a database DSN is configured
- **THEN** the system SHALL attempt to persist a `watch.paused` event and SHALL
  continue the loop even if that persist fails

### Requirement: Readiness gate fails closed
The system SHALL block dispatch with a best-effort `watch.block` audit event
whenever the readiness verdict is not `ready_for_testing`, and an undeterminable
readiness (`None` from a missing path, unreadable file, malformed JSON, or probe
error) MUST be treated as NOT ready, unless `--allow-unready-run` overrides the
gate.

#### Scenario: Unready verdict blocks dispatch
- **WHEN** dispatch is requested and the readiness verdict is anything other than
  `ready_for_testing`
- **THEN** the system SHALL skip dispatch, record the result as `blocked`, and
  emit a best-effort `watch.block` event

#### Scenario: Undeterminable readiness blocks dispatch
- **WHEN** readiness cannot be determined and yields `None`
- **THEN** the system SHALL treat it as not ready and block dispatch unless
  `--allow-unready-run` is set

### Requirement: Default-off gated dispatch
The system SHALL keep dispatch disabled unless the explicit `--dispatch` flag is
set, and MUST run dispatch only when the watcher is unpaused AND the readiness
gate is satisfied (or overridden), recording a successful dispatch only when its
return code equals `EXIT_OK`.

#### Scenario: No dispatch without the flag
- **WHEN** `--dispatch` is not set
- **THEN** the system SHALL never invoke the dispatch runner and SHALL only
  perform read-only polling

#### Scenario: Dispatch runs only when unpaused and ready
- **WHEN** `--dispatch` is set, the watcher is unpaused, and readiness is
  satisfied
- **THEN** the system SHALL invoke the dispatch runner for each issue
- **AND** a failed dispatch SHALL be contained so it never crashes the watcher

### Requirement: CLI credential gate before the loop
The system SHALL run the Jira credential gate in the CLI layer before entering
the watch loop, and the loop itself MUST perform only read-only collection so a
misconfigured watcher exits fast rather than looping at maximum backoff.

#### Scenario: Missing config exits before looping
- **WHEN** `whilly jira watch` is started with incomplete Jira configuration in a
  non-interactive context
- **THEN** the system SHALL fail the credential gate and exit before the loop
  starts

#### Scenario: Loop performs read-only collection
- **WHEN** the watch loop runs a cycle with valid configuration
- **THEN** each cycle SHALL collect a read-only Jira snapshot and SHALL NOT
  mutate the Jira issue outside the separately gated dispatch path
