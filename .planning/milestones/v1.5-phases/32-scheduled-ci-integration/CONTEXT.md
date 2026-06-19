# Phase 32: Scheduled CI Integration - Context

**Gathered:** 2026-06-19
**Status:** Ready for planning
**Mode:** Auto-resolved (autonomous smart-discuss — orchestrator authored the milestone)

<domain>
## Phase Boundary

Wire the Phase 31 fleet runner into a SCHEDULED CI job — separate from and
non-blocking the v1.4 per-PR mechanical gate — that runs the semantic check,
uploads the JSON artifact, renders the human summary, and applies a configurable
gating posture (report-only vs fail-on-HIGH).

Maps CI-01..02. Adds gating to the script + a new workflow file. The known-drift
fixture is Phase 33 — NOT here.
</domain>

<decisions>
## Implementation Decisions (auto-resolved)

### Separate scheduled workflow file (CI-01)
New `.github/workflows/semantic-drift.yml`, triggered ONLY by
`schedule` (weekly cron) + `workflow_dispatch`. NEVER `pull_request`/`push`, so
it is fully decoupled from the v1.4 per-PR gate in `ci.yml` (spec-validation,
coverage-audit) — those stay untouched and remain the per-PR mechanical gate.

### Gating posture lives in the script (CI-02)
Add a `--fail-on {none,high}` arg (default `none` = report-only). With `high`,
`main(--all)` exits 1 iff any finding has severity `HIGH`; per-unit `errors` do
NOT gate (an LLM/CLI hiccup must not look like a drift failure). Default stays
report-only so flaky LLM runs never block, and the existing `--slug` path is
unchanged (still exit 0). The workflow exposes the posture via a
`workflow_dispatch` input (default `report-only`) + cron default report-only.

### LLM in CI: explicit key + fail-fast
The check needs the Claude CLI + `ANTHROPIC_API_KEY`. The workflow installs the
Claude CLI (npm global) and passes `ANTHROPIC_API_KEY` from repo secrets. If the
secret is absent, the job FAILS FAST with a clear message (never silently
"passes" a check that did not run). Weekly cadence bounds the cost. Document the
required secret in the workflow + usage docs.

### Outputs in CI (REPORT reuse)
- Upload the JSON artifact via `actions/upload-artifact`.
- Render the Phase 31 human summary into `$GITHUB_STEP_SUMMARY` so findings are
  visible in the run without downloading the artifact.

### Testing
- Gating logic is unit-testable offline: `--fail-on high` → exit 1 when a HIGH
  finding is present (via injected fake reviewer / injected findings), else 0;
  `--fail-on none` → always 0; `errors`-only run with `high` → still 0.
- Workflow YAML validated structurally: valid YAML; triggered by
  `schedule`+`workflow_dispatch` only (assert NO `pull_request`/`push`);
  references `scripts/semantic_drift_check.py --all`; has artifact-upload +
  step-summary steps; documents the `ANTHROPIC_API_KEY` secret.
</decisions>

<code_context>
## Existing Code Insights

- `.github/workflows/ci.yml` — the v1.4 per-PR gate (`spec-validation`,
  `coverage-audit`). MIRROR its style (checkout, setup-python 3.12) but in a
  SEPARATE file; do NOT add the semantic job here (keep it off per-PR).
- `scripts/semantic_drift_check.py` — Phase 31 `run_fleet` / `build_artifact` /
  `format_summary` / `--all` CLI, `main()` currently returns 0. Add `--fail-on`.
- The Claude CLI install pattern: npm global (the binary is `claude`,
  `CLAUDE_BIN` honored). Key via `ANTHROPIC_API_KEY` secret.
</code_context>

<specifics>
## Specific Ideas

- `--fail-on` default `none`; only `none|high` accepted at this phase.
- Cron: weekly (e.g. Monday 06:00 UTC) — bounded cost; exact schedule is a detail.
- `workflow_dispatch` input `posture: report-only|fail-on-high` mapping to
  `--fail-on none|high`.
</specifics>

<deferred>
## Deferred Ideas

- Known-drift fixture validation → Phase 33.
- Per-PR (diff-scoped) semantic checking, trend dashboards → Future (deferred in REQUIREMENTS).
</deferred>
