# Phase 33: Known-Drift Fixture Validation - Context

**Gathered:** 2026-06-19
**Status:** Ready for planning
**Mode:** Auto-resolved (autonomous smart-discuss — orchestrator authored the milestone)

<domain>
## Phase Boundary

Prove the detection engine is trustworthy, not just plausible: a known-drift
fixture (a deliberately drifted spec/code pair + a clean control pair) the engine
flags as HIGH on the drifted case and clean on the control. This is the
acceptance test for the whole mechanism.

Maps VALID-01. Final phase of v1.5. Still scripts/tests/fixtures/docs only — zero
whilly/ change.
</domain>

<decisions>
## Implementation Decisions (auto-resolved)

### Two-layer validation — load-bearing
The engine's "detection" is the LLM's judgment, which is non-deterministic and
cannot be unit-tested for content. So VALID-01 is satisfied at TWO layers:

1. **Deterministic plumbing test (CI-green, offline):** feed the fixture pair
   through the real `review_spec`/`run_fleet` pipeline with a SCRIPTED reviewer
   that returns a HIGH finding for the drifted fixture and `[]` for the control.
   Asserts the harness + assertion logic correctly classify "detected HIGH" vs
   "clean". This always runs offline (no network), proving the validation
   *mechanism* itself works.

2. **Live acceptance test (real model, skip without `claude`):** run the REAL
   `claude_reviewer` against the fixtures and assert ≥1 HIGH on the drifted
   fixture AND zero HIGH on the control. This is the genuine trustworthiness
   proof. Marked skip-when-`shutil.which("claude")`-is-None (repo convention),
   so it runs in the scheduled CI (which has the key) as a canary and locally
   when `claude` is present.

### The fixture: an obvious, self-contained planted contradiction
Under `tests/fixtures/semantic_drift/`:
- `drifted/` — a tiny capability `spec.md` whose SHALL/MUST makes a concrete,
  checkable claim that the paired module source plainly violates (e.g. spec:
  "the function SHALL return a JSON object"; code: returns a plain string / XML).
  The contradiction must be unambiguous so a competent reviewer flags it HIGH.
- `clean/` — a spec whose SHALL/MUST exactly matches its paired module.
Each fixture is self-contained (own spec + module + a tiny matrix snippet or a
direct module list) so it does not depend on the real openspec/specs or
COVERAGE-MATRIX.

### Reuse existing seams
review_spec already takes injectable reviewer + specs_root/repo_root/matrix_path,
so the fixture can be pointed at via those params — no new production code needed
beyond a thin validation helper if convenient. Prefer adding the fixture + tests
over new script surface; if a small `validate_fixture()` helper aids the live
canary, keep it in scripts/ and pure-ish.

### Reproducibility (ties to VALID-01 "demonstrably trustworthy")
Document the fixture + expected verdict (drifted→HIGH, clean→clean) so the
validation is reproducible: a README in the fixture dir stating the planted
contradiction and the expected result.
</decisions>

<code_context>
## Existing Code Insights

- `scripts/semantic_drift_check.py`: `review_spec(slug, reviewer, *, specs_root, repo_root, matrix_path)`, `claude_reviewer`, `parse_findings`, `SEVERITIES` ("HIGH" = SEVERITIES[0]).
- `tests/test_semantic_drift_check.py`: scaffold helpers (`_scaffold_repo`/`_write_spec`/`_write_matrix`), injected-fake-reviewer pattern, and the `shutil.which("claude")` live-skip pattern — all reusable for both validation layers.
- Phase 31 `run_fleet` could host the fixture pair as a mini-fleet, but a direct
  per-fixture `review_spec` call is simpler for the assertion.
</code_context>

<specifics>
## Specific Ideas

- Fixtures live under `tests/fixtures/semantic_drift/{drifted,clean}/`.
- Live test marked `@pytest.mark.skipif(shutil.which("claude") is None, ...)`.
- A fixture README documents the planted drift + expected verdict (reproducible).
- No gating change; this phase only validates.
</specifics>

<deferred>
## Deferred Ideas

- None — this is the final v1.5 phase. Post-milestone: auto-remediation, per-PR
  diff-scoped checks, trend dashboards (all deferred in REQUIREMENTS).
</deferred>
