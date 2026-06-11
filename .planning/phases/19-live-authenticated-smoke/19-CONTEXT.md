# Phase 19: Live Authenticated Smoke - Context

**Gathered:** 2026-06-12
**Status:** Ready for planning
**Mode:** Smart discuss (autonomous) — all recommendations accepted by user

<domain>
## Phase Boundary

Operators can validate the Jira and GitLab integrations on a real machine with real credentials
via `whilly jira smoke` and `whilly gitlab smoke`, with every run leaving a persisted, redacted
report and actionable failure hints. Covers LIVE-01, LIVE-02, LIVE-03. Strictly read-only against
external systems. No WUI surface — CLI + report file only (roadmap "UI hint" judged a false
positive).

</domain>

<decisions>
## Implementation Decisions

### Command surface & invocation
- `whilly jira smoke` as a new ACTION in the existing `whilly jira` subparser; new `whilly gitlab`
  CLI group with a `smoke` action.
- Jira checks reuse the poll-cycle code paths against an operator-supplied `--issue KEY`:
  auth/whoami, issue fetch, comments, changelog, remote links, classify.
- Strictly read-only — no comments posted, no transitions, safe against production Jira.
- Credentials use the existing resolution chain (JIRA_SERVER_URL / JIRA_USERNAME / JIRA_API_TOKEN
  env → config → interactive prompt, as in `whilly/cli/jira.py`); GitLab analogous
  (GITLAB_URL / GITLAB_TOKEN).

### Report & evidence
- JSON report plus human-readable stdout summary (mirrors the `migration-chain-evidence.json`
  pattern from Phase 18).
- Reports written to `whilly_logs/smoke/{jira|gitlab}-smoke-{timestamp}.json`.
- Content: per-check pass/fail, duration, redacted target info (server host, project key, repo
  path). Tokens/secrets are never written — redaction is a hard requirement.
- DB audit event appended only when WHILLY_DATABASE_URL is set; the report file is primary and
  smoke must not hard-require a database.

### Failure UX & docs
- Exit codes: 0 = all checks pass, 1 = one or more checks failed, 2 = configuration missing.
- Each failed check prints what the operator should verify (credentials, project key, repo path) —
  not a raw exception.
- Documentation: new "Live smoke" section in `docs/Whilly-Usage.md` with setup steps.
- Tests: unit tests with mocked HTTP for check logic and failure hints; the live authenticated run
  is manual UAT (real credentials cannot run in CI).
- GitLab scope: token-authenticated API check plus link-refresh/repo-hint validation against a
  real repository URL.

### Claude's Discretion
- Internal module layout for the smoke checks (e.g., a shared smoke-report helper reused by both
  commands).
- Exact JSON schema field names; timestamp format; stdout formatting.
- How classify is invoked read-only against the fetched issue.

</decisions>

<code_context>
## Existing Code Insights

### Reusable Assets
- `whilly/cli/jira.py` — existing `whilly jira` subparser (import/intake/classify/readiness/poll/
  tui), credential state machine (`_jira_config_state`, `_missing_jira_settings`, interactive
  prompt fallback, `JIRA_AUTH_SCHEME` basic/PAT support), exit-code constants
  (`EXIT_OK`, `EXIT_VALIDATION_ERROR`).
- `whilly jira poll` — one-shot refresh cycle: issue, comments, changelog, remote links, repo
  hints; `--persist` requires WHILLY_DATABASE_URL (pattern for optional DB).
- `whilly/jira_work.py` — intentionally read-only classification logic (docstring guarantees no
  Jira/GitLab/git calls).
- `whilly/external_integrations.py` — `JiraIntegration.is_available()` pattern.
- Phase 18's evidence-file pattern: honest per-check result accumulation, repo-root anchored
  paths, secret-free content.

### Established Patterns
- CLI groups registered in `whilly/cli/__main__.py`; each group module exposes a parser builder.
- `whilly_logs/` is the established log/evidence directory.
- Tests: unit tests mock subprocess/HTTP boundaries (see update-check tests from Phase 13.1);
  integration tests gated on external availability with graceful skip.

### Integration Points
- New `whilly gitlab` group goes alongside existing CLI groups; `smoke` actions plug into the
  existing argparse ACTION pattern.
- Repo-hint / remote-link code used by `jira poll` is the code path GitLab smoke must exercise.
- Docs regression tests exist for docs/Whilly-Usage.md (Phase 16 pattern) — keep them passing.

</code_context>

<specifics>
## Specific Ideas

Success criteria from ROADMAP (must be TRUE):
1. Documented setup → `whilly jira smoke` → pass/fail against a real Jira project with classify,
   history, comments, link checks exercised.
2. `whilly gitlab smoke` → pass/fail against a real repository with link-refresh and repo-hint
   checks exercised.
3. Each run writes a persisted report file the operator can read afterwards.
4. Failure messages identify which check failed and what to verify — never a raw traceback.

</specifics>

<deferred>
## Deferred Ideas

- Record/replay HTTP cassettes for CI execution of smoke logic (manual UAT chosen instead).
- Write-path smoke checks (comment post / transition) — excluded to keep smoke production-safe.

</deferred>
