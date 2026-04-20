# ADR-019 — Flat-to-hierarchy reconstruction (`rebuild_hierarchy`)

- **Status:** Accepted
- **Date:** 2026-04-20
- **Domain:** inbox automation / bulk routing

## Context

ADR-017 gave us the Epic/Story/Task model; ADR-018 added per-item smart
routing. But the common entry state on a real project is a **flat pile
of issues** with no parent-child links — typical after:

* migrating from Jira/Linear/Trello (where hierarchy lived in the
  source tool);
* a long-neglected GitHub project where only labels were used;
* automatic imports from a CSV / spreadsheet;
* "we started simple, then the project grew" drift.

Doing per-item classify+match for every stale item is viable (ADR-018
already supports it) but tedious. What users actually want is one
command: *"look at everything, propose the tree, let me audit it, apply
when I'm happy."*

## Decision

Introduce `rebuild_hierarchy(items, …) → HierarchyTree` in
``whilly/classifier/rebuilder.py``. Pipeline:

1. **Classify every item** via the injected (or default) classifier.
   Input :attr:`WorkItem.level` is ignored — the imported data is
   usually wrong, classification is authoritative.
2. **Bucket by classified level** — epics / stories / tasks.
3. **Bottom-up matching:** each Task without an existing parent gets
   matched against the classified Stories via the matcher Protocol;
   each Story gets matched against the Epics. Below-threshold children
   land in ``unparented``.
4. **Return a :class:`HierarchyTree`** carrying counts, assignments,
   unparented items, and the full classifier output per item. No
   mutation at this point.
5. **Caller applies** via :func:`apply_tree` which calls
   :meth:`HierarchyAdapter.link` per assignment.

CLI: ``whilly --rebuild-hierarchy --project URL --repo X/Y [--label L]
[--apply]``. Dry-run is the default; ``--apply`` calls
``adapter.link`` for each proposed assignment.

Idempotency guarantees:

* Items with an existing ``parent_id`` are left alone — rebuilder
  never re-routes someone else's decision.
* ``apply_tree`` is idempotent — re-running with the same tree doesn't
  double-link.

## Considered alternatives

### A. One-shot LLM prompt: "here's 80 items, return a tree"
Rejected. Prompt blows past reasonable size at ~30 items, and the model
can't reliably cross-reference without re-loading context per pair.
Cost ramps up faster than linear; accuracy ramps down.

### B. Embedding-based clustering + heuristic labelling
Considered. Needs a new dep (sentence-transformers or OpenAI embeddings)
and a corpus cache. LLM-based per-item classification hits the same
accuracy on the sizes we care about (tens-to-hundreds of items) and
keeps the dep surface unchanged.

### C. Re-use `Router` for every item
The `Router` from ADR-018 does classify+match for ONE item at a time.
A naive loop would call the matcher with the full candidate list every
time — N×M lookups instead of the N+M we do here (classify all, then
match within already-classified buckets). The rebuilder is a different
*shape*, not a different primitive.

### D. Invent missing parents (auto-generate Epics/Stories)
Deferred. When the classifier says "this is a Story" but no Epic
matches any Story, the rebuilder could cluster Stories and synthesise
an Epic title. This is a cool feature but orthogonal to the routing
problem — tracked as a follow-up ("Epic inference") to keep the rebuild
step debuggable.

### E. Destructively re-parent everything, ignoring prior `parent_id`
Rejected. Too easy to annoy users who had partial manual structure
they wanted preserved. The "respect existing parent_id" rule means the
rebuilder only fills gaps.

### F. Apply immediately without dry-run
Rejected. Bulk mis-linking is an expensive to undo — one audit pass is
essentially free (print the tree) and the signal/noise gain is huge.

## Consequences

### Positive
- One command replaces manual triage of hundreds of items.
- Per-assignment confidence score surfaces the ambiguous cases — human
  only reviews those.
- Respects prior parent_id — safe to re-run as new items arrive.
- Works today against GitHub; Jira/Linear drop in when their
  ``HierarchyAdapter`` arrives.

### Negative
- N classifier calls + (Tasks + Stories) matcher calls. For 100 items
  that's 100 + ~80 = ~180 LLM calls. At haiku pricing ~$0.20 per
  rebuild, which is cheap but not zero — we cap cost by making the
  rebuild opt-in (CLI flag), not automatic.
- Matcher prompt includes candidate summaries — with 100+ Stories the
  prompt gets large. The matcher already truncates to 20 candidates;
  may need smarter pre-filtering (alphabetical pre-chunk, title-
  similarity pre-filter) if projects scale much further.
- Classified level can disagree with the tracker's native type
  (GitHub sub-issue classified as Story by the LLM). Today we trust
  the classifier; future option: emit a `reclassify-conflict` flag and
  ask a human.

### Follow-ups
- **Epic inference** — cluster orphan Stories, synthesise Epic titles,
  propose them for user confirmation before creation. Separate
  ADR-020.
- **Pre-filter matcher candidates** — keyword or embedding-based
  shortlist before the LLM call, for projects above ~200 items.
- **Idempotency marker** — a ``whilly:reviewed`` label auto-applied on
  items whose classification was reviewed by a human, so the
  rebuilder skips them on subsequent runs without re-asking the LLM.
- **Re-classification mode** — opt-in flag to re-classify items even
  when they already have a parent_id, for users who migrated wrong
  structure and want a hard reset.
- **Batch classification prompt** — for cost-sensitive environments,
  one LLM call that classifies N items at once. Accuracy tradeoff
  requires data; deferred.

## References

- `whilly/classifier/rebuilder.py` — implementation.
- `tests/test_rebuilder.py` — 15 tests (empty input, reclassification,
  threshold behaviour, existing-parent preservation, full 3-level
  integration, apply idempotency, format rendering).
- ADR-017 — `HierarchyAdapter` + `WorkItem`.
- ADR-018 — `Router` + `ClassificationResult` + `ParentMatch`.
