# ADR-013 — OpenCode backend support (alongside Claude CLI)

- **Status:** accepted (Phase 1 of OPENCODE-DOCKER plan)
- **Date:** 2026-04-20
- **Deciders:** project author
- **Domain:** agent backend integration
- **Relates to:** ADR-004 (Claude CLI subprocess vs SDK) — extended, not superseded

## Context

ADR-004 fixed «Claude CLI subprocess» as the only agent backend. Twelve months later three pressures push us to add a second:

1. **Vendor independence.** A single-provider backend leaves us exposed to pricing changes (Anthropic 2026 pricing notwithstanding) and to deprecation cycles. Workshop participants explicitly asked for a non-Anthropic option.
2. **OpenCode's emergence.** `sst/opencode` reached ~95K-140K stars by April 2026 — clear leader among open-source coding-agent CLIs. It supports MCP, has an analogous `--dangerously-skip-permissions` flag, ships custom agents, exposes a headless server (`opencode serve`) attractive for Docker.
3. **Workshop demo value.** Showing whilly run with two different backends side-by-side («one repo, two AI brains») strengthens the BRD §4 G5 (vendor independence) narrative.

Question: replace Claude or sit alongside?

## Decision

**Add OpenCode as a co-equal, opt-in backend behind a stable Protocol. Claude remains the default.**

Concretely:

- New package `whilly/agents/` with `AgentBackend` Protocol (`base.py`).
- `whilly/agents/claude.py` — refactored extraction of the existing logic from `whilly/agent_runner.py`. Behaviour-preserving.
- `whilly/agents/opencode.py` — new `OpenCodeBackend` implementing the Protocol.
- Factory `whilly.agents.get_backend(name)` validates the choice and instantiates.
- Selection via env `WHILLY_AGENT_BACKEND={claude,opencode}` (default `claude`); CLI flag `--agent {claude,opencode}` will mirror the env in a follow-up commit (gated on resolving pre-existing uncommitted `cli.py` changes).
- `whilly/agent_runner.py` will become a thin compat-shim re-exporting `AgentResult` / `AgentUsage` / `run_agent` so all existing imports keep working — also a follow-up commit (same gating reason).

## Considered alternatives

### A. Replace Claude with OpenCode

- ✅ Simpler module layout, one less moving part.
- ❌ Breaks every existing user who relies on Claude defaults — a forced migration.
- ❌ Loses parity with `~/.claude/CLAUDE.md`, hooks, MCP servers many users already rely on.
- ❌ Workshop demo strength «we support both» disappears.

### B. Keep Claude only, add OpenCode later

- ✅ Zero scope creep.
- ❌ Doesn't address vendor lock-in concern raised in BRD §4 G5.
- ❌ Workshop hour 5 demo «pluggable backends» stays theoretical.

### C. Co-equal backends behind a Protocol (chosen)

- ✅ User chooses per-run — `--agent` flag or env var.
- ✅ Workshop demo: «same plan, two backends» — strong narrative.
- ✅ Backwards-compatible — existing `whilly` invocations behave identically.
- ✅ Adding a third backend (Codex, Aider, …) becomes a single-file addition.
- ❌ More code (one extra module per backend) — accepted, modules stay small (~250 LOC each).
- ❌ Two parsers to maintain instead of one.

## Decision details

### Protocol surface (`whilly/agents/base.py`)

```
class AgentBackend(Protocol):
    name: str
    def default_model() -> str: ...
    def normalize_model(model) -> str: ...
    def build_command(prompt, model=None, *, safe_mode=None) -> list[str]: ...
    def parse_output(raw) -> tuple[str, AgentUsage]: ...
    def is_complete(text) -> bool: ...
    def run(prompt, model=None, timeout=None, cwd=None) -> AgentResult: ...
    def run_async(prompt, model=None, log_file=None, cwd=None) -> Popen: ...
    def collect_result(proc, log_file=None, start_time=0) -> AgentResult: ...
    def collect_result_from_file(log_file, start_time=0) -> AgentResult: ...
```

`AgentResult` and `AgentUsage` move to `base.py`; `agent_runner.py` re-exports them for backward compatibility.

### Completion-marker contract — unchanged

Both backends recognise `<promise>COMPLETE</promise>` in the result text. The marker is **instructed via the system prompt**, so it is provider-agnostic — no LLM-specific code path needed.

### Permission model parity

Both backends default to `--dangerously-skip-permissions` (matches today's whilly behaviour for headless / tmux / Docker usage). Both honour an env-toggle for stricter policy:

- `WHILLY_CLAUDE_SAFE=1` → Claude uses `--permission-mode acceptEdits`.
- `WHILLY_OPENCODE_SAFE=1` → OpenCode omits the flag, falls back to `.opencode/opencode.json` policy. (See `examples/multi-container/.opencode/opencode.json` for the policy template — to be added in Phase 2.)

### Model id normalisation

OpenCode requires `provider/model` form (`anthropic/claude-opus-4-6`). `OpenCodeBackend.normalize_model()` auto-prefixes bare ids based on a small heuristic table:

| Bare id prefix | Auto-prefix |
|---|---|
| `claude*` | `anthropic` |
| `gpt*`, `o1*`, `o3*`, `o4*` | `openai` |
| `gemini*` | `google` |
| `llama*` | `meta` |
| `mistral*`, `deepseek*`, `qwen*` | matching provider |

Unknown bare ids pass through unchanged. The Claude `[1m]` extended-context suffix is stripped before lookup — OpenCode rejects the Claude-specific syntax.

### Output parsing differences

Claude CLI emits one final JSON summary at exit; OpenCode emits an event-stream (single object, top-level array, NDJSON, or mixed plaintext+blobs depending on version). `OpenCodeBackend.parse_output` handles all four shapes defensively — see `whilly/agents/opencode.py` and `tests/test_agent_backend_opencode.py` for the matrix.

Cost / token reporting in OpenCode JSON is under-documented; the parser falls back to `cost_usd=0.0` and emits a debug log when no recognisable cost field is found. Empirical fixtures captured during real runs (Phase 1 task OC-106) refine the parser as the schema firms up.

### Default backend

Stays **claude**. OpenCode is opt-in via `WHILLY_AGENT_BACKEND=opencode` or (after CLI wiring) `--agent opencode`. Decision recorded with explicit user confirmation during plan kickoff.

## Consequences

### Positive

- Vendor independence is now a code reality, not just a doc claim.
- Workshop participants can A/B their tasks across backends with one env change.
- Future backends (Codex, Aider, Cline) drop in as additional Protocol implementations — registry update only.
- Decision Gate (ADR-008) and tmux runner (ADR-003) automatically inherit the chosen backend through the factory once they're wired (follow-up).

### Negative

- Two parsers to maintain. Mitigation: shared dataclasses + a strict Protocol catch most drift early.
- Two CLI binaries to install in Docker images (Phase 2). Image size goes up by ~250 MB (node_modules of opencode-ai). Accepted in ADR-014.
- OpenCode's JSON schema is moving; expect occasional fixture refresh. Tracked under `tests/fixtures/opencode/` with documented capture procedure.

### Neutral

- Claude users see no change.
- OpenCode users opt in explicitly.
- `agent_runner.py` survives as a compat shim — no import paths break.

## References

- Plan document: `docs/workshop/PLAN-OPENCODE-DOCKER.md` (Phase 1 detail).
- `whilly/agents/base.py`, `claude.py`, `opencode.py`.
- `tests/test_agent_backend_claude.py`, `tests/test_agent_backend_opencode.py` — 70 unit tests across both backends.
- ADR-004 — original Claude-CLI choice; this ADR extends, not supersedes it.
- ADR-008 — Decision Gate, will use the same factory-resolved backend in a follow-up.
- ADR-014 (planned) — Docker packaging includes both binaries.
