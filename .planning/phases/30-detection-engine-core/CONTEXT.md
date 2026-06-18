# Phase 30: Detection Engine Core - Context

**Gathered:** 2026-06-19
**Status:** Ready for planning
**Mode:** Auto-resolved (autonomous smart-discuss — orchestrator authored the milestone)

<domain>
## Phase Boundary

Build the single-spec semantic drift detection engine: given one OpenSpec
capability slug, review that spec's `SHALL`/`MUST` requirements against the
`whilly/` modules it maps to, and emit triaged, evidence-backed findings.
Scope is ONE spec per invocation; the 32-spec parallel run is Phase 31.

Maps requirements DETECT-01..04.
</domain>

<decisions>
## Implementation Decisions (auto-resolved)

### Location: `scripts/`, NOT `whilly/` — load-bearing
The engine lives under `scripts/` (alongside `scripts/audit-coverage-matrix.py`),
NOT inside the `whilly/` package. Rationale: anything under `whilly/` is subject
to the coverage matrix + an `opsx` capability spec (per FORWARD-PROCESS). A
drift-checker living in `whilly/` would have to spec itself — recursive and
absurd. As repo tooling outside the package it is exempt (pure-tooling, like the
v1.4 audit script), so this milestone ships with zero `whilly/` behavior change
and no new spec obligation.

### LLM invocation: Claude CLI subprocess, dependency-injected
Reuse the Claude CLI the repo already depends on (`claude -p --output-format
json`, the same binary `whilly/adapters/runner/claude_cli.py` and the manual
audit used; honour `CLAUDE_BIN`). CRITICAL for testability: the engine takes the
"reviewer" as an injected callable `(prompt: str) -> str`. The default reviewer
shells to Claude; unit tests inject a fake returning canned JSON. No network in
tests; skip live-CLI tests when `claude` is absent (repo convention).

### Module review set: derived live from COVERAGE-MATRIX.md (DETECT-04)
Parse `openspec/COVERAGE-MATRIX.md` to resolve slug → mapped `whilly/` module
paths. Reuse/extend the matrix-parsing already in
`scripts/audit-coverage-matrix.py` — do NOT hand-maintain a second mapping.

### Findings schema (DETECT-02, DETECT-03)
Each finding is a dict:
`{severity: HIGH|MEDIUM|LOW, slug, requirement, drift (one line), evidence
(file:line), triage: code-bug|spec-overstatement, rationale}`.
The review prompt instructs the model to return a strict JSON array; parse with
a json-repair / fence-strip fallback (mirror `prd_generator` robustness). A spec
with no drift returns an empty array (clean).

### Prompt contract
Build a deterministic prompt embedding: the full spec.md, the mapped module
source (or excerpts when large), and explicit instructions to back every finding
with `file:line` and to classify code-bug vs spec-overstatement. This mirrors the
manual-audit agent prompts that produced the known findings.
</decisions>

<code_context>
## Existing Code Insights

- `scripts/audit-coverage-matrix.py` — matrix parser + the `find whilly/` module
  enumeration; reuse its COVERAGE-MATRIX.md parsing.
- `whilly/adapters/runner/claude_cli.py` — reference for invoking the Claude CLI
  (binary resolution, `--output-format json`, timeout/error handling). The engine
  needs only a thin subprocess call, not the full retry stack.
- `whilly/prd_generator.py` — reference for robust JSON extraction from model
  output (fence-strip + json_repair fallback).
- The recent manual audit (6 clusters, evidence-backed findings) is the behavioral
  target: the engine should reproduce that finding quality for a single spec.
</code_context>

<specifics>
## Specific Ideas

- A `--slug <capability>` CLI entry that prints the findings JSON for one spec.
- Keep the LLM-facing prompt builder a pure function (testable without a model).
- Exit code 0 always at this phase (reporting), severity/gating is Phase 32.
</specifics>

<deferred>
## Deferred Ideas

- Parallel fan-out over all 32 specs + run metadata → Phase 31.
- Machine artifact + human summary format → Phase 31.
- CI scheduling + gating posture → Phase 32.
- Known-drift fixture validation → Phase 33.
</deferred>
