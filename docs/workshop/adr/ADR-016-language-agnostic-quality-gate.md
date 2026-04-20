# ADR-016 — Language-agnostic quality gate (`QualityGate` Protocol)

- **Status:** Accepted
- **Date:** 2026-04-20
- **Domain:** self-hosting pipeline / extensibility

## Context

The TRIZ+PRD pipeline (ADR-015) ran `pytest` + `ruff check` + `ruff format
--check` inline before opening a PR. That hard-codes the project to Python
tooling, which makes the pipeline unusable for:

* Node / TypeScript projects (vitest + eslint + prettier)
* Go projects (go test + go vet + gofmt)
* Rust projects (cargo test + cargo clippy + cargo fmt)
* Polyglot monorepos (Python core + Node tooling sidecar — both must pass)
* "Docs-only" repos (nothing to check — current pipeline would error trying
  to run pytest against a README)

Three ways out:

1. **Shell-config**: `.whilly/quality.yaml` with raw commands. Fast to ship,
   but we'd reinvent detection, timeout, binary-missing semantics per repo.
2. **Delegate to CI**: open the PR, let GitHub Actions decide. Fails the
   "whilly never opens a PR it wouldn't accept from a human contributor"
   commitment in ADR-015 — the signal arrives too late.
3. **Protocol per language**: same idiom as `AgentBackend` (OC-103) and
   `BoardSink` (ADR-014). One module per language, plug new ones in
   sibling-style, auto-detect from marker files.

## Decision

Option 3. Introduce `whilly/quality/` with a `QualityGate` Protocol and
four concrete impls (Python, Node, Go, Rust) covering ~95% of self-hosting
projects whilly will encounter in its first year.

Public surface:

```python
from whilly.quality import detect_gates, run_detected, get_gate

# one-shot: detect + run + aggregate
result = run_detected(Path.cwd())   # GateResult

# introspection
gates = detect_gates(Path.cwd())    # list[QualityGate]

# explicit resolution (tests, CLI flags)
gate = get_gate("python")           # QualityGate
```

Protocol surface:

```python
class QualityGate(Protocol):
    kind: str                              # "python" | "node" | ...
    def detect(self, cwd: Path) -> bool:   # marker-file check
    def run(self, cwd: Path) -> GateResult # executes stages, never raises
```

Dataclasses:

- `StageResult(name, passed, summary, duration_s)` — one tool invocation.
- `GateResult(gate_kind, passed, summary, stages)` — aggregate for one
  language. `summary` is pre-rendered Markdown-friendly text.

Shared helper `whilly.quality._runner.run_stage` handles subprocess plumbing
uniformly: PATH pre-check, timeout, stdout truncation (keeps the tail —
where error messages live).

## Considered alternatives

### A. `.whilly/quality.yaml` with raw shell commands
```yaml
stages:
  - name: tests
    cmd: ["pytest", "-q"]
  - name: lint
    cmd: ["ruff", "check", "."]
```
Rejected. It shifts detection and validation onto the user — every repo
re-learns "pytest doesn't exit 5 on empty collection", "gofmt's output
means dirty files", etc. Per-language impls encode this once.

### B. Delegate to CI (`gh workflow run` after PR opens)
Rejected. The whole point of the gate is to not open bad PRs. Moving
the check to CI means reviewers waste time on PRs that were never going
to pass — matches the behaviour the pipeline was built to avoid.

### C. One mega-gate that sniffs + hardcodes every language
Rejected. Forces every language's logic into a single file that becomes a
merge hotspot. The Protocol means Node developers add/edit node.py and
never touch python.py.

### D. Make stages configurable inside each impl (e.g., `PythonQualityGate(tools=["pytest"])`)
Deferred. Needed eventually for projects with custom tooling, but v1
ships opinionated defaults. Once a real project needs to override, we
add a `stages` constructor kwarg — no Protocol change required.

### E. Single "binary stages" abstraction
We briefly considered modelling every tool as a plain `(name, argv)`
pair and dropping the per-language classes. Killed it because language-
specific logic (Node reads package.json scripts; Go's gofmt needs
"non-empty stdout = fail" inverted semantics) doesn't flatten cleanly.

## Consequences

### Positive
- Pipeline works on Python, Node, Go, Rust out of the box (~95% of self-
  hosting use cases).
- New language = one 50-line module. Onboarding a contributor to support
  "Kotlin quality gate" is a weekend task, not an architecture review.
- Polyglot monorepos work: `detect_gates` returns every applicable gate,
  `run_all` aggregates. Tested — see `TestDetection.test_detect_gates_composes`.
- "Nothing detected" is a pass, not an error — docs-only / assets-only
  repos don't break the pipeline.
- Registry pattern mirrors `whilly.agents` / `whilly.workflow` — anyone
  who's touched those can add a gate without re-learning an idiom.

### Negative
- Each language is committed to a specific toolchain. A Python repo using
  `unittest` + `flake8` (not pytest + ruff) gets a failing gate until we
  add tool-switchable constructor args (Option D above) or they add those
  tools.
- Stage output truncation is fixed at 2000 chars — noisy toolchains
  (full stack traces, verbose benchmarks) lose context. Acceptable for PR
  body rendering; heavier users can re-run locally.
- Node gate reads `package.json.scripts` rather than running the tools
  directly — repos with tests-but-no-npm-script don't get checked. We
  judged this the right default (half of Node projects use different
  runners).

### Follow-ups
- **Per-language config overrides.** `PythonQualityGate(tools=[...])` for
  teams that use different tooling. Small, non-breaking.
- **Docker quality gates.** A `DockerQualityGate` that runs stages inside
  a container image specified by the repo (`.whilly/quality.dockerfile`).
  Handles the "Python 3.10 vs 3.12", "Node 18 vs 20" matrix without
  requiring every runner to have every version installed.
- **`whilly --quality-check` CLI** — standalone invocation of the gate
  without a full pipeline run; useful as a pre-commit hook.
- **Multi-language richer summary.** For polyglot repos the aggregated
  summary is just concatenation; a structured "per-language" table
  would render better in PR bodies.

## References

- `whilly/quality/__init__.py` — package surface + registry.
- `whilly/quality/base.py` — Protocol + dataclasses.
- `whilly/quality/{python,node,go,rust}.py` — concrete impls.
- `whilly/quality/multi.py` — composite for polyglot repos.
- `tests/test_quality_gate.py` — 30 tests pinning the contract.
- ADR-013 — agent backends (the idiom).
- ADR-014 — workflow BoardSink (the idiom, again).
- ADR-015 — self-hosting pipeline (the caller).
