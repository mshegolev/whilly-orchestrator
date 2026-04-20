# ADR-018 — Smart task routing (classify + match + route)

- **Status:** Accepted
- **Date:** 2026-04-20
- **Domain:** inbox automation / hierarchy plumbing

## Context

After ADR-017 landed the 3-level hierarchy model, whilly still had no
opinion on *where new input should land*. Every issue/idea arriving
from a user, a webhook, or an inbox-review session required manual
routing:

* "Is this an Epic, Story, or Task?"
* "Is there already an open parent this belongs under?"
* "Should we link it, create a new parent, or flag for human review?"

Doing this by hand for every inbound item burns a surprising amount of
attention. A repo of ~80 inbox items easily consumes 2 hours of
triage per week. Whilly should handle the routine case and hand the
edge cases to a human with enough context to decide fast.

## Decision

Introduce `whilly/classifier/` with the following surface:

1. **`TaskClassifier` Protocol** — takes (title, body), returns
   `ClassificationResult` (level, confidence, complexity, estimated
   children, flags).
2. **Two classifier impls:**
   - `LLMClassifier` — primary path, one structured-JSON prompt to the
     active backend. Falls back to heuristic on any error (never raises).
   - `HeuristicClassifier` — length + regex keyword buckets. Confidence
     capped at 0.6 so it never auto-applies.
3. **`ParentMatcher` Protocol + `LLMParentMatcher` + `NoopParentMatcher`**
   — given candidates at parent level, rank them by fit (0.0-1.0).
4. **`Router`** — composes classifier + matcher + adapter into a single
   decision. Output is `RoutingDecision` with one of four actions:
   `LINK_AS_CHILD`, `CREATE_ORPHAN`, `PROMOTE_DRAFT`, `REJECT`.
5. **Thresholds are opinionated:**
   - `match_threshold = 0.55` — below this, we orphan rather than link.
   - `classify_threshold = 0.6` — for flagging low-confidence decisions
     (doesn't block routing, just surfaces a warning).
   - `is_high_confidence` = 0.75 — the CLI's `--apply` gate.
6. **CLI:** `whilly --classify "TITLE | BODY" --project URL --repo X/Y
   [--apply]`. Without `--apply` it prints the decision and exits —
   the dry-run is the default for a reason (routing decisions are cheap
   to re-run, expensive to un-link).

Decisions are plain data (`RoutingDecision`); executing them goes through
`HierarchyAdapter.create_child` / `promote`. Classifier never touches
the tracker. This lets tests mock the tracker and exercise the router's
decision tree without any network.

## Considered alternatives

### A. Single LLM call: "classify AND match in one prompt"
Rejected. Two calls are cheaper at scale because the matcher prompt
needs N candidate summaries — classification doesn't. Bundling forces
every classification to haul the candidate list through. Separation
also means the classifier can run against an inbox where parents are
still being discovered (first-pass classify, second-pass match once
parents are confirmed).

### B. Embedding-based matching
Considered. Would need a new dep (sentence-transformers or an API), a
corpus cache, and re-embedding on every candidate change. LLM
prompt-based matching works today with our existing backends, is
explainable (the model writes a reason), and performs well on the
hundreds-of-items scale we care about. Revisit when a user hits a
thousands-of-items pain point.

### C. Always apply the top match with no human gate
Rejected. False-link is expensive to undo — a Task landing under the
wrong Story pollutes both parents' progress metrics and confuses
reviewers. Confidence threshold + dry-run-by-default is the minimum
viable safety net.

### D. Integrate routing into the TRIZ+PRD pipeline silently
Rejected. The pipeline today expects already-routed input. Routing is
upstream plumbing — mixing them in one script couples two concerns
whose costs differ by 10x (routing: pennies per item; pipeline: dollars).

### E. Heuristic-only, no LLM
Rejected as default. Keyword rules work on short simple inputs but fall
apart on nuanced text. Kept as a fallback for CI / no-key environments
and as the LLM classifier's safety net.

### F. Classifier reads the whole project state as context
Tempting (better classification when the model knows what else is on
the board), but expensive (prompts balloon) and brittle (schema changes
force prompt refactors). The matcher already provides "here are the
relevant neighbours" on a per-call basis — that's enough.

## Consequences

### Positive
- Inbox triage time drops by ~80% for well-formed input (one command
  instead of three clicks).
- Low-confidence items are flagged explicitly — human still owns the
  ambiguous cases.
- Router is tracker-agnostic — Jira/Linear adapters plug into the same
  router without touching classifier code.
- Heuristic fallback ensures CI paths keep working even without an
  API key.

### Negative
- Every routing call costs ~2-3k tokens (~pennies at haiku pricing).
  Mitigation: router is invoked explicitly; the pipeline doesn't
  re-classify on every run.
- Prompts are in Russian (team default) — English-only projects will
  need translation. Tracked as a future follow-up.
- Threshold tuning is currently static. Real-world accuracy data
  might push `match_threshold` one way or the other; we'll learn from
  actual use.
- `PROMOTE_DRAFT` action is defined in the Protocol but not yet wired
  in — needed when the Epic→Stories pipeline lands (Phase 2).

### Follow-ups
- **Webhook-triggered classification** — GitHub Actions that fires on
  `issues.opened` and routes automatically (with the same thresholds).
- **Inbox sweeper** — `whilly --classify-inbox URL` that walks every
  open unparented item and surfaces routing decisions in bulk.
- **English prompts** — optional second prompt variant for
  non-Russian teams.
- **Duplicate detection** — extend the flags vocabulary ("duplicate-
  suspected" already exists) and wire it to `gh issue view`-style
  nearest-neighbour search.
- **Learning loop** — when a human overrides a decision via the UI,
  log the override as training signal for later threshold tuning.

## References

- `whilly/classifier/base.py` — Protocol + dataclasses.
- `whilly/classifier/llm.py` — LLM classifier.
- `whilly/classifier/heuristic.py` — keyword-based fallback.
- `whilly/classifier/matcher.py` — LLM / noop matchers.
- `whilly/classifier/router.py` — `Router` + `format_decision`.
- `tests/test_classifier.py` — 30 tests.
- ADR-013 — `AgentBackend` (the LLM transport used here).
- ADR-017 — `HierarchyAdapter` (the tracker surface router targets).
