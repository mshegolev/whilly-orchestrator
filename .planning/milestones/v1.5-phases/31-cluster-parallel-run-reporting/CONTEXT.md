# Phase 31: Cluster-Parallel Run & Reporting - Context

**Gathered:** 2026-06-19
**Status:** Ready for planning
**Mode:** Auto-resolved (autonomous smart-discuss — orchestrator authored the milestone)

<domain>
## Phase Boundary

Add the full-fleet runner on top of Phase 30's single-spec engine: one invocation
reviews ALL 32 capability specs via a bounded parallel fan-out grouped into the
proven 6 clusters, is resilient to per-unit failure, records run metadata for
reproducibility, and emits both a machine-readable findings artifact (JSON) and a
human summary with per-cluster tallies + coverage.

Maps RUN-01..03, REPORT-01..02. Still `scripts/`-only, zero `whilly/` change.
CI scheduling is Phase 32; the known-drift fixture is Phase 33 — NOT here.
</domain>

<decisions>
## Implementation Decisions (auto-resolved)

### Build on Phase 30 engine, same file
Extend `scripts/semantic_drift_check.py`. Reuse `review_spec(slug, reviewer=...)`
as the per-spec unit; the runner injects the reviewer so tests use a fake (no
network/CLI), identical to Phase 30.

### Cluster grouping: explicit constant, validated against live slugs (RUN-01)
Define a `CLUSTERS` constant (6 clusters → slug lists) mirroring the manual
audit's grouping (orchestration, prd-decision, integrations, operator-surface,
platform, safety-quality). A test asserts the partition is exhaustive and
disjoint against the live `openspec/specs/*` slug set (every slug in exactly one
cluster; no unknown slugs) — so the grouping cannot silently drift from the 32
specs. The set of specs to review is still derived live (filesystem / matrix),
clusters are the reporting/parallelism grouping.

### Parallelism: bounded thread pool, default 6 workers (RUN-01)
Use `concurrent.futures.ThreadPoolExecutor` (the unit of work is a blocking
subprocess to the Claude CLI, so threads are the right primitive). Default
`--max-workers 6` (one per cluster); configurable. Findings are sorted
deterministically (by slug, then severity) before output so runs are stable
modulo LLM nondeterminism.

### Resilience: per-unit isolation (RUN-02)
Each spec review runs in try/except. On exception (CLI error, parse failure,
missing spec) the runner records a structured per-unit error entry
`{slug, cluster, error}` and continues — a failed unit NEVER aborts the run.
Errors are surfaced in both artifact and summary.

### Run metadata: self-describing (RUN-03)
The run records: model (resolve from `WHILLY_MODEL`/`CLAUDE_*` env or a `--model`
flag, default documented), git commit (`git rev-parse HEAD`) + dirty flag
(`git status --porcelain` non-empty), timestamp (passed in / `datetime.now`),
and tool version. Stored in the artifact's `run` block.

### Outputs (REPORT-01, REPORT-02)
- Machine-readable: a JSON artifact `{run: {...metadata}, coverage: {reviewed, total: 32},
  clusters: {<cluster>: {high, medium, low, clean, error}}, findings: [...], errors: [...]}`.
  Path via `--output <path>` (default e.g. `semantic-drift-findings.json`).
- Human summary: printed to stdout (and/or markdown) — per-cluster H/M/L + clean
  tally table, coverage `reviewed/32`, confirmed-findings vs clean-specs split.
- Pure formatter functions (build the summary string from a results dict) so they
  are unit-testable without running the fleet.
</decisions>

<code_context>
## Existing Code Insights

- `scripts/semantic_drift_check.py` (Phase 30): `review_spec`, `resolve_modules_for_slug`,
  `build_review_prompt`, `parse_findings`/`validate_finding`, `claude_reviewer`,
  `FINDING_KEYS`, `main(--slug)`. The runner composes these.
- The 6-cluster grouping is the one the manual audit used (documented in v1.5
  REQUIREMENTS motivation). Mirror it.
- `tests/test_semantic_drift_check.py` (Phase 30, 20 tests): extend with runner
  tests using a fake reviewer + a fake/seam for git metadata.
</code_context>

<specifics>
## Specific Ideas

- New CLI mode: `--all` (full fleet) alongside existing `--slug` (single). `--all`
  and `--slug` mutually exclusive; one required.
- `--max-workers N` (default 6), `--output PATH` (JSON), `--model` (metadata).
- Keep exit code 0 at this phase (gating is Phase 32).
- Make git-metadata collection injectable/seam-able so tests don't depend on repo
  git state.
</specifics>

<deferred>
## Deferred Ideas

- Scheduled CI job + configurable gating posture (report-only vs fail-on-HIGH) → Phase 32.
- Known-drift fixture validation → Phase 33.
</deferred>
