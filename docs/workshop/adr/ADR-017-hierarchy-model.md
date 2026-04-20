# ADR-017 — 3-level work hierarchy (Epic / Story / Task) via `HierarchyAdapter` Protocol

- **Status:** Accepted
- **Date:** 2026-04-20
- **Domain:** multi-tracker abstraction / pipeline hierarchy

## Context

The TRIZ+PRD pipeline (ADR-015) treats every GitHub issue as a single
"Story" — it decomposes into tasks locally (`whilly_GH-N_tasks.json`) but
never materialises those as first-class tracker items. Meanwhile, users
arriving from Jira / Linear / YouTrack expect a richer three-level flow:

```
Epic  (strategic intent)         → BRD + PRD
Story (concrete feature)         → ADR, decomposition
Task  (atomic execution unit)    → PR
```

Hard-coding this into GitHub-specific code would lock whilly to one
tracker. The project already has `AgentBackend` (OC-103), `BoardSink`
(ADR-014), and `QualityGate` (ADR-016) abstractions — adding a
`HierarchyAdapter` Protocol is the same shape, and the idiom is by now
familiar to contributors.

## Decision

Introduce `whilly/hierarchy/` with:

1. `HierarchyLevel` str-Enum (`EPIC`, `STORY`, `TASK`) with `.parent` /
   `.child` navigation helpers. Stable 3-level vocabulary, JSON-safe.
2. `WorkItem` dataclass — tracker-agnostic shape: `id`, `level`,
   `title`, `body`, `parent_id`, `children_ids`, `external_ref` (opaque
   per-tracker handle), `labels`, `status`.
3. `HierarchyAdapter` Protocol: `get`, `list_at_level`, `promote`,
   `create_child`, `link`. Narrow enough that adapters stay under
   ~400 LOC; broad enough to express the whole pipeline flow.
4. First concrete adapter — `GitHubHierarchyAdapter` — maps
   Epic = Project v2 draft item, Story = Issue, Task = sub-issue.
   Includes a checkbox-list fallback for when GitHub's sub-issue API
   isn't available (older GHES, missing scope, preview flag off).
5. Registry + `get_adapter("github", project_url=..., repo=...)`
   factory, same shape as `whilly.workflow.get_board`.

Error discipline mirrors the other Protocols:
- Transport / auth / schema errors → `HierarchyError` (RuntimeError subclass).
- Expected "not found" → return `None` or empty list — caller decides.

## Considered alternatives

### A. Just add sub-issue emission to the existing pipeline
Narrowest scope, but traps every future integration (Jira, Linear) into
"rewrite pipeline again". The incremental cost today is paying ~400 LOC
for an abstraction that makes future adapters cheap.

### B. Generic 3-field `Issue { parent, children }` schema with tracker-coded
ids only
Considered, but the polymorphic behaviour (GitHub draft→issue conversion
is a distinct operation, Jira Story→Task needs workflow transitions
alongside creation) doesn't flatten into a single `create` method
without type tags. The per-level / per-operation methods (`promote`,
`create_child`, `link`) keep the intent explicit in the signature.

### C. Map Jira Epic to GitHub parent-issue-with-checklist (no drafts)
Rejected for v1 — GitHub's Projects v2 is the natural home for
strategic-level planning in GitHub-first orgs, and users shouldn't have
to maintain two issue layers just for whilly. For Jira-first orgs the
adapter will use Jira's actual Epic issue type.

### D. Skip `external_ref`, pass tracker-native handles as strings
Rejected — lossy. GitHub needs three distinct ids (project item node id,
draft id, issue node id for different operations). Dict is the simplest
multi-value carrier.

### E. `link()` on tasks
GitHub's hierarchy is Epic → Story → Task (2 levels of nesting).
Rejected "task of task" by making `create_child(task, ...)` raise and
`link(task, ...)` return False. Should the platform grow another level,
this opens cleanly (new enum value + navigation helpers update).

## Consequences

### Positive
- Future Jira/Linear adapters are one file each — same Protocol,
  tracker-specific API calls, reuse the pipeline unchanged.
- "Whilly operates on Epic/Story/Task" is a single sentence in the
  README — users from Jira/Linear recognise the shape immediately.
- Sub-issue fallback (checkbox list) means whilly works on every GitHub
  repo regardless of feature flags — the API path is a bonus.
- `WorkItem.external_ref` carries the tracker-native bag so pipelines
  don't have to re-derive "which repo / which project" on every stage.

### Negative
- Two paths in the GitHub adapter — sub-issue API + checkbox — double
  the surface to test. Mitigated by the fallback auto-detection being a
  one-shot flag on the adapter instance (`_sub_issue_api_available`).
- Sub-issue API is still (as of 2026-Q2) in flux at GitHub's end; the
  `_M_ADD_SUB_ISSUE` mutation shape may need updating.
- "Epic as draft OR as parent issue" ambiguity — for v1 drafts only;
  parent-issue-style Epics go through `link()` manually. Revisit when
  a user hits friction.
- `create_child` in v1 doesn't attach labels to the new issue (GitHub's
  `createIssue` takes label *ids*, not names — extra round-trip). Deferred.

### Follow-ups
- **Jira adapter** — first cross-tracker validation of the Protocol.
- **Linear adapter** — same.
- **Attach labels on create_child** — cache label id lookups, pass them
  to `createIssue`.
- **Epic = parent issue** mode — when a user prefers tracking Epics as
  regular issues (with a label like `type:epic`) rather than drafts.
- **Auto-close cascade** — when all Tasks of a Story are closed, mark
  the Story closed; when all Stories of an Epic are closed, close the Epic.

## References

- `whilly/hierarchy/__init__.py` — factory + registry.
- `whilly/hierarchy/base.py` — Protocol + WorkItem + HierarchyLevel.
- `whilly/hierarchy/github.py` — GitHub adapter (draft/issue/sub-issue).
- `tests/test_hierarchy_*.py` — 33 tests pinning the contract + GitHub path.
- ADR-013 — `AgentBackend` (the idiom, first application).
- ADR-014 — `BoardSink` (the idiom, workflow integration).
- ADR-016 — `QualityGate` (the idiom, language-agnostic gate).
