## Purpose

The prd-generation capability governs non-interactive synthesis of a PRD
(Product Requirements Document) markdown file from a brief project description.
It covers `generate_prd` in `whilly/prd_generator.py`, which builds a
PRD-authoring prompt, drives the Claude CLI via `_call_claude`, post-processes
the model output into clean markdown, and writes `PRD-<slug>.md`. It also
covers the task/epic classification subsystem in `whilly/classifier/*`
(`router`, `heuristic`, `llm`, `matcher`, `epic_inferrer`, `rebuilder`) that
decides the hierarchy level of an incoming idea before it becomes structured
work. This capability explicitly scopes OUT the PRD-to-tasks.json contract,
which belongs to the task-generation capability per the Phase 23 boundaries.

## Requirements

### Requirement: PRD document synthesis from a description
The system SHALL synthesise a PRD markdown document from a free-form
description by calling the Claude CLI through `_call_claude` with a prompt
built from the PRD authoring template, and SHALL write the result to
`PRD-<slug>.md` under the configured output directory.

#### Scenario: Description produces a PRD file
- **WHEN** `generate_prd` is called with a non-empty `description` and an
  `output_dir`
- **THEN** the system SHALL create the output directory if absent, call Claude
  with the PRD authoring prompt, and write the returned markdown to a file
  named `PRD-<slug>.md`
- **AND** the system SHALL return the `Path` to the written PRD file

#### Scenario: Empty model response is rejected
- **WHEN** `_call_claude` returns an empty string for the synthesis prompt
- **THEN** the system SHALL raise a `RuntimeError` rather than write an empty
  PRD file

### Requirement: Slug derivation for the PRD filename
The system SHALL use the caller-supplied `slug` for the `PRD-<slug>.md`
filename when provided, and MUST otherwise derive the slug from the first ~50
characters of the description reduced to alphanumeric, hyphen, and underscore
characters.

#### Scenario: Explicit slug wins
- **WHEN** `generate_prd` is called with a non-None `slug` argument
- **THEN** the system SHALL name the output file `PRD-<slug>.md` using that
  slug verbatim, so the filename matches the `plan_id` the CLI later imports

#### Scenario: Slug auto-derived from description
- **WHEN** `generate_prd` is called with `slug=None`
- **THEN** the system SHALL derive the slug from the leading description text,
  keeping only alphanumeric, hyphen, and underscore characters

### Requirement: Output normalisation strips markdown fences
The system SHALL strip a leading ` ```markdown ` or ` ``` ` fence and a
trailing ` ``` ` fence from the model output before persisting, so the saved
PRD is clean markdown rather than a fenced code block.

#### Scenario: Fenced output is unwrapped
- **WHEN** Claude returns PRD content wrapped in a leading ` ```markdown ` and
  a trailing ` ``` ` fence
- **THEN** the system SHALL remove both fences and write only the inner
  markdown to the PRD file

### Requirement: Claude CLI invocation contract for synthesis
The system SHALL invoke the Claude binary resolved from the `CLAUDE_BIN`
environment variable (default `claude`) in `-p` print mode with file-writing
tools disallowed, MUST honour `WHILLY_CLAUDE_TIMEOUT`, and SHALL return an
empty string (never raise) when the binary is missing, exits non-zero, or
times out.

#### Scenario: File-writing tools are disallowed
- **WHEN** `_call_claude` builds the subprocess command
- **THEN** the system SHALL pass `--disallowedTools` covering Write, Edit,
  MultiEdit, NotebookEdit, and Bash so the model returns the document on
  stdout instead of trying to save it

#### Scenario: Timeout yields empty result
- **WHEN** the Claude subprocess exceeds the timeout from
  `WHILLY_CLAUDE_TIMEOUT`
- **THEN** the system SHALL kill the process and return an empty string, which
  the synthesis caller turns into a `RuntimeError`

### Requirement: Idea classification feeds PRD structure
The system SHALL classify an incoming idea into a hierarchy level (Epic,
Story, or Task) via the classifier subsystem, where `LLMClassifier` is the
primary path and MUST fall back to `HeuristicClassifier` (a length- and
keyword-rule classifier) on any LLM transport failure, non-zero exit, or
unparseable output, never raising to the caller.

#### Scenario: LLM failure falls back to heuristic
- **WHEN** `LLMClassifier.classify` encounters an LLM error, a non-zero exit,
  or unparseable JSON
- **THEN** the system SHALL return a `ClassificationResult` produced by the
  heuristic classifier with an added flag describing the fallback reason

#### Scenario: Router rejects out-of-scope or too-short input
- **WHEN** `Router` routes text whose classification carries an
  `out-of-scope` or `below-length-threshold` flag
- **THEN** the system SHALL return a `RoutingDecision` with action `REJECT`
  rather than attempting to link or create work

### Requirement: Parent routing and orphan handling
The system SHALL route a classified non-Epic item by matching it against
candidate parents one level above, linking it as a child only when the top
parent-match score meets the match threshold, and MUST otherwise create an
orphan at the classified level for human review.

#### Scenario: Confident parent match links as child
- **WHEN** the matcher returns a best parent whose score is at or above the
  router's `match_threshold`
- **THEN** the system SHALL return a `RoutingDecision` with action
  `LINK_AS_CHILD` and the matched parent as the target

#### Scenario: No confident parent creates an orphan
- **WHEN** no candidate parents exist or the top match score is below
  `match_threshold`
- **THEN** the system SHALL return a `RoutingDecision` with action
  `CREATE_ORPHAN` at the classified level
