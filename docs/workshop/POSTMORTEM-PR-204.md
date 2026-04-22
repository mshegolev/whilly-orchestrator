# Post-mortem: PR #204 — Self-healing retry loop in action

> **TL;DR.** Whilly opened [PR #204](https://github.com/mshegolev/whilly-orchestrator/pull/204) with all 7 CI checks green, then closed it 37 seconds later and reran the same issue in a fresh workspace. The second run produced [PR #205](https://github.com/mshegolev/whilly-orchestrator/pull/205), which merged cleanly. Root cause was a stdout-parsing bug in `whilly-auto.sh`, fixed in commit [`feb02b2`](https://github.com/mshegolev/whilly-orchestrator/commit/feb02b2). Shipped as **Whilly 3.2.1**.

This page is preserved as a teaching artefact. The same class of failure will reappear on any shell pipeline that captures `gh` stdout without discarding warning lines — so the mechanism matters more than the specific commit that fixed it.

---

## Timeline (all UTC, 2026-04-21)

| Time | Event | Artefact |
|---|---|---|
| 22:30:41 | `whilly-auto-loop.sh` iter-1 starts on issue [#158](https://github.com/mshegolev/whilly-orchestrator/issues/158) | `whilly-auto-runs/iter-1-2026-04-21T22-30-41Z.log` |
| 22:37:15 | Whilly finishes phases 1–8: `1/1 done in 6m05s`, exit 0 | same log |
| 22:37:19 | `gh pr create` opens [PR #204](https://github.com/mshegolev/whilly-orchestrator/pull/204) | PR metadata |
| 22:37:19–38 | CI kicks off (Lint, Tests × 5 platforms, Agent backends) | [Actions run 24750055661](https://github.com/mshegolev/whilly-orchestrator/actions/runs/24750055661) |
| 22:37:xx | `gh pr merge` receives a polluted URL → `error: gh pr merge failed 3` | same log |
| 22:37:51 | Loop iter-2 begins; `whilly-auto-reset.sh` closes PR #204, deletes branch, resets Projects card to *Todo* | `whilly-auto-runs/iter-2-2026-04-21T22-37-51Z.log` |
| 22:37:56 | PR #204 state → `CLOSED` (not merged) | PR metadata |
| 22:43:xx | iter-2 opens [PR #205](https://github.com/mshegolev/whilly-orchestrator/pull/205) | PR metadata |
| 22:48:xx | PR #205 merges into `main`, card → *Done* | `4f714c0 docs(GH-158): ADR-021` |

Total wall-clock cost of the incident: ~10 min and one spurious closed PR. Human intervention: zero. Production impact: zero (no downstream consumer ever saw #204).

---

## Root cause

`gh pr create` normally prints one line to stdout — the URL of the new PR. But when the working tree is not clean (stashed or un-added files), it prepends a warning:

```
Warning: 1 uncommitted change
https://github.com/mshegolev/whilly-orchestrator/pull/204
```

The pre-`feb02b2` version of `scripts/whilly-auto.sh` captured the whole stdout as a single shell variable and passed it directly to `gh pr merge`:

```bash
pr_url="$(gh pr create --title "$TITLE" --body "$BODY")"
# …
gh pr merge "$pr_url" --squash
```

That handed `gh pr merge` a two-line string starting with `Warning:` — no URL, no number, no branch. `gh` reported:

```
no pull requests found for branch "Warning: 1 uncommitted change\nhttps://github.com/mshegolev/whilly-orchestrator/pull/204"
error: gh pr merge failed 3
```

The script exited with code 3 (PR merge failed). The outer loop caught the non-zero exit, invoked `whilly-auto-reset.sh`, which by policy **closes any open Whilly PR on the workspace branch** as part of its scrub — hence PR #204 went to `CLOSED`.

---

## What saved us (already in 3.2.0)

1. **Reset script is idempotent.** `whilly-auto-reset.sh` assumes nothing about PR state; it closes any open PR on the workspace branch, deletes both the local and remote branches, removes the worktree, re-fetches `main`, and resets the Projects v2 card to *Todo*. After reset, the workspace is indistinguishable from a never-run state.
2. **Retry is bounded.** `whilly-auto-loop.sh` caps attempts via `MAX_ATTEMPTS` (default `10`) and sleeps `BACKOFF_SEC` (default `30s`) between iterations. Each iteration gets its own timestamped log file.
3. **Card hygiene on the board.** The board sync treats card state as a function of issue state, not PR state. Reset pulls the card back to *Todo* before retry, so the next success cleanly transitions `Todo → In Progress → In Review → Done` without skipping columns.
4. **The task was genuinely done.** Phases 1–8 completed in 6m05s with exit 0. The failure was **only** in the post-Whilly PR-merge plumbing. When iter-2 re-ran Whilly on the same issue, the agent reproduced the same ADR file deterministically (ADR-021) — same git patch, same validation. The incident cost one extra agent run, not one extra decision.

---

## What the fix looks like

Commit [`feb02b2`](https://github.com/mshegolev/whilly-orchestrator/commit/feb02b2) switches the stdout capture to a URL-extracting regex:

```bash
pr_stdout="$(gh pr create --title "$TITLE" --body "$BODY" 2>&1)"
pr_url="$(printf '%s\n' "$pr_stdout" | grep -Eo 'https://github\.com/[^[:space:]]+/pull/[0-9]+' | tail -1)"
if [[ -z "$pr_url" ]]; then
    printf '%s\n' "$pr_stdout" >&2
    exit 3
fi
```

Two guardrails worth noting:

- **`tail -1`** — if `gh` ever prints multiple PR URLs (a thing it can do when it detects an existing PR for the branch), take the last one, which is always the current-run URL.
- **Explicit failure** — if no URL can be extracted, dump `gh`'s full stdout to stderr before exiting. This turns the same class of bug into a one-log diagnosis next time, not a two-log detective story.

A separate follow-up, commit [`d5237aa`](https://github.com/mshegolev/whilly-orchestrator/commit/d5237aa), adds a fallback path for the post-merge Projects v2 card move when the GraphQL mutation is blocked by PAT scope — unrelated to this bug, but landed in the same cleanup pass because the script-side logging improvements from `feb02b2` surfaced the second issue.

---

## Lessons

1. **Never trust `gh` stdout to be single-line.** Git state leaks into `gh` warnings; CI environments leak into banners; proxy reconnects print reassurance lines. Any shell pipeline that consumes `gh` output must parse it, not capture-and-forward.
2. **Reset scripts are load-bearing infrastructure.** Without `whilly-auto-reset.sh`, a partial failure leaves a stale PR, a dangling branch, a half-updated Projects card, and a contaminated worktree. The retry loop would compound instead of recover. Budget for reset logic the same way you budget for forward logic.
3. **Make the deterministic-rerun guarantee explicit.** The Whilly agent phase (1–8) is deterministic enough that re-running on the same issue produces the same patch. That property is what makes the retry loop cheap. When adding new agent behaviour, keep it deterministic or mark it non-replayable — otherwise the retry-on-failure pattern stops working.
4. **Closed PRs are not trash, they are evidence.** Do not purge closed Whilly PRs. The diff, the CI rollup, and the close-timestamp form a continuous narrative that makes post-mortems like this one possible.

---

## Mechanism reference

| Script | Role on this incident |
|---|---|
| `scripts/whilly-auto.sh` | Ran phases 1–8, pushed branch, opened PR #204, captured polluted URL, `gh pr merge` exited 3 → whole script exited 3 |
| `scripts/whilly-auto-loop.sh` | Caught exit 3 from iter-1, waited `BACKOFF_SEC`, re-entered iter-2 |
| `scripts/whilly-auto-reset.sh` | Closed PR #204, deleted `whilly/workspace/github-mshegolev-whilly-orchestrator` (local + remote), removed `.whilly_workspaces/…` worktree, reset card #158 to *Todo*, re-fetched `main` |
| `gh` CLI | Correctly refused to merge a garbage "URL" — failure was loud, not silent |
| `whilly --from-issue … --go --headless` (iter-2) | Rebuilt the ADR file from the same issue deterministically |

For the eight phases referenced in row 1, see [`Task-Execution-Phases`]({{ site.baseurl }}/documents/task-execution-phases).

---

## See also

- [ADR-007 — PR creation sink](adr/ADR-007-pr-creation-sink.md) — policy for how Whilly-opened PRs are managed
- [ADR-021 — Draft PR vs auto-merge](adr/ADR-021-draft-pr-vs-auto-merge.md) — the decision that was actually being documented when this fire happened
- [`scripts/whilly-auto-loop.sh`](../../scripts/whilly-auto-loop.sh) — retry-with-reset loop reference
- [`scripts/whilly-auto-reset.sh`](../../scripts/whilly-auto-reset.sh) — idempotent workspace scrubber
- PR on GitHub: [#204 (closed)](https://github.com/mshegolev/whilly-orchestrator/pull/204) · [#205 (merged)](https://github.com/mshegolev/whilly-orchestrator/pull/205)
