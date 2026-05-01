# Workspace Topology — design only (M4 future mission)

> **⚠️ This document is design-only — NOT implemented in this mission.**
> M1 (v4.4) ships the *deployment* artifacts for split control-plane / worker
> hosts. The per-worker editing workspace described here is the M4 work and
> will land in a future mission. We capture the design now so M2 (TLS,
> per-user trust) and M3 (observability) cannot accidentally lock in
> assumptions that conflict with the workspace topology we want to converge on.
> If you are looking for the v4.4 deployment walkthrough, that lives in
> [`docs/Distributed-Setup.md`](Distributed-Setup.md).

## Why this document exists

When two or more workers are draining the same plan against a shared
control-plane, three things must be true:

1. Each worker must be able to actually edit code (read files, run tools,
   commit, ...). The control-plane is a transaction shaper, not a code
   editor — code editing happens on the worker.
2. Two workers picking up two unrelated tasks must not stomp on each
   other's working tree. The single-host demo gets away with one shared
   `./workspace` mount because there is exactly one worker. Multi-host
   deployments need an isolation story.
3. The orchestrator must be able to integrate the per-worker output back
   into a canonical branch — a worker can finish its task without that
   integration being immediate (workers are stateless pollers), but the
   integration must be *possible* and the design must say how.

These are the **workspace topology** concerns. They are orthogonal to the
M1 deployment topology (split-host) and orthogonal to the M2 TLS work, but
they constrain both, so we lock the decision in here before either of those
ships.

## Decision summary

> **Option A is locked in. Decision: A.** (Sometimes written more tersely as
> "A locked in" in our planning notes — same statement, same finality.)

We commit to **per-worker git clone + push-branch** as the workspace
topology for M4. Options B (shared workspace) and C (patch-based / queue)
were considered and ruled out for the reasons below; they are documented
mainly so future maintainers can re-open the decision with full context
if circumstances change (e.g. an SDK change makes one of them attractive).

The remainder of this document is structured as:

* [Goals & non-goals](#goals--non-goals) — what M4 must / must not do.
* [Option A — per-worker git clone + push-branch](#option-a--per-worker-git-clone--push-branch) (chosen).
* [Option B — shared workspace, advisory locking](#option-b--shared-workspace-advisory-locking) (rejected).
* [Option C — patch-based / queue](#option-c--patch-based--queue) (rejected).
* [Why A wins](#why-a-wins).
* [Open questions deferred to M4](#open-questions-deferred-to-m4).
* [Risks](#risks-of-option-a).
* [Worked example](#worked-example-end-to-end-trace).
* [Status / out-of-scope reminder](#status--out-of-scope-reminder).

---

## Goals & non-goals

### M4 must

* Let two workers running on different hosts edit code on disjoint task
  paths concurrently without stomping on each other's working tree.
* Preserve the v4 audit-log invariant: every state transition is observable
  through the `events` table. The workspace layer is allowed to *append*
  events (e.g. `workspace.cloned`, `workspace.pushed`), but must not skip
  the existing CLAIM / COMPLETE / FAIL transitions.
* Keep the worker dependency closure narrow. Workspace operations may use
  `git` (already a hard requirement on workers — Whilly shells out to it
  for plan PRD discovery) but must not pull `asyncpg`, `fastapi`, or
  `jinja2` into the worker's import path.
* Be idempotent on retry. A worker that completes a task and then crashes
  before the control-plane records `COMPLETE` must be safe to re-run; the
  workspace layer must not duplicate work or push conflicting branches.
* Be operator-debuggable: a human SSHing into a worker host should be able
  to inspect the working tree of any in-flight claim with standard git
  tooling (`git log`, `git status`, `git diff`).

### M4 must not

* Require workers to share a network filesystem (NFS, EFS) — that has
  failure modes that are misery to debug from the worker's POV and forces
  a sticky placement story onto the control-plane.
* Introduce a new long-running daemon on the worker beyond `whilly-worker`
  itself. Whatever workspace primitive we pick must be reachable from the
  existing httpx-based loop without a sidecar.
* Change the v4 `tasks` row contract. Workspace state is per-claim and
  per-worker; no `tasks.workspace_*` columns are added. (If we need to
  persist workspace metadata it lands in `events.detail` jsonb, which is
  already part of the schema since `003_events_detail`.)

---

## Option A — per-worker git clone + push-branch

**TL;DR:** Each worker maintains its own local git clone of the target
repo. On CLAIM, the worker `git fetch`'s, branches off the canonical
ref captured in the plan, runs the agentic CLI inside the working tree,
then `git push`'es a per-task branch (e.g. `whilly/<plan_id>/<task_id>`)
back to the upstream remote. The control-plane records the resulting
ref in the `events` payload; an operator (or a future Forge integration)
opens a PR / merges the branch.

```
┌─────────────────────┐      claim         ┌────────────────────────────────┐
│  control-plane      │ ─────────────────► │  whilly-worker (Host B)        │
│  (FastAPI)          │   {task_id, ...}    │  ┌──────────────────────────┐ │
└─────────────────────┘                     │  │ ./workspace/<plan_id>/   │ │
        ▲                                   │  │   .git ...               │ │
        │                                   │  │   ... working tree ...    │ │
        │                                   │  └──────────────────────────┘ │
        │   complete + branch_ref           │                               │
        └─────────────────────────────────  │  agentic CLI runs in tree    │
                                            │  (claude / opencode / ...)    │
                                            │  git push origin branch       │
                                            └────────────────────────────────┘
```

### Per-task lifecycle

1. **CLAIM** received from `POST /tasks/claim`. Payload includes
   `plan_id`, `task_id`, and the canonical `git_ref` (head SHA at plan
   creation, persisted on the `plans` row).
2. Worker ensures `./workspace/<plan_id>/.git` exists. First-claim path:
   `git clone <upstream_url> ./workspace/<plan_id>`. Subsequent: `git
   fetch origin && git reset --hard <git_ref>`.
3. `git checkout -B whilly/<plan_id>/<task_id> <git_ref>` so the working
   tree matches the canonical ref byte-for-byte before the agentic CLI
   runs.
4. Agentic CLI executes under `./workspace/<plan_id>` as cwd. It can
   read / write / commit at will; the commit history is local to the
   per-task branch.
5. On agent success: `git push origin whilly/<plan_id>/<task_id>` (with
   `--force-with-lease` so re-runs of the same task overwrite the prior
   attempt). On agent failure: branch stays local; nothing pushed.
6. Worker calls `POST /tasks/complete` (or `/fail`) with
   `event_payload.branch_ref = "whilly/<plan_id>/<task_id>"`.
7. Control-plane writes the `COMPLETE` event with that payload. An
   operator (or Forge integration) reads the ref and opens a PR.

### Failure modes

* **Agent fails mid-edit.** Worker emits FAIL; the per-task branch is
  *not* pushed. The local branch can be inspected with `git log` for
  forensics. Re-claim by another worker creates a fresh branch on a
  fresh clone — no contamination.
* **Worker crashes after `git push` but before `complete`.** Visibility
  timeout fires (≤30 s default), control-plane releases the claim, a
  peer worker re-runs the task, force-pushes its own attempt over the
  same branch name. The "loser" attempt is replaced byte-for-byte;
  `git reflog` retains it for forensics on the worker that finished
  first.
* **Two workers race on the same task before the lost-race path.** The
  state machine's `FOR UPDATE SKIP LOCKED` already prevents two workers
  from holding the same `task_id` claim simultaneously, so this case
  never happens at the workspace layer. M4 inherits the M0 invariant.
* **Disk pressure on a worker.** Per-plan clones grow unbounded.
  Operators can prune via `whilly worker workspace gc` (a future
  subcommand) or just `rm -rf ./workspace/<plan_id>` on idle workers.
  M4 tracks last-used timestamp per clone.

### Worked example — operator-side trace (Option A)

The block below is a copy-paste-runnable trace of the Option A flow
from the operator's POV. It uses the canonical branch-naming sketch
(`worker-<id>/<plan_id>/<task_id>`) and the `--force-with-lease` push
policy locked in above. The example assumes one worker host (call it
`worker-A`) draining one plan (`demo`) against the operator's git
remote `origin`.

```bash
# 1. First-claim setup on worker-A (one-time per plan):
#    `git clone --branch worker-A` carves out a per-worker namespace
#    on the operator's remote so two workers cloning the same repo
#    don't accidentally collide on a shared default branch. This is
#    the canonical worked example for Option A.
git clone --branch worker-A git@github.com:org/repo.git ./workspace/demo
cd ./workspace/demo

# 2. Per-task lifecycle on a CLAIM (illustrative — the worker
#    automates this; an operator only runs it manually for forensics):
git fetch origin
git reset --hard <task.git_ref>
git checkout -B worker-A/demo/T-42 <task.git_ref>

# 3. Agentic CLI runs in the working tree...
#    (claude / opencode / codex — produces commits on the local branch)

# 4. On agent success, the worker pushes the per-task branch back
#    under the worker-A namespace:
git push origin worker-A/demo/T-42 --force-with-lease

# 5. Cleanup or re-claim (optional — `git push origin worker-A/...`
#    is the canonical handoff back to the operator's remote):
git push origin worker-A/demo/T-42 --force-with-lease
```

The `git clone --branch worker-A` and `git push origin worker-A/...`
commands are the two operator-visible touchpoints; everything between
is the agentic CLI's job.

### Design constraints baked in by M1

* The `./workspace` placeholder mount on `docker-compose.worker.yml` is
  declared but unused at M1 — a deliberate hook so operators who
  pre-create the directory today won't have to re-run compose when M4
  ships.
* Worker container hostname (set via `WHILLY_WORKER_HOSTNAME`) is
  surfaced in the `workers` table and event payloads, so per-worker
  branch ownership is auditable from the SQL side too.
* The `events.detail` jsonb column already exists (migration
  `003_events_detail`) — M4 stuffs `branch_ref` and `git_ref` there
  rather than introducing a new column.

---

## Option B — shared workspace, advisory locking

**Idea:** All workers mount the same NFS / EFS / shared volume. Per-task
isolation is provided by a per-task subdirectory plus an advisory lock
table on the control-plane.

**Why we ruled it out:**

* NFS lock semantics are notoriously hard to get right under a partial
  partition; the existing `workers.heartbeat_ts`-based offline detection
  composes badly with stale lock entries.
* Forces colocation: the workers must run in the same network as the
  shared volume. That defeats the entire M1 split-host story.
* Doubles the auth surface: workers now need credentials for both the
  control-plane and the shared volume.
* Operator debuggability is worse — `git status` on one host doesn't
  describe what another host is doing inside the same path.

The rejection still does not make Option B *evil*; on a fully-trusted
single-DC deploy with a battle-tested NFS, B is a reasonable shape. It is
not the right shape for the M1+ public-internet scenarios this mission
targets, so we do not pay the complexity tax.

---

## Option C — patch-based / queue

**Idea:** Worker sends the task's resulting diff back to the
control-plane as a unified-diff payload. The control-plane is responsible
for applying the diff to a canonical branch on a server-side bare repo.
Workers never push directly; they only POST.

**Why we ruled it out:**

* Inverts the dependency direction: the worker is no longer
  httpx-only — it must serialise diffs, reckon with binary blobs, etc.
  That bloats the worker import closure significantly.
* Loses byte-fidelity for edits the agent makes through tools that
  bypass git (e.g. installer scripts that write generated files).
* Makes operator forensics harder: the "what did the worker actually do"
  question becomes a server-side puzzle instead of a `git log` away.
* Loses the natural unit of audit (a git ref) — every task becomes a
  jsonb blob in `events.detail` instead of a git branch we can inspect.

C remains technically interesting for hostile-worker scenarios where you
do not trust workers with push access. That's a future mission, not
M4.

---

## Why A wins

* Reuses the worker's existing `git` dependency without adding a new
  protocol or daemon.
* Works identically across all M1 deployment shapes (loopback,
  `WHILLY_BIND_HOST=0.0.0.0`, Caddy, Tailscale Funnel) because the
  workspace layer talks to the upstream git remote directly, not through
  the control-plane.
* Per-task branches are a unit of audit operators already understand;
  PR-style review fits naturally on top.
* Failures degrade to "no branch pushed" — the worst case is identical
  to M3-era "task failed", just with extra forensic data on the worker.
* Composes with future M5 capability scoping: a per-task branch is a
  natural place to attach signed metadata (e.g. provenance attestations)
  later.

---

## Open questions deferred to M4

* **Branch naming convention.** Current sketch is `whilly/<plan_id>/<task_id>`.
  We may want to namespace by worker_id or by ISO date to reduce the chance
  of an operator hand-creating a colliding branch.
* **Force-push policy.** `--force-with-lease` is the safer default;
  `--force` is simpler but allows re-claim races to overwrite each
  other's reflog. We pick `--force-with-lease`.
* **GC policy.** When does a worker reclaim disk from old per-plan
  clones? Likely "after X days of no claim from this plan", but X is
  TBD.
* **Submodules / monorepos.** First-class submodule support is post-M4.
* **Read-only worker hosts.** A worker with no push credentials should
  fall back to FAIL with a clear error rather than silently emit a
  COMPLETE with no branch.
* **Conflict resolution between two task branches.** Out of scope —
  M4 keeps branches independent; merging is the operator's job.

---

## Risks of Option A

| Risk | Mitigation |
|---|---|
| Worker disk fills up from per-plan clones. | M4 ships `whilly worker workspace gc`; operator alarming via `df -h` is an existing best practice. |
| Branch name collision with operator's own branches. | `whilly/...` prefix is reserved by convention; M4 doc spells this out. |
| Force-push surprises a reviewer mid-PR. | `--force-with-lease` requires the local ref to match the remote, so a concurrent operator update will fail the push instead of overwriting it. |
| Push secrets exposed via argv / env are visible in `ps` output. | Use a git credential helper or GIT_ASKPASS hook rather than baking secrets into the remote URL itself; the M4 spec mandates this. |
| First-claim clone is slow on big repos. | Clone is per-plan (not per-task), amortised across N tasks; later tasks just `git fetch`. |

---

## Worked example end-to-end trace

The trace below is illustrative; the actual trace at M4 will be similar
but emitted from real code.

```
[control-plane] CLAIM {plan_id=P, task_id=T, git_ref=abcdef0, worker_id=Wb}
                                                         │
[Wb worker]  ensure ./workspace/P/.git                   │  (clone if missing,
             git fetch origin                            │   else fetch+reset)
             git reset --hard abcdef0                    │
             git checkout -B whilly/P/T abcdef0          │
                                                         │
[Wb worker]  agentic CLI runs in ./workspace/P           │  (claude/opencode/etc.)
             commits land on local branch whilly/P/T     │
                                                         │
[Wb worker]  git push origin whilly/P/T --force-with-lease
                                                         │
[control-plane] COMPLETE {task_id=T, branch_ref="whilly/P/T",
                          git_ref=abcdef0, worker_id=Wb}
```

The audit log thus captures both the input ref and the produced
branch ref — sufficient for an operator to reproduce, review, and
ship the result.

---

## Status / out-of-scope reminder

* This document is **design only** — **NOT implemented in this mission**.
  No code in v4.4 reads / writes `./workspace`; the placeholder mount on
  `docker-compose.worker.yml` exists for forward compatibility only.
* The companion deployment doc that *does* describe what's shipping today
  is [`docs/Distributed-Setup.md`](Distributed-Setup.md).
* Implementation lands in M4 (future mission) — not M2, not M3.
* If a v4.5 / v4.6 (M2 / M3) feature requires this design to evolve,
  please update this doc *first*, then evolve the feature; do not
  silently change the workspace topology.

> **One more time, for the operator skim-reading this section:** Option
> **A is locked in** as the workspace topology, but it is **NOT
> implemented in this mission**. M4 is a future mission. Read
> [`docs/Distributed-Setup.md`](Distributed-Setup.md) for the v4.4
> features that ship now.
