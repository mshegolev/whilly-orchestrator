## Purpose

The reporting capability governs Whilly's cost-and-progress reporting surface in
`whilly/reporter.py`: the per-iteration JSON report written to a timestamped path under
the report directory, the terminal/finalized report that records cost totals at end of
run, the cross-plan Markdown summary produced by `generate_summary`, and the
human-readable formatting helpers (`fmt_tokens`, `fmt_duration`, `CostTotals`). This
capability also records the truthful v4 wiring status: the `Reporter` class and
`generate_summary` are a legacy reporting contract that is not instantiated or called in
the v4 worker-claim execution path, while the formatting helpers remain the live
presentation contract consumed by the Rich Live dashboard.

## Requirements

### Requirement: Per-iteration JSON report path
The `Reporter` SHALL write its report as JSON to a timestamped path of the form
`whilly_{plan-stem}_{timestamp}.json` under the report directory, creating the report
directory if it does not exist.

#### Scenario: Report path is timestamped under the report dir
- **WHEN** a `Reporter` is constructed with a plan file and report directory
- **THEN** the system SHALL create the report directory and set `json_path` to
  `whilly_{stem}_{YYYYMMDD_HHMMSS}.json` within it
- **AND** the system SHALL write an initial JSON report with `finished_at` set to null

#### Scenario: Adding an iteration persists the JSON
- **WHEN** `Reporter.add_iteration` is called with an `IterationReport`
- **THEN** the system SHALL append the iteration and rewrite the JSON report to
  `json_path`

### Requirement: Finalized report records cost totals
The `Reporter.finalize` method SHALL emit a terminal JSON report that sets `finished_at`
and embeds the run cost totals together with iteration count, duration, and task counts.

#### Scenario: Finalize stamps completion and totals
- **WHEN** `Reporter.finalize` is called with the total iterations, duration, and initial
  / final / done task counts
- **THEN** the system SHALL write the JSON report with a non-null `finished_at`
- **AND** the `totals` object SHALL include `cost_usd`, `iterations`, `duration_s`,
  `tasks_initial`, `tasks_final`, and `tasks_done`

### Requirement: Cross-plan Markdown summary
The `generate_summary` function SHALL aggregate multiple plan report JSON files into a
single Markdown summary and return the summary path, and SHALL return `None` when no
readable reports are supplied.

#### Scenario: Summary aggregates multiple report files
- **WHEN** `generate_summary` is called with one or more readable report JSON files and an
  output directory
- **THEN** the system SHALL write a `whilly_summary_{timestamp}.md` file containing the
  grand totals and a per-plan table, and SHALL return its path

#### Scenario: No reports yields None
- **WHEN** `generate_summary` is called with an empty list (or only unreadable files)
- **THEN** the system SHALL return `None` and SHALL NOT write a summary file

### Requirement: Human-readable formatting helpers
The system SHALL provide `fmt_tokens`, `fmt_duration`, and the `CostTotals` accumulator as
the human-readable presentation contract for token, duration, and cost values reused by
the dashboard.

#### Scenario: Token counts are abbreviated
- **WHEN** `fmt_tokens` is called with a value of at least one thousand
- **THEN** the system SHALL return a compact suffixed form (`K` for thousands, `M` for
  millions)

#### Scenario: Durations are rendered compactly
- **WHEN** `fmt_duration` is called with a number of seconds
- **THEN** the system SHALL return an `Nh`/`Nm`/`Ns` compact string scaled to the
  magnitude of the value

#### Scenario: CostTotals accumulates agent usage
- **WHEN** `CostTotals.add_usage` is called with an agent usage record
- **THEN** the system SHALL add its input, output, cache-read, cache-create tokens and
  cost into the running totals

### Requirement: Legacy v4 wiring status
The system SHALL treat the `Reporter` class and `generate_summary` as a legacy reporting
contract that is not wired into the v4 worker-claim execution loop, while the formatting
helpers (`fmt_tokens`, `fmt_duration`, `CostTotals`) SHALL remain the live presentation
contract imported by `whilly/dashboard.py`.

#### Scenario: Reporter is not driven by the v4 worker-claim loop
- **WHEN** the v4 worker-claim execution path runs
- **THEN** the system SHALL NOT be required to instantiate `Reporter` or call
  `generate_summary` per iteration

#### Scenario: Dashboard consumes only the formatting helpers
- **WHEN** `whilly/dashboard.py` imports from `whilly.reporter`
- **THEN** the import SHALL be limited to `CostTotals`, `fmt_duration`, and `fmt_tokens`
