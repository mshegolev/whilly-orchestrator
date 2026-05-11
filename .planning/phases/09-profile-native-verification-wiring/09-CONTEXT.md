# Phase 9: Profile-native verification wiring - Context

**Gathered:** 2026-05-08
**Status:** Ready for planning

<domain>
## Phase Boundary

Phase 9 wires `ProjectConfig.verification_commands` into generated Whilly plans and worker execution so profile-native required verification can block `DONE`. It must preserve explicit CLI verification commands and reuse the existing verification event/audit path.

</domain>

<decisions>
## Implementation Decisions

### Command Sources And Precedence
- Profile-native verification commands should be generated from `ProjectConfig.verification_commands` into task/plan metadata that workers can execute.
- Explicit CLI verification commands remain supported and must not be silently replaced.
- When both profile-native and explicit CLI commands exist, execute the union in deterministic order: profile-native commands first, then explicit CLI commands.
- Required verification failures continue to block normal `DONE` and route through the existing `verification_failed` worker behavior.

### Runtime Wiring
- Local and remote workers should use the same verification command resolution semantics.
- Profile-native verification must reuse `whilly/pipeline/verification.py` result models, redaction, and event builders.
- Phase 8 secret lint and runner environment allowlist boundaries should remain intact; this phase should not bypass guard/redaction behavior when verification commands come from profile config.
- Generated plans should carry verification evidence clearly enough for compliance reporting to distinguish profile-native commands from ad hoc CLI commands.

### Compliance Evidence
- Compliance should report profile-native verification separately from explicit CLI verification support.
- Compliance wording must stay honest: this phase proves configured profile commands feed runtime verification, not that every project profile has exhaustive test coverage.
- Current-vs-target docs should only be updated if needed to align compliance evidence; avoid broad documentation rewrites.

### Claude's Discretion
- The exact internal representation of generated verification commands is at Claude's discretion as long as it is typed, testable, and does not break existing public CLI behavior.

</decisions>

<code_context>
## Existing Code Insights

### Reusable Assets
- `whilly/project_config/plan_builder.py` generates Whilly plan/task structures from project configs.
- `whilly/project_config/loader.py` validates project configs, including `verification_commands`.
- `whilly/pipeline/verification.py` owns verification command execution, result events, required/optional semantics, and Phase 8 redaction.
- `whilly/cli/run.py`, `whilly/worker/local.py`, and `whilly/worker/remote.py` already support explicit verification command execution paths.
- `whilly/compliance/__init__.py` already reports current/partial/future capability evidence.

### Established Patterns
- Preserve control-plane framing and explicit current-vs-target boundaries.
- Reuse existing worker failure paths and audit event builders instead of adding new task statuses.
- Keep security behavior additive: profile verification should build on Phase 8 guards and redaction, not weaken them.
- Prefer focused unit tests first, then broaden around worker paths and compliance.

### Integration Points
- Generated plan payloads from project configs must connect to worker verification settings.
- Local and remote worker execution should resolve profile-native and CLI verification commands through one shared helper or equivalent shared behavior.
- Compliance evidence should read concrete implementation signals and distinguish profile-native verification from ad hoc CLI verification.

</code_context>

<specifics>
## Specific Ideas

- Canonical backlog source: `docs/superpowers/plans/2026-05-07-doc-pack-alignment-roadmap.md`, Task 4.
- Roadmap success criteria:
  1. Profile verification commands flow into worker execution without replacing explicit CLI verification commands.
  2. Required verification failures block normal `DONE`.
  3. Compliance report can distinguish profile-native verification from ad hoc CLI verification.

</specifics>

<deferred>
## Deferred Ideas

- CI polling and bounded repair loops belong to Phase 11.
- Rollback behavior belongs to Phase 10.
- Governance and semantic-memory target status belong to Phase 12.

</deferred>
