# ADR-014 — Workflow board integration via `BoardSink` Protocol

- **Status:** Accepted
- **Date:** 2026-04-20
- **Domain:** integration / extensibility

## Context

Whilly needs to move project-board cards as issues flow through the pipeline
(ready → picked_up → in_review → done / refused / failed). Without this, the
board drifts from reality the minute whilly picks up an issue, and users have
to eyeball logs to know what's in flight.

Two obvious approaches:

1. **Hard-code GitHub Projects v2 calls** directly in `whilly_e2e_demo.py`
   (and every future agent script) — fastest path to "works for one user".
2. **Protocol abstraction** with a pluggable registry (like
   [ADR-013 / OC-103](../../README.md) did for agent backends) — up-front cost,
   but second tracker plugs in without touching callers.

The project's public position (README, BRD) already names Jira, Linear, GitLab
as future first-class integrations for team adoption. Choosing option 1 guarantees
a painful rewrite within 2-3 sessions; choosing option 2 is ~30% more code up front.

A third tension came from the **workflow mismatch problem**: a user's board
almost certainly doesn't have exactly the six columns whilly uses internally.
Forcing rename is hostile; ignoring gaps silently is invisible. We need a
proactive analyzer that detects gaps and proposes options.

## Decision

1. **Introduce `whilly/workflow/` package** with a `BoardSink` Protocol and
   a pluggable registry, mirroring the `whilly.agents.AgentBackend` design from
   OC-103 (same idioms for consistency).

2. **Ship one concrete adapter** — `GitHubProjectBoard` backed by
   `gh api graphql`. No new runtime deps.

3. **Canonical lifecycle vocabulary** as a `LifecycleEvent` str-Enum
   (6 core events) plus an extensible `register_event()` for custom
   stages (TRIZ challenge, PRD review, future workshop experiments).

4. **Analyzer produces a `GapReport`**, not a crash, when the live board
   misses columns. Matching is fuzzy (alias table + substring) and respects
   user overrides from `.whilly/workflow.json`.

5. **Interactive proposer** (`propose()`) walks gaps with three modes:
   `interactive` (TTY, default), `apply` (add all missing, CI-friendly),
   `report` (dry-run, no mutations). Output is `(Proposal, WorkflowMapping)`
   — the mapping is what gets persisted; the proposal is auditable context.

6. **Mapping persists as `.whilly/workflow.json`** — committable, so teams
   share one contract. Gitignored cwd JSON was rejected (the same argument
   that killed the first tactical design).

7. **CLI entry**: `whilly --workflow-analyze <URL> [--apply|--report]`.
   Standalone — doesn't touch the main run loop. Syncer integration is a
   separate decision (likely ADR-015 once we have the TRIZ+PRD pipeline
   emitting events).

## Considered alternatives

### A. Hard-coded GitHub calls inline
Rejected — duplicates move-card logic everywhere an agent is launched,
blocks Jira/Linear without rewriting every script, and mixes tracker
concerns into the agent runner.

### B. Plugin via entry points (setuptools)
Over-engineered for two boards. If third-party plugin authors appear, we
revisit — the registry pattern already leaves the door open (`_BOARD_REGISTRY`
is public-ish).

### C. Re-use `whilly.sinks.github_pr` namespace
Sink semantics are different: `github_pr` is *write-only* (open PR), boards
are *read + write*. Also, Jira has both sinks (JQL query to feed tasks +
transition mutation), so "sink" ≠ "source" ≠ "board" — three roles, three
packages is cleaner than conflating.

### D. Force users to rename board columns
Rejected on principle — whilly adapts to the user's workflow, not the other
way around.

### E. Event-driven syncer (background thread tailing `whilly_events.jsonl`)
Deferred to ADR-015. It's the *right* end-state for self-hosting + CI, but
ships independently of the analyzer/proposer work. The Protocol is designed
so a syncer can plug in without any interface change.

## Consequences

### Positive
- Jira / Linear / GitLab adapters are `BoardSink` impls — single file each.
- Analyzer is read-only → safe to run in CI with a minimal-scope token.
- Mapping as committed artefact → team-wide consistency, reviewable diffs.
- Custom lifecycle events (TRIZ, PRD) extend without whilly-core changes.

### Negative
- +30% code up front vs. the tactical inline version.
- `gh api graphql` only — a non-`gh` auth path would require a separate PR
  (e.g., token via `requests` for containerised environments).
- `add_status` for GitHub is currently stubbed to raise `NotImplementedError`
  because the v2 schema for programmatic column creation needs verification —
  interactive "A" falls back to "map or skip" in that case. To be closed in
  a follow-up before claiming full parity with "interactive add".

### Follow-ups
- **ADR-015** — Event-driven `ProjectBoardSyncer` tailing `whilly_events.jsonl`.
- **Second adapter** — `JiraBoard` or `LinearBoard` to validate the Protocol
  surface at ~1-2 adapter-weeks each.
- **Multi-repo routing** — when a Project aggregates issues from several
  repos, whilly needs per-repo worktree dispatching.
- **Close GitHub `add_status` NotImplemented** once the `updateProjectV2Field`
  semantics are nailed down.

## References

- `whilly/workflow/base.py` — the Protocol and dataclasses.
- `whilly/workflow/github.py` — reference adapter.
- `tests/test_workflow_*.py` — 73 tests pinning contract.
- ADR-013 context: agent backends (OC-103) — the idiom reused here.
