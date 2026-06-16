## Purpose

The verification-gates capability governs the post-task quality gates that decide
whether agent work is allowed to reach a DONE state in the live v4 worker-claim path.
It covers the pipeline verification runner in `whilly/pipeline/verification.py`
(`run_verification_commands` / `resolve_verification_specs`, required-vs-warning
command semantics, the `VerificationRunOutcome` aggregate, env allowlisting,
timeout/blocked handling, and verification started/result events), the human-review
checkpoint gate in `whilly/pipeline/human_review.py` (`requires_human_review`,
`build_human_review_checkpoint`, `is_human_review_approved`, and the required/approved/
rejected/changes-requested events), and the CI-side verification contract in
`whilly/ci/verification.py`. It also records that the v3 commit-revert verifier
`whilly/verifier.py::verify_task` is legacy and is NOT wired into the v4 worker path.
These gates consume an AgentResult (see result-collection) and run where the
orchestration loop completes a task (see orchestration-loop).

## Requirements

### Requirement: Required-vs-warning verification outcome gates DONE
The system SHALL run resolved verification specs via `run_verification_commands` and
return a `VerificationRunOutcome` whose `required_failed` is True if and only if at
least one required command did not succeed, where `succeeded` is the negation of
`required_failed` and a failing non-required command increments `warning_count`
without failing the gate, so the worker blocks the DONE transition only on a required
failure.

#### Scenario: Required command failure fails the gate
- **WHEN** `run_verification_commands` runs a spec with `required=True` whose command
  exits non-zero
- **THEN** the resulting `VerificationRunOutcome.required_failed` SHALL be True and
  `succeeded` SHALL be False

#### Scenario: Optional command failure is a warning only
- **WHEN** a spec with `required=False` fails
- **THEN** the system SHALL mark that result as `warning=True`, count it in
  `warning_count`, and leave `VerificationRunOutcome.succeeded` True

### Requirement: Verification started and result events
The system SHALL expose `make_verification_started_event` and
`make_verification_result_event` and a `VerificationRunOutcome.event_names` sequence
that begins with the started event followed by one result event per command, and MUST
redact secrets from the command text and captured stdout/stderr carried on result
events.

#### Scenario: Started event precedes per-command results
- **WHEN** verification runs over a set of resolved commands
- **THEN** `event_names` SHALL begin with `verification.started` and then carry one
  `event_name` per command result in order

#### Scenario: Secrets redacted on result events
- **WHEN** `make_verification_result_event` builds the event for a command result
- **THEN** the system SHALL redact secrets from the command string and the stdout and
  stderr detail before they are emitted

### Requirement: Env allowlist and non-hanging command execution
Verification command execution SHALL run each command under an allowlisted child
environment containing only the named variables that are present in the parent
process, and MUST return a structured timeout or blocked result (never hang) when a
command exceeds its timeout or is denied by the shell command scanner.

#### Scenario: Only allowlisted variables reach the child
- **WHEN** a command runs with an `env_allowlist`
- **THEN** the child environment SHALL contain only the allowlisted variables that
  exist in the parent environment and SHALL omit any that are absent

#### Scenario: Timeout produces a result instead of hanging
- **WHEN** a command exceeds its timeout
- **THEN** the system SHALL kill the process group and return a
  `VerificationCommandResult` with `timed_out=True` rather than blocking indefinitely

#### Scenario: Dangerous command is blocked
- **WHEN** the shell command scanner flags a command before execution
- **THEN** the system SHALL return a `VerificationCommandResult` with `blocked=True`
  and SHALL NOT execute the command

### Requirement: Human-review checkpoint gate
The system SHALL detect a human-review requirement via `requires_human_review` (stage
human gate, configured required step, or a human-review cue in task/stage text), build
a `HumanReviewCheckpoint` via `build_human_review_checkpoint`, hold the task until a
decision is recorded, and proceed only when `is_human_review_approved` returns True
for the latest matching decision, emitting the required/approved/rejected/
changes-requested events accordingly.

#### Scenario: Review requirement detected and checkpoint built
- **WHEN** a stage carries a human gate or a task's review text contains a recognized
  human-review cue
- **THEN** `requires_human_review` SHALL return True and
  `build_human_review_checkpoint` SHALL return a populated `HumanReviewCheckpoint`

#### Scenario: Task proceeds only on a matching approval
- **WHEN** `is_human_review_approved` evaluates the recorded events for a checkpoint
- **THEN** the system SHALL return True only when the latest matching decision is an
  approval carrying a non-empty reviewer, and SHALL return False on rejection or
  changes-requested

### Requirement: CI verification contract
CI-sourced verification SHALL be executed via `run_ci_verification`, which converts a
CI verification spec into a CI poll, runs the configured poll runner, and maps the CI
poll result into a `VerificationCommandResult` and `CIPollEvidence` consistent with
the required-vs-warning semantics, and MUST return an unavailable result rather than
raising when no CI poll runner is configured.

#### Scenario: CI poll mapped into a verification result
- **WHEN** `run_ci_verification` runs a CI spec with a configured poll runner
- **THEN** the system SHALL return `CIPollEvidence` and a `VerificationCommandResult`
  whose success and event name follow the same required-vs-warning rules as shell
  verification

#### Scenario: Missing CI runner yields an unavailable result
- **WHEN** `run_ci_verification` runs without a configured CI poll runner
- **THEN** the system SHALL synthesize an unavailable CI poll result rather than
  raising

### Requirement: Legacy commit-revert verifier is unwired
The system SHALL treat `whilly/verifier.py::verify_task` (the v3 commit-revert verifier
that reverts the last commit on lint/test failure) as LEGACY and MUST NOT rely on it
in the v4 worker-claim DONE path; the live gates are the pipeline verification runner,
the human-review checkpoint gate, and the CI verification contract above.

#### Scenario: Live path does not invoke the legacy verifier
- **WHEN** the v4 worker completes a task and runs its gates
- **THEN** the DONE decision SHALL be driven by `run_verification_commands` and the
  human-review gate, and SHALL NOT depend on `verify_task`

#### Scenario: Legacy verifier remains documented but inert
- **WHEN** `verify_task` is referenced in the repository
- **THEN** it SHALL appear only as a legacy helper (including a compliance docstring
  mention) and SHALL NOT be wired into the worker-claim completion path
