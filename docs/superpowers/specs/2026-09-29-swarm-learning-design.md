# Swarm memory, collaboration and continuous improvement

Status: written specification approved by owner on 2026-09-29; implementation plans await review.
Date: 2026-09-29.
Epic: SWARM-LEARNING.

## Intent and agreed boundaries

The owner wants one product chief, repository specialists that exchange knowledge
and propose work for neighboring modules, current shared context, and a daily
research-and-retrospective cycle. Success means better product outcomes with
less repeated investigation, not more agents, messages or generated tasks.

The owner approved the following conversational design: autonomous research,
knowledge exchange and task proposals; execution only inside approved scope,
permissions and budgets. This approval does not activate a schedule, authorize
new providers or data exports, or approve the implementation plan.

Initial release is observe-and-propose. No autonomous policy changes, protected
configuration edits, merges, deployments or production-data access. Later
low-risk automation requires separately approved activation and measured evidence.

## Architecture and sources of truth

Extend the existing product/feature/session and mailbox mechanisms; do not build
a competing task system. Git owns code, contracts and versioned specifications.
PostgreSQL owns durable workflow state, knowledge metadata, messages and proposals.
An optional search index is derived and rebuildable; it cannot authorize work.

Domain policies describe freshness, visibility, proposal eligibility and outcomes.
Application use cases compose these policies through ports for storage, search,
source verification, research, evaluation and scheduling. Git, HTTP, PostgreSQL,
Langfuse and model providers are adapters. Dependency tests enforce inward imports.
No requirement to introduce a vector database or additional model in release one.

Private product knowledge stays in private configured storage. Public source code
contains neutral schemas/examples only. Existing authentication and approval
checks remain mandatory for new API/UI surfaces.

## L1 — Versioned shared knowledge

Each knowledge revision records:

- Stable id, product/project scope, kind (fact, hypothesis, decision, observation).
- Content, evidence references and hashes, source URI/path, source revision/SHA.
- Author identity, observed/verified/expiry timestamps, verifier and policy version.
- Visibility/data classification, status and superseded/conflicting revision ids.

Lifecycle: candidate -> verified -> stale/superseded/retracted. Contradictory
evidence creates a conflict; newest text does not silently overwrite truth.
Model confidence alone never promotes a candidate to a verified fact. Verifiable
code claims require source checks; decisions require authorized decision records.
Hypotheses remain explicitly labeled even when frequently retrieved.

Freshness is source-specific: code/contract claims compare the relevant revisions;
external claims use explicit expiry and revalidation. Missing or inaccessible
sources yield unknown/unverified, not fresh. A changed source triggers recheck,
not an automatic assertion that every previous fact is false.

At task start, construct a bounded context package from the approved task, project
map, relevant contracts, verified knowledge, conflicts and known failures. Include
citations, freshness state, omissions and a manifest of exact revisions used.
Filter access before retrieval/ranking, not after passing content to a model.
Material contract changes trigger approval revalidation; unrelated new knowledge
does not silently change execution authority.

Retention periods are explicit private configuration by data class; missing
retention configuration blocks session ingestion. Support deletion/redaction of
content and derived indexes while retaining non-sensitive audit tombstones.
Do not retain hidden model reasoning, credentials or unrestricted raw transcripts.

## L2 — Durable agent communication

Message envelope: message id, product/feature/task ids, authenticated sender,
recipient role/project, type, correlation/causation ids, idempotency key, expiry,
bounded payload and evidence references. Coordinator pins sender identity.

Types: question, answer, finding, contract-change notice, task proposal, receipt.
States distinguish persisted, delivered, acknowledged, rejected and expired.
Acknowledgment means receipt, not agreement or task completion.

Use transactional outbox/inbox or equivalent durable atomic delivery. Delivery
is at least once; consumers deduplicate before side effects. Test crash/retry gaps.
Offline recipients keep queued messages, with visible expiry and escalation.
Agents cannot delegate permissions they lack. Messages and their attachments are
untrusted data, not system instructions. Bound message size, fan-out and hop depth.

## L3 — Cross-project task proposals

A proposal identifies the affected project/module, originating feature/task,
observed problem, evidence, desired outcome, contract impact, acceptance tests,
dependencies, estimated resource class and deduplication fingerprint.

Lifecycle: proposed -> triaged -> awaiting_approval/eligible -> queued -> running
-> verified/blocked/rejected/cancelled. Eligibility does not imply execution.

The coordinator checks registry membership, duplicate work, dependency cycles,
owner permissions, protected paths, budget and the approved feature scope.
Specialists may confirm or dispute evidence; they cannot approve their own scope
expansion. A new task or changed canonical plan follows the existing revision-
bound approval mechanism. Automatic dispatch within an already approved plan is
allowed; newly discovered work remains a proposal pending approval.

Potential cross-project contract changes require consumer impact review and
producer/consumer contract tests. Independent repositories do not share write
worktrees. Contradictory parallel changes produce an integration blocker.

## L4 — Daily research and session retrospective

Persist schedules with explicit timezone, run window, enable flag, approved model
profiles, source policy and hard call/time/document/byte limits. No schedule is
enabled by this specification. One run per schedule window uses a durable lease
and idempotency key. Restarts never launch an unbounded backlog of missed runs.
Ambiguous orphan work remains blocked until safely reconciled.

Research topics come from active product needs and approved questions, not an
unbounded news feed. Prefer primary sources; store source date, retrieval date,
evidence and applicability. Contradictions and inaccessible sources are visible.
Fetch through a restricted adapter: public HTTPS destinations only, validate DNS
and every redirect, block private/link-local/metadata addresses, cap time and
content size. Downloaded content is never executed or installed automatically.
Search queries exclude internal source code, incidents, identifiers and secrets.

Retrospectives consume authorized structured events and redacted session data:
task outcomes, retries, tool failures, review corrections, repeated investigations,
context omissions, latency and known/unknown costs. Record sample sizes and missing
data. Produce observations and improvement proposals, not self-awarded success.

Daily output: useful findings, contradictory evidence, measured failure patterns,
candidate improvements, costs and named blockers. Empty data is not a clean bill
of health. Chief UI exposes the report and links to evidence, without flooding
the shared chat with every intermediate event.

## L5 — Evaluated improvement, not unrestricted self-modification

Cycle: observation -> hypothesis -> isolated experiment -> comparison -> owner
decision -> controlled rollout/rollback. Keep baseline, candidate, dataset,
rubric, policy and model versions. Separate tuning examples from held-out checks;
freeze evaluation inputs before running a candidate.

Evaluate task correctness, missed escalations, regression rate, repeated failures,
latency and full cost including retries/review. Product-specific evaluation uses
approved metrics and examples. Unknown token/cost data stays unknown. No arbitrary
universal confidence threshold or guarantee of daily improvement.

Store linked experiment evidence in local durable state; Langfuse is an optional
authorized adapter, not an additional uncontrolled export channel. Proposed prompt,
skill or routing changes cannot bypass independent evaluation and approval.
Changes to policies, permissions, CI, tests, models or budgets are protected.

TRIZ-informed decision records describe the contradiction, ideal outcome,
available resources, alternatives (including no new agent/service), expected
benefit and falsifying experiment. Small routine fixes need a proportionate record.
Jev remains an optional separately authorized evaluator pilot; it is not required
for memory, message delivery, permissions, completion or approval.

## Safety, limits and failure behavior

All model calls share the existing global cap of five, with per-run and per-feature
limits reserved atomically. Missing profiles fail closed; no expensive fallback.
Background work yields to interactive tasks and cannot consume their reserved
budget. Exact priorities and limits must be explicit before schedule activation.

Authorization is deterministic and separate from model judgments. Source content,
messages and stored memory never change the instruction hierarchy. Agent-executed
code and tests require restricted environments; a worktree alone is not a sandbox.
No raw secret-bearing logs in knowledge or model prompts.

Storage failure cannot produce a successful delivery/completion claim. Source
failure marks evidence unavailable. Provider failure produces a bounded retry or
blocker. Stop prevents new work and exposes in-flight drain status; it does not
claim to reverse external effects. Audit metadata includes actor, action, policy,
scope and outcome, with content redacted according to the data policy.

## Acceptance evidence

1. A changed contract revision makes dependent knowledge stale; retrieval names
   the conflict or missing verification instead of claiming current knowledge.
2. An unauthorized project cannot retrieve restricted knowledge or forge sender
   identity, including via search results, messages and derived indexes.
3. A crash after persistence and before acknowledgment causes redelivery without
   duplicate task creation. Expired messages never silently execute.
4. An agent can propose a neighboring-module task, but scope expansion, dependency
   cycles, changed plan digests and exhausted budgets block dispatch.
5. A missed schedule window does not create an execution storm. Duplicate scheduler
   instances produce one logical run; global concurrency remains at most five.
6. Malicious retrieved instructions cannot alter policy or trigger tool execution;
   fetch tests reject private addresses and redirect-based boundary bypasses.
7. An improvement that weakens a guard or fails held-out evaluation cannot activate.
   Negative/mutation tests demonstrate that each critical gate actually rejects.
8. Chief UI shows freshness, delivery state, proposal approvals, experiment results
   and blockers across restart. No-data and not-checked remain distinct outcomes.
9. Verification separates fake-adapter contract tests, disposable database tests,
   browser acceptance and explicitly authorized live canaries.

## Delivery sequence and non-goals

Deliver L1, then L2/L3, then disabled-by-default L4, then L5. Each increment must
retain existing product sessions, approvals and Gantt compatibility. Use additive
migrations and explicit rollback procedures without deleting user history.

Not included: retraining foundation models, unrestricted internet crawling,
automatic production access, universal rewrites of every repository, replacing
all project instructions, or granting agents authority to accept their own work.

Activation requires owner review of this written specification and a subsequent
implementation plan; exact schedule, private data retention/export policy, allowed
providers and budgets must be configured before live operation. Their absence is
a setup blocker, not permission to guess.
