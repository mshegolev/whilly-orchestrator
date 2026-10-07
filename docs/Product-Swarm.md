# Product swarm

Enable `WHILLY_PRODUCT_SWARM=1` alongside the existing trusted
`WHILLY_SWARM_REGISTRY` configuration after running migrations. The dashboard's
**Swarm** link then opens `/swarm/product`; `/swarm` retains legacy sessions and
task Gantt. Both interfaces require an administrator session and same-origin
CSRF protection. Worker bearer credentials do not grant dashboard access.

## Research and evaluated improvements

The cockpit exposes durable daily-research reports and frozen experiment
evidence under **Research & evaluation**. Research schedules and optional
external export remain disabled by default and report the named setup blockers
`schedule_configuration_required` and `export_policy_required`. Manual dry runs
accept only already-redacted structured fixture events; retrieved text is
untrusted evidence and cannot change policy or invoke tools.

Experiments pin baseline/candidate revisions, dataset and held-out hashes,
rubric, policy, models, acceptance limits and TRIZ evidence. Missing costs stay
unknown, regressions reject recommendations, and only an authenticated owner may
record a decision. Acceptance and rollback records never authorize execution;
normal feature approval, publication and delivery boundaries still apply.

## Workflow

1. Discuss requirements with the product chief. Product messages persist across
   restarts; each feature has its own canonical swarm session.
2. Create/select a feature and set its model-call and elapsed-work budget.
3. Plan with an explicitly configured planner profile. Review the specification
   and acceptance criteria, then approve its server-computed revision digest.
4. Run up to five workers. Cheap workers use separate worktrees; verification
   commands run on the host and a separate cheap reviewer must approve.
5. A retryable failure gets at most two cheap attempts. Exhaustion asks the
   configured escalation planner to re-decompose; the changed plan requires
   fresh approval. The strong planner never implements code.
6. Accepted results enter publication review. Only configured, authorized
   GitLab targets can receive a push/MR. No automatic merge or deployment.

Changes to spec/budget invalidate approval. Registry or base-commit drift blocks
execution. Planning results based on a concurrently edited spec are rejected.
Stop revokes approval; interruption does not silently restart jobs or spend more
tokens. Legacy sessions remain manually operated and are not auto-converted.
Stop returns `202 stop_requested` with an explicit `drained` result. An in-flight
external push is drained and its receipt retained; cancellation cannot undo it.
Open the feature's canonical-session link to inspect the task plan and verification
commands before approving. Publication receipts retain the approved digest and
plan revision along with the exact SHA and MR link.

## Explicit model profiles

Private registry `profiles` entries contain `engine` (`claude` or `codex`),
`model` (explicit CLI model identifier), `reasoning_effort`, `max_turns`,
`timeout_seconds`, and optional `budget_usd`. Omit the monetary limit or use
`null` for subscription-backed CLI runs; unknown cost is not zero. If supplied,
the value must be positive and finite. Required execution profiles are
`worker-cheap` and `reviewer-cheap`. Optional `worker-cheap-claude` and
`worker-cheap-codex` preserve per-task engine selection without falling back to
an unapproved model. Planning choices are `planner-strong`,
`planner-strong-claude`, `planner-strong-codex`, and `planner-escalation`.
Missing choices block: there is no automatic expensive-model fallback.

Wall-clock timeout, feature call/elapsed budgets, and the global five-call cap
still apply without a monetary limit. The current Codex adapter does not enforce
per-process `budget_usd` or `max_turns`; these must not be presented as hard Codex
caps. Claude receives its supported CLI turn and optional monetary limits.

The engine's executable can be a configured argv wrapper for a shell function
such as `ch`; subprocess invocation never interpolates a browser-supplied shell
command. Use existing engine configuration for authentication. Never put tokens
in model profiles or prompts.

Every feature call reservation, including planning and review, consumes the
feature call limit. Failed or ambiguous reservations are not refunded. Elapsed
model work is counted without time spent waiting for human approval; run wall
time is also bounded. Unknown backend prices remain unknown, not zero. A USD
limit is only enforceable where the backend reports/supports it.

Global admission uses PostgreSQL serialization only while reserving one of five
durable slots. Model processes run concurrently. A crashed owner leaves its slot
occupied intentionally; an operator must establish that its process group is
dead before repairing the reservation. Do not delete active rows to unblock a
queue. Database outages fail closed.

## BMAD boundary

The private `bmad` configuration names a `skill_root`, `workflow_skills`, and an
optional explicit `resources` list and customization. Paths cannot escape the
trusted root. The adapter loads real instruction files and bounds their size;
it does not install skills or treat a workflow name as executed work.

Instruction-only mode rejects script-dependent workflows. For `bmad-spec`,
`mode: "host-spec"` plus an explicit `project_root` enables the host lifecycle:
resolve configuration/customization, initialize and append the canonical memlog,
then validate and persist a returned SPEC kernel and Markdown companions in a
private revision workspace. The planner remains read-only; it returns artifacts
in a `bmad-artifacts` fence and a separate canonical task plan. Artifact contents
are included in the approval binding. Existing capability IDs are supplied to
the next revision for continuity.

Custom activation/on-complete executable hooks are intentionally unsupported and
produce setup blockers. This adapter supports the specification lifecycle, not
arbitrary unattended execution of every BMAD skill. Real-model output quality
and semantic preservation still require acceptance, beyond host protocol tests.

## Publication policy

Private registry `publication` maps a project id to `remote_url`, numeric
`project_id`, `target_branch`, `branch_prefix` starting with `swarm/`,
`gitlab_url` (HTTPS), `token_env` (explicit credential variable name),
`ci_safe: true`, and `enabled: true`. Enabling publication authorizes source
branch pushes and MR API writes; first confirm that that project's CI cannot
deploy as a side effect. There is no default target or credential discovery.

One stable source branch is retained per feature/project. The candidate SHA must
contain every accepted project result; divergent heads require explicit
integration. Dirty or changed candidates cannot publish. Receipts persist in
PostgreSQL. Existing MRs are reused; missing/failed exact-SHA CI is not ready.
Use Publish to refresh a pending result. No force push or direct target-branch
write is allowed.

## Verification scope

Tests with fake agents/GitLab prove orchestration contracts, not real model
quality, provider authentication, production evaluation, or live MR delivery.
Real acceptance requires explicitly selected models and an authorized test
repository. Never run an arbitrary business feature merely to verify the UI.

## Planner setup readiness

The cockpit shows which supported planner profiles exist in the trusted registry.
It starts with no selected planner; choose a configured profile explicitly to
enable Plan/Discuss. A missing profile produces `planner_setup_required` before
any background job or feature update. Refresh the page after changing registry
configuration. Existing recorded blockers are history, not cleared automatically.
No worker profile is silently promoted to planner and no expensive fallback is
chosen. Actual model/provider credentials, budgets and model-quality acceptance
remain explicit operator setup, separate from infrastructure tests.

## Durable collaboration (L2)

The product cockpit includes a read-only **Agent collaboration** inspector:
recipient project/role, persisted/delivered/acknowledged/expired state, payload
and evidence. Viewing history never marks a message delivered or runs a model.
Acknowledgment is receipt, not approval or task completion.

Delivery is disabled until the operator explicitly configures
`WHILLY_SWARM_DELIVERY_POLICY` as JSON with exactly three positive integer
fields: `max_payload_bytes`, `max_hops`, `max_fanout`. There are no implicit
limits or model/provider fallbacks. Missing configuration is reported as
`message_delivery_policy_required`. This installation does not activate it.
Malformed policy fails application composition rather than silently selecting
fallback limits; correct the private configuration before restarting.

Product workers use `whilly swarm collaborate --inbox` and
`whilly swarm collaborate --request '<JSON>'` through their coordinator-created
local mailbox, without database credentials. Send requests contain `op: send`,
recipient project/role, kind, correlation/idempotency keys, timezone-aware
expiry, a payload object and evidence references; replies may add `causation_id`.
Acknowledgments contain only `op: ack` and `message_id`. The coordinator pins
sender/product/feature/task and derives hop depth. Stable retries must retain
the same content and expiry. Requests are queued locally first; only a durable
receipt confirms persistence.

Send grants and inbox-read grants are separate. A worker can address registered
roles only in projects allowed for its assigned role; it reads only its own
project/role inbox. Manual API sends require an administrator session and CSRF
validation. Neither channel can dispatch tasks or expand execution permissions.
Legacy session messaging remains unchanged. Additive migration038 preserves
existing session and task history; schedules and external exports stay disabled.

## Cross-project proposals (L3)

Product workers can submit data-only neighboring-work proposals using
`whilly swarm propose-task --request '<JSON>'`. This is distinct from the
existing `propose --plan-file` canonical-plan command. Required request fields:
`op: propose`, `target_project`, relative `target_module`, `evidence_refs`,
`outcome`, `contract_impact`, `acceptance`, `dependencies`, `resource_class`.
Array fields must be arrays of strings; dependencies use `proposal:<id>` or
`task:<canonical-id>`. The coordinator supplies the actor, product, origin
feature/task and retry identity. Targets must be registered and within the
worker role's project grants. Workers receive no owner-decision authority.

The cockpit's **Cross-project proposals** panel shows evidence, blockers and
append-only history. Reads never call a model. An authenticated administrator
may accept an eligible proposal **for planning** with a reason and the displayed
feature revision. This atomically preserves the previous specification, adds
the proposal, creates a new revision and clears both old approval digests.
The normal Plan -> review -> Approve -> Run workflow remains mandatory; existing
Gantt dependencies appear only after actual canonical planning, not on submission.

Unknown/cyclic dependencies, protected configuration paths, unavailable/exhausted
feature budgets, running features and active work in the target project block
admission. Current safe mode does not yet have a host-bound producer/consumer
contract-proof type: any `contract_impact` other than exact `none` remains blocked
with `contract_verification_proof_type_missing`. Editable specifications or an
agent's claimed verification are never accepted as proof.

Rejection with a reason is available only before a proposal enters a plan.
To withdraw accepted/planned work, use the originating feature's normal edit or
stop workflow; merely changing a proposal label must not leave executable work
silently authorized. Proposal delivery receipts are not approvals. Migration039
adds canonical proposal storage without modifying project repositories or
enabling any research schedule, export, model call, merge or deployment.
