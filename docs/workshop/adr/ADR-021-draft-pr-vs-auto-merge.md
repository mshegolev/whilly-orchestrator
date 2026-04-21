# ADR-021 — Draft PR ≠ auto-merge: clarifying the merge-to-main policy

- **Status:** Accepted
- **Date:** 2026-04-22
- **Deciders:** project author
- **Domain:** sink / release policy / self-hosting contract
- **Tracking issue:** [#158](https://github.com/mshegolev/whilly-orchestrator/issues/158)
  (title references "[ADR-017]"; that slot was already taken by
  `ADR-017-hierarchy-model.md`, so this ADR ships under the next free
  number and the issue tag is treated as historical).

## Context

Two flags in the PR pipeline are routinely confused, and the confusion
keeps surfacing in reviews and self-hosting demos:

1. **`--draft`** on `gh pr create` — whether the PR is opened as a
   "work-in-progress" draft. This is a **collaboration signal**, not a
   merge gate.
2. **Auto-merge** — any mechanism by which a PR reaches `main` without a
   human explicitly clicking "Merge". In practice this includes
   `gh pr merge --auto`, branch-protection bypass tokens, and direct
   `git push origin main`.

Prior ADRs have already taken a hard stance:

- **ADR-007** ("PR creation sink"): "Никогда не пушить в main напрямую —
  security gate." The PR body template even ends with
  "Human review required before merge."
- **ADR-016** (quality gate): lint + tests must pass **before** a PR is
  offered up for merge.

However the rules have never been written in one place, and the
self-hosting helper `scripts/whilly-auto.sh` *does* call
`gh pr merge --auto` as part of its end-to-end "issue → merged" chain.
This looks, at a glance, like a violation of the "no auto-merge" rule —
and new contributors have asked whether opening a PR as `--draft` is
somehow "more allowed" than opening it ready-for-review.

This ADR captures the intended policy so reviewers and future
contributors can reason about both flags without re-deriving the model
every time.

## Decision

**Whilly never merges to `main` of its own volition. A human — or a
human-configured branch-protection rule — is always on the critical
path between a whilly-generated PR and the default branch.**

Two orthogonal axes, stated explicitly:

### 1. Draft status (orthogonal to merge policy)

`--draft` is **purely a notification/review-queue convention**:

- Draft PR → no review-requested notifications, no "ready to merge" UI.
- Ready PR → notifications fire, CODEOWNERS get pinged.

Draft status **does not** change who can merge, when, or how. A draft
PR that passes all checks is still blocked by branch protection; a
ready PR still cannot be merged by whilly itself.

Practical rule: **open as ready by default**; pass `--draft` when the
caller wants to stage a review pile without notification noise
(e.g., overnight autonomous runs where a human will triage in the
morning). This is exposed as the `draft=` field of
`GitHubPRSink` and the `--draft` flag pathway in
`open_pr_for_task` (`whilly/sinks/github_pr.py:52`,
`whilly/sinks/github_pr.py:232`).

### 2. What counts as "auto-merge"

"Auto-merge" in this project means: **the trigger that puts a commit on
`main`**. Whilly categorises each path as follows:

| Path                                                | Allowed? | Why                                                                                                                                                                     |
|-----------------------------------------------------|----------|-------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| `git push origin main` by whilly                    | ❌ Never | Direct push, no review. Security gate.                                                                                                                                  |
| `gh pr merge <pr>` by whilly (no `--auto`)          | ❌ Never | Whilly unilaterally clicking merge.                                                                                                                                     |
| `gh pr merge --auto <pr>` by whilly                 | ✅ Only when branch protection is on | `--auto` with branch protection = "merge once all required, human-configured checks pass". The **policy** is the human's; whilly is asking the policy to run. |
| `gh pr create [--draft]` by whilly                  | ✅ Always | Opens the PR; does not merge anything.                                                                                                                                   |
| Human clicking "Merge" in the GitHub UI             | ✅ Always | Canonical path.                                                                                                                                                          |

The third row is the subtle one. `scripts/whilly-auto.sh` calls
`gh pr merge --auto` and then polls until the PR state flips to
`MERGED` (`scripts/whilly-auto.sh:209-222`). This is **not** a policy
violation: `--auto` is a request to GitHub that says "merge this when
the required status checks pass". If branch protection is off,
`--auto` merges immediately — therefore:

> **Running `scripts/whilly-auto.sh` against a repo without branch
> protection is operator error, not a whilly bug.** The script assumes
> the required-checks contract exists; it is the human's responsibility
> to configure it.

### 3. Where the policy is enforced

- `whilly/sinks/github_pr.py` (the default on-done sink) never calls
  `gh pr merge`. It only opens the PR and writes "Human review required
  before merge." into the PR body
  (`whilly/sinks/github_pr.py:172`).
- `scripts/whilly-auto.sh` is an **opt-in end-to-end helper**, not a
  sink. It lives outside the main loop and must be invoked explicitly.
- New sinks/scripts that want to merge automatically MUST require, at
  least, either (a) `gh pr merge --auto` with an asserted branch-
  protection check, or (b) an explicit human-in-the-loop prompt. A new
  code path that calls plain `gh pr merge` on whilly-opened PRs is a
  blocking review comment.

## Considered alternatives

### A. Ban `gh pr merge --auto` across the project

Rejected. `--auto` under branch protection is *the* standard GitHub
idiom for "merge when CI goes green" and is what `whilly-auto.sh`
needs for unattended self-hosting demos. Banning it would either
(a) keep a human tethered to the terminal for every demo run, or
(b) push the script toward a worse alternative (polling + plain
`gh pr merge`), which has strictly less safety.

### B. Always open PRs as draft

Rejected. Drafts suppress review notifications, which is exactly wrong
when a human *is* available and reviewing in real time. Draft should be
an explicit caller choice, not a blanket default.

### C. Encode the policy as CI check ("no `gh pr merge` in whilly code")

Considered. Probably worth a follow-up: a cheap grep-based pre-commit
hook that fails if `gh pr merge` appears in `whilly/` (the library
tree) without `--auto`. Scripts under `scripts/` are exempt because
they are opt-in. Deferred as a follow-up so this ADR stays documentation-
only.

### D. Treat `--draft` as "in-progress = may auto-merge later"

Rejected — and this is the misconception this ADR exists to kill. Draft
vs ready never says anything about merge authority. Conflating them
erodes the single-sentence invariant ("whilly never merges to main") we
want to be able to state to auditors.

## Consequences

### Positive

- One-sentence invariant that anyone reading this repo can repeat:
  **"Whilly never merges to `main` on its own; `gh pr merge --auto`
  only appears in opt-in scripts and relies on branch protection."**
- The draft flag is freed up to do its actual job (notification control)
  without carrying hidden policy weight.
- `whilly-auto.sh`'s use of `--auto` is defended on the record, so
  future reviewers don't have to re-derive the reasoning every time they
  see the line.

### Negative

- The policy assumes branch protection is correctly configured on the
  target repo. Whilly does not verify this at runtime. A follow-up
  preflight check (`gh api repos/:o/:r/branches/main/protection`) would
  close the gap — captured as a follow-up, not required for this ADR.
- New contributors still need to read this ADR to absorb the
  distinction; the pyproject surface doesn't enforce it.

### Neutral

- ADR-007 is not superseded; this ADR complements it by naming the
  draft/auto-merge axes explicitly.

## Follow-ups

- **Pre-commit grep check** — fail if `whilly/` (not `scripts/`)
  contains `gh pr merge` without `--auto`.
- **Runtime branch-protection preflight** — `whilly-auto.sh` should
  assert that `main` has at least one required status check before
  calling `--auto`, and refuse otherwise.
- **CHANGELOG entry** pointing at this ADR when the next release cuts,
  so downstream users see the clarified policy at upgrade time.

## References

- ADR-007 — PR creation sink (`docs/workshop/adr/ADR-007-pr-creation-sink.md`).
- ADR-016 — Language-agnostic quality gate.
- `whilly/sinks/github_pr.py` — default sink; never merges.
- `scripts/whilly-auto.sh` — end-to-end helper; uses `gh pr merge --auto`
  under the branch-protection assumption documented above.
- Issue #158 — tracking issue (originally tagged `[ADR-017]`; see note
  at top of this file).
