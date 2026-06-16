## Purpose

The quality-compliance-audit capability governs the post-implementation quality,
compliance, audit-trail, and QA-release subsystems that establish whether a change
is mergeable and whether the repository matches its claimed behavior. It covers the
language-agnostic quality gates in `whilly/quality/` (per-language lint/test runners
with auto-detection and multi-language aggregation), the deterministic target-doc
compliance report generated via `whilly compliance` (`whilly/compliance/`), the
append-only JSONL audit-event sink in `whilly/audit/jsonl_sink.py`, and the QA
release-verification artifacts (release context, test plan, autotest scaffold)
produced via `whilly qa-release` (`whilly/qa_release/`). This capability exists so
quality outcomes, compliance findings, audit events, and QA artifacts are produced
deterministically and never raise on expected failures.

## Requirements

### Requirement: Per-language quality gate contract
Each language quality gate registered under `whilly/quality/` SHALL implement the
`QualityGate` Protocol by exposing a `kind` key, a `detect(cwd)` that returns True
only when its marker files are present, and a `run(cwd)` that executes the language's
lint/test stages and returns a `GateResult` whose `passed` is True if and only if
every `StageResult` passed, and MUST NOT raise on a test/lint failure, missing
binary, or timeout (those are reported as a failed `StageResult` carrying a
human-readable summary).

#### Scenario: Gate detects its language and runs stages
- **WHEN** `PythonQualityGate.run(cwd)` executes against a project whose markers
  (`pyproject.toml`/`setup.py`/`setup.cfg`/`requirements.txt`) are present
- **THEN** the system SHALL run the pytest and ruff lint/format stages via
  `run_stage` and return a `GateResult` with one `StageResult` per stage
- **AND** `GateResult.passed` SHALL be True only when every stage's `passed` is True

#### Scenario: Missing binary or timeout fails the stage without raising
- **WHEN** a stage's binary is absent from PATH or the subprocess exceeds the stage
  timeout in `run_stage`
- **THEN** the system SHALL return a `StageResult` with `passed=False` and a summary
  explaining the missing binary or timeout rather than raising an exception

### Requirement: Multi-language detection and aggregation
The system SHALL detect every applicable gate for a project via `detect_gates` and
run them through `run_all`, returning a single aggregate `GateResult` with
`gate_kind="multi"` whose `passed` is True only when every gate passed and whose
`stages` concatenate the stages of all gates, and MUST return `passed=True` with an
informational summary when no language gate is detected.

#### Scenario: Multiple languages aggregated
- **WHEN** `run_detected(cwd)` runs in a repository where more than one language gate
  detects
- **THEN** the system SHALL run each detected gate and return one `gate_kind="multi"`
  result whose `passed` is the logical AND of the individual gate results
- **AND** the aggregate `stages` SHALL include the stages from every gate that ran

#### Scenario: No gates detected is a non-error outcome
- **WHEN** `run_all` is called with an empty gate list
- **THEN** the system SHALL return a `GateResult` with `gate_kind="multi"`,
  `passed=True`, and a summary noting that no language gates were detected

### Requirement: Deterministic target-doc compliance report
The `whilly compliance report` command (`run_compliance_command`) SHALL build a
`ComplianceReport` by deterministically inspecting the repository and emit it in
markdown or JSON to the `--out` path, where every capability row carries a
`CapabilityStatus` of PASS, PARTIAL, FAIL, or UNKNOWN with concrete repo evidence and
the overall status degrades to the weakest row's status.

#### Scenario: Report written in the requested format
- **WHEN** `run_compliance_command` runs `report --out <path> --format json` against a
  repository root
- **THEN** the system SHALL write the serialized `ComplianceReport.to_dict()` to that
  path and return exit code 0
- **AND** each matrix row SHALL use exactly one of PASS, PARTIAL, FAIL, or UNKNOWN

#### Scenario: Present-but-unwired capability reported as PARTIAL
- **WHEN** a capability's helper exists in the repository but is not wired into the
  default runtime path
- **THEN** `build_compliance_report` SHALL classify that capability as PARTIAL rather
  than PASS

### Requirement: Append-only JSONL audit-event sink
The `JsonlEventSink.record` method SHALL append exactly one JSON object per line to
`<log_dir>/whilly_events.jsonl`, carrying `ts`, `event`, `event_type`, `task_id`,
`plan_id`, and `payload` keys, and MUST treat the write as a best-effort mirror that
logs and swallows any `OSError` so the caller's primary flow (the Postgres commit) is
never affected.

#### Scenario: One JSON object appended per event
- **WHEN** `JsonlEventSink.record` is called with an `event_type` and payload
- **THEN** the system SHALL append a single newline-terminated JSON object carrying
  both `event` and `event_type` set to that type plus the `task_id`, `plan_id`, and
  `payload` fields

#### Scenario: Disk write failure is swallowed
- **WHEN** appending the line raises an `OSError` (read-only filesystem, full disk,
  permissions)
- **THEN** the system SHALL log the failure at WARNING and return without raising

### Requirement: QA-release artifact generation
The `whilly qa-release` command (`run_qa_release_command`) SHALL provide the `collect`,
`plan`, and `scaffold-tests` subcommands that respectively produce a `ReleaseContext`
from a Jira reference, a deterministic `QATestPlan` from a release context, and a
generated pytest contract suite from a test plan, and MUST refuse to overwrite a
non-generated existing test file unless `--force` is supplied.

#### Scenario: Context flows through plan into a scaffolded suite
- **WHEN** `qa-release plan` consumes a release-context JSON
- **THEN** the system SHALL build a `QATestPlan` with deterministic requirements and
  test cases and serialize it to the `--out` target

#### Scenario: Refuse to clobber a hand-written test file
- **WHEN** `write_autotest_suite` targets an existing file that lacks the generated
  marker and `force` is False
- **THEN** the system SHALL raise rather than overwrite the non-generated file
