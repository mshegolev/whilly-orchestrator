# Guarded swarm execution — design

Date: 2026-09-30
Status: implementation plan explicitly approved by owner on 2026-09-30; implementation in progress, not yet accepted.

## Intent and scope

Enable development through one coordinating conversation, with strong planning,
small implementation models, independent review, and locally inspectable results.
Agent-generated code is untrusted executable input, including tests and hooks.
Instructions to behave safely are not an execution boundary.

The owner approved this sequence: execution protection, project gates, bounded
pilot, remaining projects, then retirement of the old dispatcher. This document
specifies the first subsystem and its acceptance contract. Later stages retain
their own explicit acceptance; approving this design does not approve an unseen
feature plan or authorize production, push, merge or deployment.

Constraints: preserve existing worktrees and dirty changes; retain existing
approval/revision semantics; at most five agents globally; optional USD cap does
not remove call/time limits. No new model calls for development acceptance until
the existing restriction on real-model pilot calls is explicitly resolved.

## Verified starting points

- `swarm/runtime.py::_agent_env` copies ambient environment and removes mainly
  database variables. Coordinator credentials outside that list can reach agents.
- `runtime.py::_verify` launches approved commands directly through `run_bounded`.
  The commands can consume tests/source changed by a worker after plan approval.
- `gitops.py::commit_task_changes` uses normal Git hooks before independent review.
  Configured hooks can execute candidate code; Git metadata isolation alone does
  not isolate that execution.
- Both mailbox consumers in `mailbox.py` can perform blocking path-based reads.
  `proposal_mailbox.py` already demonstrates descriptor-based regular-file checks.
- Existing plan/registry digest binding and independent-review admission should
  be extended, not replaced. Missing mandatory project gates must remain visible.

These are source findings, not claims that exploit probes or provider calls ran.

## Decision and alternatives

Choose incremental fail-closed execution protection with one local backend first.
Adding only lint/tests leaves the execution boundary open. Converting every
project and introducing a remote execution platform together increases rollout
risk and is outside this change.

The design resolves the autonomy/safety conflict by separating authority: models
propose and edit; the coordinator grants narrow capabilities and records evidence.
This is the relevant TRIZ decision, not a requirement to generate a long TRIZ
report for every small edit.

## 1. Explicit execution capabilities

Introduce one execution-policy boundary used by worker, reviewer, verifier and
candidate-consuming Git operations. Keep policy/value types independent of OS,
database, provider SDK and subprocess imports. OS adapters implement execution;
the runtime composes them. Add an executable import/dependency contract for this
boundary without rewriting unrelated modules.

An execution request carries a phase, immutable policy digest, canonical allowed
paths, argv, environment profile, timeout, output cap and cancellation ownership.
No shell parsing and no model-supplied expansion of capabilities. Child processes
inherit the same OS restrictions. A tool-name allowlist alone is insufficient.

Initial adapter: local macOS `sandbox-exec`, with explicit read/write grants and
network denied for verification and hooks. Executable presence is not readiness:
the actual deny probes below must pass. Unsupported or ineffective isolation
returns `execution_isolation_unavailable`; there is no direct-host fallback.
Linux/container adapters are separate future work, not implicitly supported.

Verification may read the pinned candidate and explicitly provisioned toolchain;
it may write only its disposable output/cache/temp directories. It cannot read
coordinator state, credential files or other project roots, write source files,
access network/host sockets, or spawn a child outside the same policy. Required
generated files are prepared in a separate disposable copy and compared against
the candidate, never silently written into the accepted tree.

## 2. Workspaces, Git and hooks

Guarded attempts use an independent disposable Git repository materialized at the
approved base SHA, rather than granting worker access to shared linked-worktree
metadata. This workspace retains its local branch and is the local handoff
artifact; no branch is automatically imported into the registered source repo.
Source checkouts, their refs and existing worktrees remain unchanged.

Candidate-consuming Git commands run under the execution boundary too. Grant
metadata writes only inside the independent attempt repository and only during
the coordinator Git phase. Workers do not receive metadata write permission.
Repository configuration, signing helpers, clean/smudge filters and diff helpers
must not introduce an unreviewed executable outside that boundary.

Resolve the source repository's effective hooks before the attempt. A nonempty
hook configuration requires an explicit pinned hook policy: approved bytes,
dependencies, execution phase and expected result. Missing/unsupported policy
returns `hook_policy_required`; never silently skip an existing required hook.
Approved hooks run sandboxed and offline. Policy changes require renewed approval.

Stop/reap the worker process group before capturing candidate content. Record
the candidate SHA and checked tree; after verification confirm they are unchanged.
Any mutation invalidates evidence. Independent review refers to the exact tested
SHA, base SHA and policy digest, not a mutable branch label.

## 3. Environment and provider authentication

Replace ambient-copy filtering with phase-specific allowlists for every model
entry point, including discussion, planning, escalation, workers and reviewers.
Verifier/Git profiles contain no provider credentials. Runtime identity fields
are coordinator supplied and cannot be overwritten by arbitrary extra env keys.

Provide fresh task HOME/temp locations. Explicitly provision only the selected
provider's supported authentication mechanism; do not copy the user's entire
home, CLI configuration, MCP configuration or credential directory. A provider
that cannot authenticate with the scoped configuration is blocked with
`provider_auth_scope_unavailable`, not retried with the full ambient environment.
Credential provisioning is host-owned and secret values never enter evidence.

Do not propagate database/admin/signing/publication/cloud variables or SSH agent
sockets. Network capability, where needed by a model provider, is distinct from
the offline test/hook profile; no claim of offline execution for provider calls.
Record the unavoidable provider-auth exposure as a scoped capability, not a claim
that the selected provider's credential is hidden from its own CLI process.

## 4. Mailbox filesystem boundary

Use a shared bounded descriptor-based reader for legacy and collaboration
mailboxes. Open with nonblocking/no-follow semantics, inspect the descriptor with
`fstat`, require a regular file, and read at most the configured limit plus one
byte. Never use a separate pathname stat as the authority for a later read.

Anchor directory operations to trusted directory descriptors, including receipts
and deletion. Reject symlinked/replaced roots and child directories; do not follow
worker-controlled paths during error reporting. Preserve host-derived identity,
deduplication and acknowledgement semantics. A rejected file gets a named outcome
when a safe receipt can be written; directory-integrity failure blocks the attempt.
Limits must cover request bytes and files processed per tick without unbounded
directory enumeration. Malformed/special files must not block the event loop.

## 5. Mandatory gates and approval binding

Project verification is trusted host policy, not solely model-generated argv.
The policy identifies test, lint and architecture checks, toolchain requirements,
hook requirements and protected configuration paths. Task-specific checks may
add requirements, never replace the mandatory set. Empty project gates block
development before model admission; discussion and planning remain available.

Bind policy, resolved base SHA and project registry to plan approval. Changes to
CI, agent instructions, test configuration, gate definitions or dependencies
require explicit review and renewed approval before acceptance. The candidate
cannot weaken its own acceptance policy. Existing legitimate test updates are
allowed through this review, not prohibited permanently.

Run trusted baseline and candidate gates in isolation. Distinguish `passed`,
`failed`, `not_run`, `dependency_missing`, `policy_changed`, `timed_out` and
`isolation_unavailable`. A skipped required test, empty test discovery or missing
architecture rule is not a pass. Evidence includes policy/backend fingerprints,
argv, exit status, timing, base/head SHAs, and bounded redacted diagnostics.

## Acceptance: first subsystem, without real models

1. Fake worker changes a test to access a synthetic secret outside its roots:
   verification fails, while an ordinary offline test passes.
2. File writes outside the allowed roots, loopback/network access and child-process
   escape attempts are denied. Probe only disposable fixtures, never real secrets.
3. A configured hook attempts an outside write; it is denied. A required failing
   hook blocks acceptance. Missing hook policy blocks before running candidate code.
4. Synthetic environment canaries cannot reach model/verifier/Git subprocesses
   except an explicitly selected fake provider credential in its own profile.
5. FIFO, symlink, oversize, malformed JSON and directory-swap mailbox fixtures
   produce bounded rejection; cancellation and unrelated task progress still work.
6. Missing backend, empty mandatory gates, changed policy digest or stale approval
   prevents execution with a named blocker and no fallback subprocess.
7. A deliberate code defect and forbidden dependency each fail the relevant gate;
   restoring the fixture makes it pass. Mere documentation is not architecture evidence.
8. Complete the fake-model discussion/plan/approve/edit/test/review flow. Confirm
   reapproval after escalation and exact-SHA review binding. Negative tests prove
   a worker cannot mark its own task accepted through a message or output text.

Each negative probe has a positive control so a broken runner cannot masquerade
as successful protection. Do not report protection from a mocked argv assertion
alone; the installed local OS backend must be exercised.

## Rollout and later gates

First ship the above subsystem and corresponding OpenSpec deltas; propose/apply/
archive behavior changes together. Existing stored sessions and reports remain
readable. New execution requires the guarded policy; missing configuration blocks
it rather than silently changing old approvals or resuming old jobs.

Next enroll one real project using the existing private gate inventory, then roll
out the remaining projects individually. No claim that all projects implement
Clean Architecture merely because a shared document or checker exists.

After explicit real-model permission: disposable-repository pilot, maximum two
concurrent workers, eight provider calls total and 30 minutes wall time; per-call
timeouts still apply and USD cap may be unset. The budget includes planning,
implementation, review and escalation. On exhaustion, stop with evidence.
Feature-plan approval remains a separate required action before execution.

Only after pilot acceptance: back up cron, remove exactly the old dispatcher
entry, preserve board and unrelated maintenance, and import selected tasks as
deduplicated drafts. Remote publication remains disabled. These are downstream
cutover gates, not completed work or authority to migrate unspecified tasks.

## Review and delivery state

This document describes the proposed behavior, not implemented guarantees.
No runtime code, project CI, cron, production or provider configuration changes
belong to this documentation step. No commits or staging of existing user work.
After owner review of this written design, prepare the implementation plan and
its execution handoff; do not infer approval for artifacts not yet reviewed.
