# ADR-020 — Epic inference from orphan Stories

- **Status:** Accepted
- **Date:** 2026-04-20
- **Domain:** hierarchy reconstruction / inbox automation

## Context

ADR-019's `rebuild_hierarchy` reconstructs an Epic/Story/Task tree from a
flat list, but it can only assign Stories to Epics that already exist.
When the input has no Epics at all (common after a freshly-imported
project, or a "Stories-only" board that never used Epics), every Story
lands in ``unparented`` — the rebuild gives up rather than inventing
structure.

Real users want the next step: "look at my orphan Stories and *propose*
Epics that would group them." That's a **synthesis** task — it needs to
see all the orphan Stories at once, spot semantic clusters, and produce
Epic titles + short bodies that are sensible as parents.

## Decision

Add `whilly/classifier/epic_inferrer.py` with:

1. `InferredEpic` dataclass — proposal object (`title`, `body`,
   `child_story_ids`, `confidence`, `reasoning`, `applied`).
2. `infer_epics(orphan_stories, …) -> list[InferredEpic]` — one LLM
   call that clusters the input and returns the proposals.
3. Integration:
   - `HierarchyTree.inferred_epics: list[InferredEpic]` — new field.
   - `rebuild_hierarchy(…, infer_missing_epics=False)` — opt-in flag;
     when True the rebuilder runs `infer_epics` on orphan stories after
     the matching phase.
   - `apply_tree(…)` — when `materialise_inferred=True` (default), it
     calls `adapter.create_at_level(EPIC, proposal.title, proposal.body)`
     to materialise each high-confidence proposal, then uses
     `adapter.link` to attach the declared child stories.
4. New `HierarchyAdapter.create_at_level(level, title, body) -> WorkItem`
   Protocol method — the only way to create a root-level item (Epic or
   Story) without a pre-existing parent. GitHub adapter implements it
   via `addProjectV2DraftIssue` for Epic and `createIssue` for Story.
5. CLI: `whilly --rebuild-hierarchy --infer-epics` — adds one flag,
   reuses the existing rebuild flow.

Safety defaults:

- `min_stories_per_epic = 2` — singleton "epics" are a code smell, skip.
- `max_epics = 5` — hard ceiling keeps proposals reviewable.
- `inferred_confidence_threshold = 0.5` in `apply_tree` — below this,
  the proposal is printed but not materialised.
- Dry-run remains the CLI default — `--apply` required for any mutation.

## Considered alternatives

### A. Have the classifier invent Epics as it goes
Rejected — the per-item classifier doesn't see other orphan Stories,
so it can't cluster. Clustering is inherently a set-level operation.

### B. Full embedding-based clustering (k-means / hierarchical)
Would need a new runtime dep (sentence-transformers or OpenAI embeddings),
a cache, and distance thresholding. Accuracy on 10-50 orphans is no
better than the LLM "group these and propose titles" path, and the LLM
gives you titles *for free*. Revisit only if we hit the hundreds-of-orphans
scale.

### C. Ask user to write Epic titles manually
Already the fallback today (the rebuilder outputs unparented). ADR-020 is
specifically for the case where the user wants to *not* write titles
manually — so saying "write them manually" is nil content. Keeping this
as the "don't use the flag" option is exactly right.

### D. Auto-apply every inferred Epic above any confidence
Rejected — false Epic creation pollutes the board with junk cards that
are expensive to clean up. Threshold gating + dry-run default mirrors
the discipline in ADR-018 (router) and ADR-019 (rebuilder): whilly
proposes, human approves, then whilly executes.

### E. Don't preserve the existing `HierarchyAdapter` — add a new
`HierarchyCreator` Protocol instead
Overkill. One new method (`create_at_level`) on an existing Protocol
is less surface area than introducing a second Protocol with tight
coupling to the first.

### F. Run inference on every orphan item, not just Stories
The existing orphan Tasks (without matching Stories) are a distinct
problem — they need Story inference, not Epic inference. Keeping scope
to Story→Epic lets ADR-020 ship cleanly; Task→Story inference is a
natural follow-up with almost-identical plumbing.

## Consequences

### Positive
- Users can rebuild hierarchy from a truly flat state (no Epics, no
  parents) in a single command.
- Proposals carry per-cluster confidence + reasoning — reviewable
  diff, not a black-box mutation.
- `HierarchyAdapter.create_at_level` is generally useful beyond this
  ADR (ADR-018's CREATE_ORPHAN action can now actually apply via
  adapter rather than being "caller's problem").
- Opt-in flag keeps costs bounded — inference runs only when asked.

### Negative
- One LLM call per rebuild (in addition to per-item classification).
  Prompt size grows with orphan count; we cap at 50 stories.
- LLM-synthesised Epic titles can be generic ("Auth improvements").
  Users will often rename them after review — expected, not a bug.
- `create_at_level` broke the Protocol surface (one new method).
  Minor concern — we have zero external adapters.

### Follow-ups
- **Story inference from orphan Tasks** — same pattern, one level down.
  Copy this ADR's structure, probably ~150 LOC + tests.
- **Epic description quality** — current body is one paragraph; a
  structured "rationale / expected children / acceptance" block might
  read better in PR reviews. Tune prompt after live use.
- **Incremental inference** — re-running the rebuilder with an already-
  inferred Epic present should match existing Stories to the existing
  Epic rather than proposing a duplicate. Needs a "don't re-propose
  inferred Epics already on board" filter.
- **Title deduplication** — if the LLM proposes a title that already
  exists (case-insensitive), merge into the existing Epic rather than
  creating a sibling duplicate.

## References

- `whilly/classifier/epic_inferrer.py` — implementation.
- `whilly/classifier/rebuilder.py` — integration (`inferred_epics`
  field + `apply_tree` materialisation).
- `whilly/hierarchy/base.py` — Protocol extended with `create_at_level`.
- `whilly/hierarchy/github.py` — concrete implementation (draft + issue).
- `tests/test_epic_inferrer.py` — 16 tests (dataclass, infer_epics,
  rebuild integration, apply materialisation, low-confidence skip,
  unknown child id, `--apply` off).
- ADR-017 — `HierarchyAdapter`.
- ADR-018 — `Router` / classification.
- ADR-019 — `rebuild_hierarchy` (the predecessor).
