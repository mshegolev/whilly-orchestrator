# ADR-021 — Draft PR vs auto-merge: clarifying the human gate

- **Status:** Accepted
- **Date:** 2026-04-22
- **Domain:** sink / safety policy
- **Supersedes:** none (clarifies ADR-007 + ADR-015)
- **Tracker:** GH-158 (title tagged `[ADR-017]` for historical reasons;
  ADR-017 was already taken by the hierarchy-model ADR at the time this
  one was written — see "Numbering note" below).

## Context

Two separate knobs in whilly have been repeatedly conflated in reviews
and user questions:

1. **`--draft` on `gh pr create`** — makes the PR a *draft* on GitHub.
   Exposed by `GitHubPRSink.draft` and `open_pr_for_task(draft=...)` in
   `whilly/sinks/github_pr.py`. Whilly can also open a draft PR from the
   e2e pipeline (`scripts/whilly_e2e_triz_prd.py`) when quality gates are
   inconclusive or the author wants a review pile-up without pings.
2. **`--allow-auto-merge`** — an explicit opt-in on the e2e scripts
   (`whilly_e2e_demo.py`, `whilly_e2e_triz_prd.py`) that, when set,
   shells out to `gh pr merge --squash --delete-branch` after a *clean*
   self-review. OFF by default.

These are orthogonal. Observed confusion:

- "You opened a draft PR, so the bot already merged." — wrong: a draft
  PR by definition cannot be merged on GitHub (the merge button is
  disabled until the PR is marked ready for review).
- "Draft PRs are a security footgun because the auto-merger will flip
  them to ready and merge." — also wrong: no whilly code path flips a
  draft PR to ready, and the auto-merger only runs for PRs it *opened
  non-draft* in the first place.
- "`--draft` is the auto-merge escape hatch." — backwards: `--draft` is
  the *conservative* mode; it makes a merge strictly harder, not easier.
- Conversely, reviewers have assumed `--allow-auto-merge` is safe
  because "the PR is still a draft" — but `--allow-auto-merge` is only
  honoured on non-draft PRs with a clean review, and it really does
  merge to `main`.

The README (`README.md:53`, `README-RU.md:50`) and NFR-7 in PRD-Whilly
(`docs/workshop/PRD-Whilly.md:290`) both state the policy in one line
("merge always human, auto-merge requires explicit opt-in"), but there
is no single document a reviewer can be pointed at. Issue #158 was
filed (and duplicated multiple times by a board-bootstrap loop —
#37, #61, #85, #109, #134) specifically to produce that document.

## Decision

Adopt the following policy and pin it here.

1. **Draft PR ≠ auto-merge violation.** Opening a PR as a draft is a
   *stricter* state than opening it non-draft — draft PRs cannot be
   merged via the GitHub UI or API until a human marks them ready for
   review. Whilly has no code path that transitions a PR from draft to
   ready.
2. **The merge step is always human** for any PR whilly opens in
   self-hosting mode (PRs whose changes touch whilly's own code).
   `--allow-auto-merge` exists only for the e2e demo scripts running
   against *other* repositories; the default is OFF and any whilly
   self-hosting invocation must leave it OFF.
3. **The two flags compose as an AND, not an OR:**

   | `draft` | `--allow-auto-merge` | Outcome |
   |---|---|---|
   | False | False | Non-draft PR, left open for human review. Default. |
   | False | True  | Non-draft PR, auto-merged **iff** self-review returns `clean: true`. Only legal on non-self-hosting runs. |
   | True  | False | Draft PR, left open for human to mark ready. |
   | True  | True  | Draft PR. `--allow-auto-merge` is a no-op here — draft PRs cannot be merged. |

4. **`GitHubPRSink.draft=True` is the preferred mode for any pipeline
   that mutates whilly's own code.** The self-hosting bootstrap demo
   (ADR-012) and the TRIZ+PRD pipeline (ADR-015) both honour this.

## Considered alternatives

### A. Remove `--draft` entirely; rely on `--allow-auto-merge` being off
Rejected. Draft mode carries signal that `not auto-merging` does not:
"this PR is explicitly not ready for review *yet*". Removing it would
force reviewers to either (a) read the PR body to discover that it's a
bot-produced stub, or (b) treat every open PR as review-ready and
spend attention on things that aren't. The two states are not
redundant.

### B. Remove `--allow-auto-merge` entirely; always leave PRs for humans
Considered. For self-hosting this is already the behaviour. The reason
to keep the flag is that the e2e scripts double as a reference
implementation for whilly running against arbitrary repos — in a
sandboxed "whilly can have the whole repo" scenario, auto-merge after a
clean self-review is a legitimate optimisation. The safety is provided
by the default being OFF and by the quality-gate + self-review being
mandatory prerequisites, not by banning the feature outright.

### C. Name them `--draft` and `--allow-auto-merge` more distinctly
(e.g. `--bot-wip` and `--full-autopilot`) to reduce confusion
Rejected. `--draft` is GitHub-native vocabulary — renaming it would
obscure which concept it controls. `--allow-auto-merge` is already the
most explicit reasonable name (the "allow-" prefix signals it's an
opt-in on something the system otherwise refuses).

### D. Collapse the combined matrix into a single enum
(`--pr-mode={draft,open,merge}`)
Rejected on orthogonality grounds: the two flags answer different
questions (is this PR ready for review? may the system self-merge
afterwards?). Flattening them would produce an enum whose values are
just the cross-product; combinatorially no simpler, and it would break
existing CLI/env contracts.

## Consequences

### Positive

- A single URL to hand reviewers the next time "whilly auto-merged my
  draft PR" shows up in a thread.
- Removes a class of questions from new-contributor onboarding by
  making the policy explicit rather than folklore.
- Makes the combined matrix (draft × auto-merge) explicit — no behaviour
  changes, but the corners are now pinned in the documentation.
- Future flags on the sink (`--ready-for-review-after`, `--labels`,
  etc.) have a place to land that already distinguishes *review state*
  from *merge behaviour*.

### Negative

- Adds one more ADR to the index for a policy that is, in the code,
  ~20 LOC of conditionals. Mitigated by the fact that reviewers keep
  asking about it — the documentation-to-code ratio here is
  intentional.
- The numbering is awkward (see below).

### Numbering note

The tracker issue (GH-158, GH-134, …) was filed with the title
`[ADR-017] Draft PR vs auto-merge clarification` on the assumption
that ADR-017 was the next free slot. By the time the document got
written, ADR-017 was already taken by the hierarchy-model ADR, and
ADRs are never renumbered after `Accepted` (see the "How to add a new
ADR" section in `docs/workshop/adr/README.md`). The document therefore
lands at ADR-021 — the first free slot — and the issue title's
`[ADR-017]` tag is kept as-is so inbound links from the README
(`README.md:53`, `README-RU.md:50`) continue to resolve to the issue,
which in turn links here.

## References

- `whilly/sinks/github_pr.py` — `GitHubPRSink.draft`, `open_pr_for_task(..., draft=...)`.
- `scripts/whilly_e2e_demo.py` — `--allow-auto-merge` flag, `ALLOW_AUTO_MERGE`.
- `scripts/whilly_e2e_triz_prd.py` — same flag, TRIZ+PRD variant.
- `docs/workshop/PRD-Whilly.md` §NFR-7 — policy one-liner.
- `docs/workshop/BRD-Whilly.md` §10 D9 — human-gate constraint from the BRD.
- `README.md:53`, `README-RU.md:50` — forward links to this policy via GH-158.
- ADR-007 — PR creation sink (the `draft` flag lives here).
- ADR-012 — Self-hosting bootstrap demo (applies the policy).
- ADR-015 — TRIZ+PRD e2e pipeline (where `--allow-auto-merge` ships).
