# Distributed Whilly — Gap Analysis & Roadmap Input

> **Scope.** Turning Whilly Orchestrator (v4.3.1, image `mshegolev/whilly:4.3.1`)
> from a single-host demo (Postgres + control-plane + worker on one Docker
> network) into a true multi-host distributed system: control-plane on a
> VPS, 2–5 colleague laptops / cloud VMs running `whilly-worker` against
> it, all draining the same plan with no overlap and a full audit trail.
>
> **Source of truth for "what exists today".** Cross-references in this
> doc point at:
>
> * `whilly/adapters/transport/server.py` — FastAPI app factory, claim
>   long-poll, visibility-timeout / offline sweeps.
> * `whilly/adapters/transport/auth.py` — bootstrap-token + per-worker
>   bearer dependencies, SHA-256 hashing of `workers.token_hash`.
> * `whilly/worker/remote.py` + `whilly/cli/worker.py` — httpx client
>   loop, `--connect / --token / --plan` flags.
> * `docker-compose.demo.yml` — current single-host stack (postgres,
>   control-plane, worker; all bound to `127.0.0.1`).
> * `docs/Whilly-v4-Architecture.md` + `docs/Whilly-v4-Worker-Protocol.md`
>   — the wire-level contract.
>
> **Severity scale.** *blocker* = remote workers cannot function at all;
> *high* = they function but the system is unsafe / unsupportable for
> a real team; *medium* = friction we can ship around but should fix;
> *low* = polish.
>
> **Effort scale.** S = ≤1 day · M = 2–5 days · L = >1 week.

---

## 1. Network Exposure & TLS

### Gap
The demo binds **all** services to `127.0.0.1` (`docker-compose.demo.yml`
ports `127.0.0.1:8000:8000`, `127.0.0.1:5432:5432`). FastAPI runs plain
HTTP — no TLS terminator, no HSTS, no rate-limiter. The Python client
(`whilly/adapters/transport/client.py`) accepts any `--connect` URL but
has no client-side cert pinning, no minimum-TLS enforcement, no proxy
helpers. `WHILLY_CONTROL_URL` is plumbed through env without guard
rails — a colleague who points a worker at `http://control.example.com:8000`
ships per-worker bearers in cleartext over the public internet.

### Severity
**Blocker** for any topology that crosses an untrusted network (laptops
on different WiFi, VPS-to-VPS over the public internet). Acceptable only
for a same-LAN demo (M1 below).

### Solution sketches

1. **Reverse proxy with Let's Encrypt (Caddy / nginx / Traefik).**
   *Tradeoff:* zero code change in Whilly — slap Caddy in front of
   uvicorn, point a DNS A-record, ACME handles certs. Lowest friction
   for "VPS with a public domain". Cost: operator must own the domain
   and keep ports 80/443 open. *Cite:* the GitHub Actions self-hosted
   runner installer assumes exactly this shape; Tailscale Funnel does
   it without DNS ownership.
2. **Tailscale (or WireGuard) mesh.**
   *Tradeoff:* worker boxes join a private overlay; control-plane is
   reachable only at `100.x.x.x` (Tailscale CGNAT space). Ships with
   mTLS-equivalent auth via WireGuard keys + identity, no public DNS,
   no cert rotation, NAT traversal solved. Best fit for "5 colleague
   laptops". Cost: every participant installs Tailscale (light, but
   non-zero).
3. **Cloudflare Tunnel / ngrok for ad-hoc laptop control-planes.**
   *Tradeoff:* operator runs control-plane on a laptop, exposes via
   tunnel, hands out a public URL. Great for the "show the demo to
   a colleague tonight" use-case; bad as a long-running posture
   (ngrok URLs rotate, Cloudflare tunnel needs a CF account).
4. **mTLS in addition to bearer for VPS-to-VPS.**
   *Tradeoff:* belt-and-suspenders — defeat of either bearer or TLS
   alone doesn't compromise the cluster. Cost: cert provisioning on
   every worker. Recommend reserving for environments where the bearer
   itself is considered low-trust (e.g. shared by 50+ people).

The sweet spot for the "team of 5" target is **Caddy in front of the
control-plane VPS + Tailscale option for laptop participants**.

### Effort
M (Caddy reverse-proxy compose addition + Tailscale onboarding doc + a
worker-side `--insecure` opt-out flag for local dev so we never silently
allow plain HTTP against a non-loopback host).

---

## 2. Authentication & Trust

### Gap
Today's auth (per `whilly/adapters/transport/auth.py`):

* **One** cluster-wide bootstrap secret (`WHILLY_WORKER_BOOTSTRAP_TOKEN`)
  authorises `POST /workers/register`.
* Per-worker bearer minted at registration, hashed (`SHA-256`) into
  `workers.token_hash`; the partial UNIQUE index pins lookup
  determinism. Revocation = `UPDATE workers SET token_hash = NULL`.
* No identity beyond `workers.hostname` (free-form string, no
  attestation). No mapping of "human ↔ worker_id". No expiry / TTL on
  per-worker bearers. No audit row links a registration to a specific
  human/host beyond what the worker self-reports.
* Legacy fallback to a *cluster-shared* `WHILLY_WORKER_TOKEN` is still
  honoured (with a deprecation warning) — a v4.x compromise of one
  worker leaks the cluster bearer.

For a "team of 5 colleagues" model this is too coarse: there is no way
to revoke "Alice's tokens" specifically, and no way to tell from an
event row whether the worker was Alice's laptop or a hijacked VPS using
Alice's bootstrap secret.

### Severity
**High.** The system *runs* in this state; what fails is *forensics
after a leak* and *operator confidence*.

### Solution sketches

1. **Per-user bootstrap secrets (multi-tenant bootstrap).**
   Replace the single `WHILLY_WORKER_BOOTSTRAP_TOKEN` env with a small
   `bootstrap_tokens` table: `(token_hash, owner_email, expires_at,
   revoked_at)`. Operator mints a token per colleague via
   `whilly admin bootstrap mint --owner alice@…`. Registration carries
   `owner` forward into `workers.owner_email`, every event in the
   audit log gains a stable human attribution.
   *Tradeoff:* small migration + new admin CLI; biggest UX win.
2. **Token TTL + automatic rotation.**
   Per-worker bearers expire (e.g. 24h); the worker hits
   `POST /workers/{id}/refresh-token` (new endpoint) using the soon-to-
   expire bearer to mint a successor. *Tradeoff:* keeps short-lived
   credentials on disk only, but the refresh path itself is now a
   target — has to be carefully rate-limited. *Cite:* GitHub Actions
   runners do this with 1h JIT tokens; Buildkite agent has a similar
   pattern.
3. **Revocation surface.**
   `whilly admin worker revoke <worker_id>` flips `token_hash = NULL`
   *and* terminates any in-flight claim by the same worker via
   `release_stale_tasks` with a special reason. Today a revocation
   leaves the in-flight task ticking until visibility timeout — fine
   for crash recovery, slow for active eviction.
4. **OIDC / GitHub-OAuth bootstrap (longer-term).**
   Worker performs an OIDC device-code flow against the operator's
   identity provider; control-plane verifies the issued JWT and only
   then issues a per-worker bearer. *Tradeoff:* heavy (operator must
   run / configure an IdP) but kills "DM-the-bootstrap-token" entirely.

### Effort
M for #1 + #3 (per-user bootstrap + revocation). L if we adopt #4.

---

## 3. Workspace Topology

### Gap
**This is the single biggest unanswered design question for going
distributed.**

Today, the worker process invokes `CLAUDE_BIN` as a subprocess
(`whilly/adapters/runner/claude_cli.py`); the agentic CLI inherits the
worker's CWD and has free access to whatever filesystem is mounted
there. In the demo container, that's `/opt/whilly` — there is no
"target git repository". The worker emits a `<promise>COMPLETE</promise>`
flag based on stdout but the *file edits never make it back to a
canonical place*. The plan JSON in `tasks` rows has `key_files`
references that name files, but no contract for where those files
physically live or how worker A's edits to `whilly/main.py` reach
worker B for a follow-up task.

For a single-host demo this is invisible (everyone shares one FS).
For multi-host, picking a workspace model is the gating decision —
nothing else can be designed without it.

### Severity
**Blocker** — the system can claim/complete state-machine rows fine
across hosts today, but no actual *code change* survives a remote run.
Until this is decided we can demo "distributed task scheduling" but not
"distributed code-editing agents".

### Solution sketches (three coherent topologies)

#### A. Per-worker scratch repo + push-branch
Each worker clones the target git repo locally (e.g.
`~/.whilly/workspaces/<plan_id>/`); the agentic CLI edits files under
that clone; on `COMPLETE` the worker `git push origin
whilly/<plan_id>/<task_id>`; control-plane stores the branch name in
`tasks.payload` and a follow-up "merge agent" (or human) opens a PR.

* **Worker code changes.** New `whilly_worker.workspace` module:
  resolves repo URL from plan, clones / `git fetch`-updates, runs the
  agent in that CWD, pushes branch on success, releases on fail.
* **Control-plane API changes.** Plan needs `repo_url` +
  `repo_default_branch` fields. New `POST /tasks/{id}/result` carrying
  `{branch_name, commit_sha}` for the merge step. `claim` needs to
  return the parent branch the worker should fork from
  (so two workers don't fork from each other's mid-flight branches).
* **Plan schema.** Add `repo_url`, `default_branch`,
  `merge_strategy ∈ {rebase, merge, squash}` to the plan; per task,
  optional `base_branch` override for stacked work.

* **Pros.** Mirrors GitHub Actions / Buildkite — operators understand
  it. Each worker is sandboxed; an agent that goes rogue can only mess
  up its own clone. Auditable by `git log` outside Whilly.
* **Cons.** Cross-task dependencies become a merge-conflict problem.
  Two workers editing overlapping `key_files` fork off the same base
  and the "later" merge has to rebase. Whilly's existing `key_files`
  collision avoidance in batch planning helps, but doesn't eliminate
  it.

#### B. Shared workspace (NFS / object-storage / S3-FUSE)
All workers mount a single shared filesystem; the agent edits in
place; `key_files` collision avoidance + optimistic-locking in the
state machine are the only collision controls.

* **Worker code changes.** Almost none — the worker just `chdir`s into
  the mount.
* **Control-plane API changes.** None for the wire.
* **Plan schema.** Optional `mount_point` hint.

* **Pros.** Lowest code change. Agents see the *current* state of the
  project always.
* **Cons.** *Operationally awful.* NFS over the public internet is
  slow and brittle; agents writing concurrently to the same file
  produce silent corruption no Whilly invariant catches. The "5
  laptops + 1 VPS" target makes a shared FS implausible.

#### C. Patch-based (control-plane stores patches, applies centrally)
Worker runs the agent against a *frozen snapshot* (tarball downloaded
from control-plane at claim time), captures the diff with `git diff`,
uploads the patch via a new `POST /tasks/{id}/patch`. Control-plane
applies patches sequentially in a single canonical workspace on the
VPS.

* **Worker code changes.** Workspace module + `git diff` capture +
  patch upload helper.
* **Control-plane API changes.** Snapshot endpoint
  (`GET /plans/{id}/snapshot.tar.gz`), patch upload endpoint, server-
  side queue + applier worker (single-writer to the canonical repo).
* **Plan schema.** Plan owns the canonical repo path on the VPS.

* **Pros.** Single linear history; no cross-worker merge conflicts at
  push time (they're materialised at apply time, where the operator
  can see them). Workers don't need git push credentials.
  Strong audit story — every patch is in the events log.
* **Cons.** Snapshot download per claim is heavy (gigabyte repos
  become a problem); patch-apply ordering is now a critical-section
  bottleneck. *Cite:* this is roughly the Phabricator Differential /
  Gerrit shape.

### Recommendation
Ship **A (per-worker scratch repo + push-branch)** as the v5 default.
It matches operator intuition (everyone has used a CI runner that
clones, edits, pushes), keeps the control-plane stateless w.r.t. the
target repo, and we can graft B/C in later for niche cases. Document
B as "single-host fallback" and C as a "future option for monorepos".

### Effort
L for option A (workspace module + plan schema migration + push-branch
flow + a follow-up merge agent). M for option B (mostly docs +
per-worker `--workspace-path` flag). L+ for option C.

---

## 4. Secret Distribution

### Gap
Each remote worker needs at least one of `ANTHROPIC_API_KEY`,
`OPENAI_API_KEY`, `GEMINI_API_KEY`, `OPENROUTER_API_KEY`,
`GROQ_API_KEY` to drive its agentic CLI. Today the only mechanism is
"set the env var on the worker host before `docker run`" —
i.e. the operator tells a colleague "paste this into a `.env`" via DM.

There is no central secret store, no rotation, no per-worker scoping
("Alice's worker should only have access to *her* Anthropic key, not
the operator's"), and no telemetry on which key is being used by
which worker.

### Severity
**High.** Operationally fragile (a leaked key takes a Slack scroll-back
to track) and a privacy issue (operator who fronts the keys ends up
seeing every colleague's traffic on their account).

### Solution sketches

1. **BYO keys (recommended).**
   Each colleague brings their own provider key; operator never sees
   it. Worker reads from local env / keychain. The control-plane gains
   a `worker_capabilities` payload at registration (`{"providers":
   ["anthropic", "openai"], "max_cost_per_task_usd": 0.50}`) so the
   scheduler can route tasks to workers that can pay for them.
   *Tradeoff:* trivial code change (already supported via env), but
   shifts the cost question to the colleague — fine for a friendly
   team, awkward for a paid arrangement.
2. **Operator-mediated short-lived bundles.**
   Operator stores keys server-side (`secrets` table, encrypted at
   rest with a KMS / age key); worker fetches a short-lived signed
   bundle at registration via a new
   `GET /workers/{id}/secret-bundle?expires_at=...` endpoint. The
   bundle lands as ENV in the agent subprocess only, never on disk.
   *Tradeoff:* central control, central cost, central blast radius
   if the secrets table leaks. *Cite:* HashiCorp Vault's
   "approle-at-startup" pattern, AWS STS AssumeRole.
3. **Hybrid.**
   BYO by default; operator-mediated as opt-in with a per-worker flag
   on the bootstrap token (e.g. "Alice gets the cluster keys, Bob
   doesn't"). Marries the autonomy of #1 with the operator-pays
   convenience of #2.

### Effort
S for #1 (already mostly works — needs a docs page + capability
advertisement). M for #2 (new endpoint + on-disk crypto). M for #3.

---

## 5. Observability Across Nodes

### Gap
* **Dashboard.** `whilly dashboard` reads Postgres directly via asyncpg
  (`whilly/cli/dashboard.py`) — operator must run it on a host that
  can reach the DB. No web UI. No worker-status grid.
* **Logs.** Each worker writes its own stdout / `whilly_logs/`. There
  is no log shipping, no per-worker log endpoint on the control-plane,
  no filtered "show me what worker-on-VPS-A did in the last 5 min"
  view.
* **Metrics.** No Prometheus endpoint, no OpenTelemetry traces. The
  audit log (`events` table) is rich enough to *reconstruct* everything,
  but operators have to write SQL.
* **Streaming.** No `/events/stream` SSE endpoint — the dashboard
  polls every second.
* The lifespan-owned `EventFlusher` (`whilly/api/event_flusher.py`)
  batches `INSERT INTO events` so per-event diagnostics are already
  cheap; we just don't surface them externally.

### Severity
**High.** "Operator wants to see 5 worker streams at once" is the
explicit user-vision requirement; nothing in v4.3.1 ships this.

### Solution sketches

1. **Web dashboard, server-side rendered.**
   FastAPI Jinja template at `GET /` rendering the same
   `_SELECT_DASHBOARD_ROWS_SQL` projection as the TUI, with HTMX
   `hx-trigger="every 2s"` for live refresh. *Tradeoff:* one-day
   spike, no JS framework, ships in the same container as the API.
2. **SSE event stream.**
   `GET /events/stream` (bearer-auth) tails the events table via
   asyncpg `LISTEN/NOTIFY`. Clients (web UI, log forwarder, an
   operator's `curl -N`) subscribe. *Tradeoff:* the right primitive
   for "5 worker streams in one place"; cost is an extra asyncpg
   listener connection on the pool.
3. **Prometheus `/metrics` endpoint.**
   Counts/gauges for: tasks by status, claim queue depth, worker
   online/offline counts, cost spent vs. budget. *Tradeoff:* tiny
   diff, opens the door to Grafana dashboards. Use
   `prometheus-client` (already a Python ecosystem default).
4. **Structured per-event JSONL on the control-plane.**
   Mirror every `events` row to `/var/log/whilly/events.jsonl`
   for filebeat / Loki / Vector to ship. *Tradeoff:* zero code on
   the worker side, all aggregation happens centrally.
5. **Worker-to-control-plane log shipping.**
   Worker tail-uploads its stdout in 1 KB chunks via
   `POST /workers/{id}/logs`. *Tradeoff:* makes "see worker-A's last
   5 min" trivial; cost is bandwidth + a new endpoint that has to be
   rate-limited or it becomes an exfil channel.

### Effort
S for #3 (Prometheus). M for #1 + #2 (web UI + SSE). M for #5 (worker
log shipping). The vertical slice "web UI + SSE + Prometheus" is M.

---

## 6. Network-Partition Resilience

### Gap
Today's resilience floor (per `repository.py` + `server.py`):

* **Visibility timeout.** `release_stale_tasks` runs every
  `WHILLY_VISIBILITY_TIMEOUT_INTERVAL` seconds and reverts CLAIMED /
  IN_PROGRESS rows whose `claimed_at` is older than the timeout
  (default 15 min, `VISIBILITY_TIMEOUT_DEFAULT_SECONDS = 15 * 60`).
* **Heartbeat.** Workers POST `/workers/{id}/heartbeat` every 30s; a
  separate sweep flips `workers.status='offline'` after 2× the
  interval (`HEARTBEAT_STALENESS_DEFAULT_SECONDS`) and releases the
  worker's in-flight tasks.
* **Graceful shutdown.** SIGTERM/SIGINT path in `run_remote_worker_with_heartbeat`
  calls `POST /tasks/{id}/release` before exiting.

What's missing for flaky-WiFi reality:

* **Worker-side retry policy.** The httpx client (`client.py`) raises
  on network errors; the loop catches and re-iterates, but each
  iteration re-does claim from scratch — there is no "I had task T-001
  in flight, let me try `complete` again with the same version" replay.
  A worker that loses connectivity *during* `complete` can produce a
  duplicate task run (it sees timeout, the server applied the update,
  worker re-claims a peer's next task — fine — but *its own* next
  iteration loses the audit trail of the in-flight task).
* **Heartbeat backpressure.** No exponential backoff if the worker
  can't reach the control plane; it just times out per call.
* **No persistent worker state.** Worker process keeps its
  `(worker_id, token, in_flight_task_id)` in RAM only. A restart →
  re-register → orphaned old worker_id row that ages out 15 minutes
  later.
* **15-min visibility timeout is too long for laptops.** A 15-min cap
  matches "developer wandered off for lunch"; for the 2–5 worker
  target, we want 60–120 s so a flaky-WiFi worker's tasks are
  re-claimable promptly.

### Severity
**Medium.** The system *recovers* — visibility timeout will sweep
abandoned claims; offline detection will flip the worker; peer
re-claims work. The user-visible failure is *latency* (15-min stuck
tasks) and the *audit gap* on the lost-RPC path.

### Solution sketches

1. **Tunable visibility timeout per plan.**
   Plan-level `claim_visibility_timeout_seconds` column; default to
   120 s for "interactive" plans; 15 min for batch jobs.
   *Tradeoff:* tiny migration; right knob for the right job.
2. **Local worker state file with replay.**
   Worker persists `(worker_id, token, in_flight_task_id, version)`
   to `~/.whilly/worker-state.json` after each transition. On
   restart, replay the *terminal* RPC (complete / fail) idempotently —
   the existing 409 envelope already carries `actual_status`, so the
   worker can detect "server already accepted my prior complete"
   without a fresh task being lost.
3. **Exponential backoff + jitter on heartbeat / claim.**
   Borrow the AWS SDK retry policy: 1s, 2s, 4s, … cap 60s, with
   random jitter. *Tradeoff:* trivial code change, big win for
   flaky-WiFi UX.
4. **Worker-side connection pool warming.**
   Pre-open the httpx HTTP/2 connection on startup; reuse across
   iterations (already true) and on a transport error, blow it away
   and reconnect rather than reusing a half-broken socket.

*Cite:* SQS + Lambda Event Source Mapping does exactly #1 + #2 (DLQ +
visibility-timeout-extension); Temporal workers do #2 + #3.

### Effort
S for #3 + #4. M for #1 + #2 (state file with replay).

---

## 7. Deployment Story

### Gap
Today there is exactly **one** ready-to-run distributed shape: the demo
compose file (`docker-compose.demo.yml`), which puts the control-plane,
Postgres, and worker on **one** host. Specifically missing for the
target user vision:

* No "control-plane only" compose (with optional Postgres) for putting
  on a VPS.
* No standalone worker installer. `whilly-worker` exists as a console
  script (`whilly/cli/worker.py`) and `whilly_worker/pyproject.toml`
  has its own meta-package, but the documented install path is
  "clone the repo, `pip install -e '.[dev]'`".
* No one-line bootstrap for a colleague: today they need (a) Docker,
  (b) the agentic CLI of their choice, (c) the bootstrap token
  delivered out-of-band, (d) the control-plane URL, (e) hand-edited
  env vars.
* No production compose / k8s manifest. (`docs/Whilly-Workstation-Bootstrap.md`
  exists but covers single-host.)

### Severity
**High.** Without this, "I run it on a VPS, colleagues connect" is a
multi-hour engagement, not the 5-minute onboarding the user-vision
implies.

### Concrete artifacts needed

1. **`docker-compose.control-plane.yml`** (new).
   Postgres + control-plane only. Caddy front-end as an opt-in profile.
   Healthchecks. Volume layout. Documented env-var cheat sheet.
2. **`docker-compose.worker.yml`** (new).
   Single-service compose for a colleague's laptop. Mounts a host
   workspace dir. Reads `.env.worker` with `WHILLY_CONTROL_URL`,
   `WHILLY_WORKER_BOOTSTRAP_TOKEN`, the agentic-CLI key of choice.
3. **`whilly worker connect <url>`** subcommand.
   One-line bootstrap: takes the URL + bootstrap token, registers,
   stores the per-worker bearer in OS keychain, runs the loop. Replaces
   today's three-step "register → grab token → set env → run".
4. **PyPI worker meta-package install path.**
   `pip install whilly-worker` (today the meta-package exists in
   `whilly_worker/`; verify it ships and pins
   `whilly-orchestrator==4.3.1`'s worker dep closure, no asyncpg /
   FastAPI). `pipx install whilly-worker` should Just Work.
5. **`curl … | sh` installer (last-mile UX).**
   Detects OS, installs Docker if missing, runs
   `whilly worker connect`. *Tradeoff:* nice for laptops, security-
   conscious users hate it; offer it as an alternative, not the
   default.
6. **k8s manifest** for `control-plane + Postgres` (`charts/whilly/`
   Helm chart). *Tradeoff:* L effort, defer to v5.x once the compose
   path is solid.
7. **Onboarding doc** at `docs/Distributed-Setup.md` walking through
   "VPS A → control-plane; laptop B/C/D → workers". Replaces
   `Continuing-On-Another-Machine.md`'s single-host scope.

### Effort
M for #1 + #2 + #3 + #7 (the must-haves). S for #4 (mostly verification
+ docs). S for #5 (small shell script). L for #6 (Helm chart).

---

## 8. Cost & Rate-Limit Awareness

### Gap
Today's budget guard (per migration `005_plan_budget.py` /
`repository.py`):

* `plans.budget_usd` + `plans.spent_usd` columns.
* `complete_task` atomically increments `spent_usd` by the worker-
  reported `cost_usd` and emits a `plan.budget_exceeded` sentinel
  event when over.
* Per-task: nothing. Per-worker: nothing. Per-provider: nothing.
* Rate limits are *not* tracked at all — workers smash provider APIs
  and discover 429s the hard way (whichever agentic CLI returns the
  error message gets surfaced in the FAIL reason).

For 5 workers × 4 agentic CLIs all hitting the same provider, the
likely failure mode is "Anthropic 5h-tier-3 quota burns at 14:30 on
Wednesday and three workers stall simultaneously" — the operator has
no early warning.

### Severity
**Medium.** Doesn't break correctness; produces ugly UX and surprise
bills.

### Solution sketches

1. **Per-worker `max_concurrent_claims` (lightweight).**
   Workers self-throttle: `--max-in-flight 1` (already implicit).
   Plan-level `WHILLY_MAX_PARALLEL` is enforced today by the local
   loop, not by the server — extend it server-side: `claim_task` checks
   "how many CLAIMED rows for this plan?" and 204s if at cap. Stops
   five workers all racing for plan capacity at minute zero.
2. **Per-provider rate-limit awareness in the cost-router.**
   Worker advertises `(provider, tier)` at registration; control-plane
   tracks per-provider claim rate; rejects claims that would put the
   plan over `<provider>_max_calls_per_minute`. *Tradeoff:* moves
   business-logic into the scheduler, but it's the only way to do this
   without per-worker bookkeeping.
3. **Per-worker daily / monthly cost cap.**
   Plan-level cap is in; add `workers.daily_budget_usd` so a colleague
   can self-impose a "no more than $5 of my key per day" guardrail.
4. **Cost telemetry → dashboard.**
   New column on the dashboard projection: "$ spent in last hour" and
   "API calls in last hour". Trivial extension of the existing
   `events` SELECT once we tag events with `cost_usd` (already
   captured in the COMPLETE payload).
5. **Failure-mode classification on 429s.**
   When the agent CLI returns an HTTP 429 / "rate-limit" structured
   error, the worker emits `fail_reason="rate_limit:<provider>"` and
   the server holds the row in PENDING with a `retry_after_seconds`
   hint instead of FAILED. *Tradeoff:* requires every CLI adapter to
   surface the rate-limit signal cleanly — varies by provider.

### Effort
S for #1 + #4. M for #2 + #5. S for #3.

---

## Proposed Vertical-Slice Roadmap (M1 → M6)

Each milestone leaves the system **in a coherent, demoable state**.
Each delivers a step on the path to the user-vision; none is a pure
refactor / "infra week".

### M1: Two laptops, same WiFi, no TLS yet
**Goal.** Prove the core invariants survive a real-world two-host
topology before adding any new code paths.

**What changes.**
* Add `docker-compose.control-plane.yml` + `docker-compose.worker.yml`
  for two-host LAN deploys.
* Bind control-plane to `0.0.0.0:8000` only when a new
  `WHILLY_BIND_HOST` env is set (default still `127.0.0.1` to fail
  safe).
* Workspace stays on each laptop's local FS, **no push-back yet** —
  document the limitation explicitly.

**Why this slice ships value.** Smallest possible step: confirms the
state machine works under genuine network split, surfaces any
single-host assumptions early.

**Demo.** Operator runs control-plane on laptop A; colleague's laptop
B runs `whilly-worker --connect http://laptop-a.local:8000 ...`; both
laptops pull from one plan, no overlap. Visible proof: dashboard on
laptop A shows two distinct `worker_id`s, each with claims attributed.

---

### M2: TLS + Tailscale + per-user bootstrap tokens
**Goal.** Make M1 safe for the public internet.

**What changes.**
* Caddy reverse-proxy as an opt-in profile in
  `docker-compose.control-plane.yml`; ACME via Let's Encrypt by
  default, Tailscale Funnel as a documented alternative.
* Per-user bootstrap tokens (#2.1 above): `bootstrap_tokens` table,
  `whilly admin bootstrap mint --owner <email>` CLI; `workers.owner_email`
  column propagated through events.
* `whilly-worker --insecure` flag required to talk plain HTTP to a
  non-loopback host (defence against fat-fingered colleague configs).

**Why this slice ships value.** Smallest delta on top of M1 that makes
"I host on a VPS, friends connect" not a security hazard.

**Demo.** Operator's VPS hosts control-plane behind `control.example.com`
with a real cert; three colleague laptops connect with three distinct
bootstrap tokens; operator runs `whilly admin bootstrap revoke alice@…`
and watches Alice's worker drop off the dashboard.

---

### M3: Web dashboard + SSE event stream + Prometheus metrics
**Goal.** Operator can see all five workers in one place without SSH'ing
into the VPS.

**What changes.**
* `GET /` web UI (HTMX, no JS framework) with a worker-status grid +
  per-task last-5-events feed.
* `GET /events/stream` (SSE, asyncpg LISTEN/NOTIFY) — dashboard
  consumes; CLI clients can `curl -N` for live audit feed.
* `GET /metrics` (Prometheus) — operators can wire a Grafana board.

**Why this slice ships value.** Closes the #1 user-pain ("I can't see
what workers are doing"); also forms the foundation for cost and
rate-limit telemetry in M6.

**Demo.** Operator opens `https://control.example.com/` on a phone,
watches three colleagues' workers churn through tasks in real time;
opens Grafana, sees claim rate, cost-per-hour.

---

### M4: Per-worker scratch repo + push-branch workspace
**Goal.** Make the agent's edits actually mean something across hosts.

**What changes.**
* Plan schema gains `repo_url`, `default_branch`, `merge_strategy`.
* New `whilly_worker.workspace` module: clones / fetches per
  `(plan_id, worker_id)`; runs the agent in that CWD; on COMPLETE,
  pushes `whilly/<plan_id>/<task_id>` and reports the branch name via
  a new `POST /tasks/{id}/result`.
* Control-plane records branch names; documented "merge agent" recipe
  (a separate `whilly-merge` worker, optional) consumes them and opens
  PRs / does fast-forward merges.

**Why this slice ships value.** This is the milestone that turns
"distributed task scheduling" into "distributed code editing" — the
real product story.

**Demo.** Five colleague workers chew through a 50-task plan against
`github.com/operator/playground.git`; 47 of those tasks land as
auto-merged commits on `main`; 3 get human-review PRs; the audit log
ties every commit SHA back to a `(worker_id, owner_email, task_id)`
triple.

---

### M5: BYO secrets + capability-aware scheduling
**Goal.** Each colleague pays for their own LLM calls; operator's
billing stays sane.

**What changes.**
* `worker_capabilities` payload at registration: list of providers
  the worker can drive, optional per-task max-cost.
* `claim_task` filters by capability: a task tagged `provider=anthropic`
  is only handed to workers that advertised `anthropic`.
* Plan schema gains optional `task.preferred_provider` and
  `task.estimated_cost_usd`.
* Docs walking through "Alice uses her own Anthropic key, Bob uses
  Groq, both work the same plan".

**Why this slice ships value.** Operator's own keys stop being a single
point of failure (cost or compromise); plans can reach more capacity
by mixing providers.

**Demo.** A 100-task plan; three workers with three different provider
keys; the operator never set their own key. Final cost report shows
each colleague's spend separately.

---

### M6: Cost guards + rate-limit awareness + flaky-WiFi resilience
**Goal.** Production-grade operations: nothing surprises the operator.

**What changes.**
* Server-side enforcement of `WHILLY_MAX_PARALLEL` + per-provider
  rate-limit caps; 429 responses from agent CLIs translate to a
  PENDING-with-retry-after row instead of a FAILED.
* Configurable per-plan visibility-timeout (replace the 15-min default
  with 120 s for interactive plans).
* Worker-side state file (`~/.whilly/worker-state.json`) with replay
  of in-flight terminal RPCs on restart.
* Exponential backoff + jitter on heartbeat / claim.

**Why this slice ships value.** Closes out the "real-world deployment"
story: no more 15-min ghost tasks, no more midnight quota surprises,
crash-restart of a colleague's laptop replays cleanly.

**Demo.** Pull the cable on a colleague's laptop mid-task; 90 s later,
a peer worker has re-claimed and finished it; the original colleague
plugs back in, replays, exits cleanly without re-running the
already-completed task. Total observable artefact: the audit log shows
exactly two CLAIM events, one RELEASE (visibility timeout), one
re-CLAIM, one COMPLETE — no double-write.

---

## Beyond M6 (out of scope for this analysis)

Items deliberately deferred but worth flagging:

* **Helm chart / k8s manifest** (#7.6). Wait for ≥3 production users
  asking for it.
* **OIDC bootstrap** (#2.4). Wait for the first compromise scare or
  for a 50-person team that outgrows per-user bootstrap tokens.
* **Patch-based workspace topology** (#3.C). Reserve for monorepo
  users where push-branch produces too many merge conflicts.
* **Multi-region control-plane.** Whilly is single-region single-DB
  by design; if anyone needs HA, do it at the Postgres layer
  (managed Postgres + read-replica failover) rather than in Whilly.
