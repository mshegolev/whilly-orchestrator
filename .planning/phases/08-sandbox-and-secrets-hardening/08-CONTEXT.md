# Phase 8 Context: Sandbox and Secrets Hardening

## Goal

Implement the `a3-a4-sandbox-and-secrets-lint` hardening slice from
`docs/CODEX-MISSION.md` without claiming full VM/container isolation. This phase strengthens guards
around secrets, task-authored command surfaces, runner environments, and audit evidence.

## Canonical References

- `.planning/ROADMAP.md`
- `.planning/REQUIREMENTS.md`
- `.planning/ROADMAP-ANALYSIS.md`
- `docs/CODEX-MISSION.md`
- `docs/superpowers/plans/2026-05-07-doc-pack-alignment-roadmap.md`
- `whilly/security/prompt_sanitizer.py`
- `whilly/core/agent_runner.py`
- `whilly/core/prompts.py`
- `whilly/worker/local.py`
- `whilly/worker/remote.py`
- `whilly/pipeline/verification.py`
- `whilly/adapters/runner/proxy.py`
- `whilly/adapters/runner/claude_cli.py`
- `whilly/compliance/__init__.py`
- `tests/unit/test_prompt_sanitizer.py`
- `tests/unit/test_prompt_sanitizer_wiring.py`
- `tests/unit/test_local_worker.py`
- `tests/unit/test_remote_worker.py`
- `tests/unit/test_verification_runner.py`

## Requirements

- SEC-01: Secret linting covers task descriptions, comments, config values, runner prompts, and
  external feedback.
- SEC-02: Runner environments are scrubbed to an explicit allowlist plus configured required tokens.
- SEC-03: Command and prompt guard failures emit auditable reasons.

## Success Criteria

1. Secret linting covers task descriptions, comments, config values, runner prompts, and external
   feedback.
2. Runner environments use explicit allowlists plus configured required tokens.
3. Blocked work emits auditable reasons.
4. Docs and compliance evidence clearly state residual sandbox risk instead of full isolation.

## Implementation Decisions

### Scope

- Treat Phase 8 as a guard-and-evidence hardening slice, not a full sandbox backend.
- Create a reusable pure secret-lint module if needed instead of duplicating regexes across prompt,
  PR, config, and command guard code.
- Preserve existing prompt-injection guard and shell deny-list behavior; extend coverage and tests
  rather than replacing the current path.
- Keep full VM/container isolation as a documented residual risk. Do not claim isolation unless an
  actual per-task isolation backend is added in a later phase.

### Secret linting

- Existing `whilly/security/prompt_sanitizer.py` already redacts common AWS, GitHub, Slack, and
  OpenAI token shapes in sanitized external text and title slots.
- Existing prompt deny scanning blocks only the task description. Acceptance criteria, test steps,
  PRD requirements, PR review comments, and diffs are fenced/redacted but are not currently treated
  as blocking prompt-injection attempts.
- Phase 8 should make that secret-pattern contract reusable for:
  - task descriptions, acceptance criteria, test steps, and PRD requirement text,
  - external issue/PR comments and feedback,
  - runner prompt text before subprocess invocation,
  - config-like string values that may accidentally contain plaintext secrets.
- Guard outputs must use redacted excerpts and stable reason/pattern identifiers. Do not persist raw
  secrets in events, logs, or test fixtures beyond synthetic fake tokens.

### Runner environment allowlist

- `whilly/pipeline/verification.py` already runs verification commands with an explicit
  `env_allowlist`.
- The Claude runner environment is separate from verification and still needs direct coverage:
  `whilly/adapters/runner/proxy.py` and `whilly/adapters/runner/claude_cli.py` should be inspected
  before implementation because proxy/model-provider credential forwarding can otherwise inherit too
  much from the parent process.
- Phase 8 should define the agent runner environment contract separately from verification: an
  explicit base allowlist plus configured required credential names that may be forwarded.
- The allowlist contract should be pure and testable; worker tests can assert injected hidden
  variables are not passed to the runner surface where the runner environment is controlled by
  Whilly.
- Preserve required credentials for configured model/provider execution; do not silently strip the
  keys the selected runner actually needs.

### Auditable guard failures

- Local and remote workers already emit prompt guard and shell guard failure paths before runner
  invocation.
- `ShellScanResult` supports warning results, but current worker and verification paths mostly treat
  blocking paths as the auditable surface. If warnings are preserved, tests should pin whether they
  are reported or intentionally non-blocking.
- Phase 8 should make secret-lint blocked work produce the same quality of audit evidence:
  deterministic event type, reason, pattern id, task id, plan id, and redacted excerpt.
- Blocked guard paths must fail or block before agent execution. The runner must not be called after
  a blocking secret/prompt/shell finding.

### Compliance and docs

- Update compliance evidence so sandbox/security rows describe concrete guards and residual risk.
- Do not turn the `Sandbox/VM isolation` row into a full `PASS` unless per-task VM/container
  isolation is actually implemented.
- It is acceptable for compliance to say command/prompt/secret guards improved while full isolation
  remains partial/future.

## Existing Code Insights

### Existing hardening

- `whilly/core/prompts.py` raises `PromptInjectionBlocked` for prompt deny markers and workers fail
  tasks with a `prompt_injection_blocked` prelude event.
- `whilly/core/agent_runner.py` scans shell-command surfaces for destructive patterns and returns a
  redacted excerpt; local and remote workers fail before runner invocation on blocked scans.
- `whilly/security/prompt_sanitizer.py` fences untrusted text, redacts several secret patterns,
  strips dangerous control bytes, neutralizes closing fences, and caps length.
- `whilly/pipeline/verification.py` already uses explicit env allowlists and shell-command scanning
  for verification commands.
- Verification command/stdout/stderr details are audit payloads; Phase 8 should redact detected
  secrets from persisted verification details.

### Likely integration points

- Secret lint module: `whilly/security/secret_lint.py`.
- Shared sanitizer/secret patterns: `whilly/security/prompt_sanitizer.py`.
- Command/task guard surfaces: `whilly/core/agent_runner.py`.
- Runner env scrub: `whilly/adapters/runner/proxy.py` and `whilly/adapters/runner/claude_cli.py`.
- Local worker block path: `whilly/worker/local.py`.
- Remote worker block path: `whilly/worker/remote.py`.
- Verification event/detail redaction: `whilly/pipeline/verification.py`.
- Compliance evidence: `whilly/compliance/__init__.py`.
- Tests: new `tests/unit/test_secret_lint.py`, prompt sanitizer/unit wiring, local worker, remote
  worker, verification runner, runner env tests, and compliance report.

## Deferred Ideas

- Full per-task VM/container isolation backend is explicitly deferred.
- A rich policy engine for governance risk scoring belongs to Phase 12.
- Profile-native verification wiring belongs to Phase 9 and should build on Phase 8 env/guard
  contracts instead of being bundled here.

---
*Phase: 08-sandbox-and-secrets-hardening*
*Context gathered: 2026-05-08*
