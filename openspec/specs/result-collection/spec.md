## Purpose

The result-collection capability defines how Whilly turns the stdout of a
Claude CLI agent invocation into a single immutable AgentResult value object.
It governs parsing of the `--output-format json` envelope, normalisation of
both the single-object and stream-event-array shapes, token and cost
accounting, threading the subprocess exit code through, the
`<promise>COMPLETE</promise>` completion handshake, and the defensive contract
that malformed stdout never raises.

## Requirements

### Requirement: AgentResult value object
The system SHALL parse agent stdout into an immutable `AgentResult` carrying
`output`, `usage`, `exit_code`, and `is_complete`, and MUST keep both
`AgentResult` and its nested `AgentUsage` frozen so the value can be shared
across asyncio tasks without copying.

#### Scenario: Well-formed envelope parsed
- **WHEN** `parse_output` receives a JSON object with a string `result` field
- **THEN** the system SHALL set `AgentResult.output` to that result text and
  populate `usage` from the envelope

#### Scenario: Result is immutable
- **WHEN** a caller holds an `AgentResult`
- **THEN** the system SHALL forbid mutating any field of the result or its
  nested `AgentUsage`

### Requirement: Usage and cost accounting
The system SHALL populate `AgentUsage` from the envelope with the nested token
counters (input, output, cache-read, cache-create) plus the top-level
`total_cost_usd`, `num_turns`, and `duration_ms`, and MUST default any missing
or wrongly-typed counter to zero.

#### Scenario: Counters read from the envelope
- **WHEN** the envelope nests `usage` token counters and carries top-level
  cost, turn, and duration fields
- **THEN** the system SHALL map them onto `AgentUsage.input_tokens`,
  `output_tokens`, `cache_read_tokens`, `cache_create_tokens`, `cost_usd`,
  `num_turns`, and `duration_ms`

#### Scenario: Missing usage block zeroed
- **WHEN** the envelope has no `usage` block or a wrongly-typed one
- **THEN** the system SHALL yield an `AgentUsage` with every counter at its
  zero default

### Requirement: Exit code threaded from the subprocess
The system SHALL accept the subprocess `exit_code` as a parameter to
`parse_output` rather than reading it from stdout, and MUST default it to zero
so parse-only callers stay terse.

#### Scenario: Exit code carried onto the result
- **WHEN** `parse_output` is called with an explicit `exit_code`
- **THEN** the system SHALL set `AgentResult.exit_code` to that value unchanged

#### Scenario: Default exit code
- **WHEN** `parse_output` is called with only stdout and no exit code
- **THEN** the system SHALL default `AgentResult.exit_code` to zero

### Requirement: Stream-event array normalisation
The system SHALL accept both the single result object and the newer stream
-event-array form, selecting the final `{"type": "result", ...}` event (or the
last event carrying a string `result`) as the envelope to parse.

#### Scenario: Single object envelope
- **WHEN** stdout decodes to a single JSON object
- **THEN** the system SHALL use that object directly as the result envelope

#### Scenario: Stream-event array
- **WHEN** stdout decodes to a JSON array of stream events
- **THEN** the system SHALL scan from the end and use the final result event
  for output, usage, and completion detection

### Requirement: Completion signal detection
The system SHALL set `AgentResult.is_complete` to true if and only if the
literal completion marker `<promise>COMPLETE</promise>` appears in the parsed
result text, treating that marker as the protocol-level task-done signal.

#### Scenario: Marker present
- **WHEN** the parsed result text contains `<promise>COMPLETE</promise>`
- **THEN** the system SHALL set `is_complete` to true

#### Scenario: Marker absent
- **WHEN** the parsed result text does not contain the completion marker
- **THEN** the system SHALL set `is_complete` to false

### Requirement: Defensive no-raise fallback
The system SHALL never raise on empty, malformed, plaintext, or usage-less
stdout, and MUST instead return a valid `AgentResult` with a zeroed
`AgentUsage`, the raw stdout as `output`, while still scanning that raw stdout
for the completion marker.

#### Scenario: Empty stdout
- **WHEN** `parse_output` receives an empty string
- **THEN** the system SHALL return an `AgentResult` with empty output, zeroed
  usage, and the supplied exit code, without raising

#### Scenario: Plaintext or malformed stdout
- **WHEN** stdout cannot be decoded as JSON, or decodes to a value with no
  usable result envelope
- **THEN** the system SHALL return an `AgentResult` whose `output` is the raw
  stdout and whose `is_complete` reflects whether the marker is in that stdout

#### Scenario: Envelope without a usable result field
- **WHEN** the envelope parses but `result` is missing or non-string
- **THEN** the system SHALL fall back to the raw stdout as `output` so
  operators retain visibility
