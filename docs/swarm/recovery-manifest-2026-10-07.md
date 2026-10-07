# Whilly Swarm recovery and activation manifest

Status: source review against the recovery branch base and read-only filesystem
snapshots; this manifest is the record of what is carried forward, what is
already present, and what must stay out of the initial swarm rollout.

## Recovery evidence and classification

The two recovered checkout snapshots had unusable Git metadata. Their files
can be compared with the current repository, but Git cannot establish whether a
file was committed, uncommitted, or belonged to a particular branch. The
classifications below therefore describe file content and current capability,
not commit ancestry. No old snapshot was modified.

| Candidate group | Classification | Evidence and disposition |
|---|---|---|
| Product feature cockpit and guarded product workflow | Already present in current baseline | `whilly/api/templates/product_swarm.html.j2`, `whilly/api/static/product-swarm.js`, `whilly/api/product_swarm.py`, `whilly/api/product_workflow.py`, and `whilly/swarm/product_workflow.py` provide the feature-oriented save, plan, approval, run, stop, review, and report flow. Keep this separate from legacy session/revision UI. The old UI snapshot did not contain the product cockpit. |
| Current legacy swarm UI improvements | Already present in current baseline | `whilly/api/templates/swarm.html.j2`, `whilly/api/static/swarm-gantt.js`, `whilly/api/static/whilly-theme.js`, `whilly/api/static/whilly-navigation.js`, and shared fragments retain legacy session controls while adding current responsive, theme/navigation, status, and report behavior. Do not replace these with the older fixed-layout snapshot. |
| JEV typed decision layer and worktree analyzer | Optional; excluded from initial use | Snapshot-only files: `whilly/core/decision_layer.py`, `scripts/analyze_worktrees.py`, `docs/JEV-Decision-Layer.md`, and `tests/unit/test_decision_layer.py`. The swarm-learning design says Jev is an optional, separately authorized evaluator pilot. It is not needed for guarded execution or the baseline learning workflow. No equivalent runtime module is claimed in the current tree. Revisit only with a separately scoped spec, threat/authority boundary, tests, and approval. |
| Standalone Clean Architecture quality-gate package | Superseded for this recovery | Snapshot-only `.clean-architecture.json`, `whilly/quality/__init__.py`, `whilly/quality/architecture.py`, `tests/unit/test_architecture_gate.py`, `docs/Clean-Architecture-Gates.md`, and the `whilly-architecture` entry point are not copied. Current swarm verification already accepts configured architecture commands and structured `architecture-json` evidence through `whilly/swarm/verification.py`, `whilly/swarm/verification_runner.py`, and `Coordinator._verify` in `whilly/swarm/runtime.py`. The remaining acceptance question was coverage across launch paths, recorded below; it did not justify a second gate implementation. |
| Semantic drift checker and CI workflow | Current baseline retained; snapshot copies superseded | Keep the current `scripts/semantic_drift_check.py`, `.github/workflows/semantic-drift.yml`, and checker/workflow/fixture tests. The snapshot checker had a stale capability inventory (32 entries versus the current 33 capability specs); its copy must not replace the current one. This repository CI check is distinct from the product's swarm-learning memory lifecycle. |
| Drift remediation, scaffold, and emitter | Deferred; excluded from initial use | Snapshot-only `scripts/drift_remediate.py`, `scripts/drift_scaffold.py`, `scripts/drift_emit.py`, `tests/test_drift_remediate.py`, `tests/test_drift_scaffold.py`, and `tests/test_drift_emit.py` are absent from the current tree. They write files and need a separate design and path-containment, triage, anonymization, and OpenSpec review before operational use. The existing semantic drift checker does not imply these write-capable tools are required. |
| Mixed runtime and test differences in the old snapshot | Rejected from this recovery; not adjudicated as a general product decision | Do not bulk-copy changes to `whilly/adapters/runner/swarm_sandbox.py`, `whilly/adapters/runner/swarm_environment.py`, `whilly/core/swarm_execution.py`, `whilly/api/admin_users_routes.py`, `whilly/api/webauthn_challenge_repo.py`, `whilly/adapters/runner/env.py`, `whilly/adapters/db/learning_memory.py`, `whilly/adapters/filesystem/knowledge_sources.py`, `whilly/adapters/transport/server.py`, `whilly/api/dashboard.py`, `whilly/forge/intake.py`, or `whilly/operator_views.py`, nor adjacent tests. Named snapshot-only tests include `tests/unit/test_admin_users_routes.py`, `tests/unit/test_webauthn_challenge_clock.py`, `tests/integration/test_session_persistence.py`, `tests/unit/test_agent_backend_wiring.py`, `tests/unit/test_forge_plan_proxy.py`, and `tests/unit/test_testcontainer_cleanup_policy.py`. The snapshot includes policy-relevant differences (including relaxed phase-specific network restrictions), session/auth behavior, and unrelated route/forge/learning changes. Each requires its own source review, tests, and behavior-spec delta if ever proposed. |
| Generated and environment-specific material | Rejected from source recovery | Exclude virtual environments, caches, bytecode, build output, logs, and generated graph/report data. Preserve protected analysis artifacts; this recovery does not authorize cleanup. |

The active `add-guarded-swarm-execution` OpenSpec change had one unchecked item:
controller review of the complete per-launcher inventory. This manifest records
that review. The code already routes the principal swarm candidate execution
paths through the guarded executor. One coordinator exception mapping was also
identified during this recovery: setup-time `ExecutionBlocked` must retain its
named reason in the durable task outcome. The implementation and updated
acceptance evidence are part of the recovery diff; do not use the earlier static
audit report as evidence that this mapping is still absent.

## Launcher inventory and guarantee boundary

The guarantee established here is limited to the swarm coordinator's
candidate-consuming execution lifecycle. `GuardedExecutor.run` and
`GuardedExecutor.run_sync` enforce a phase-matched `ExecutionPolicy`, an
enrolled working directory, sandbox execution, bounded output, and secret
redaction. `runtime.py` builds phase policies and `swarm_sandbox.py` launches
the sandbox process. The nested subprocess transport in `whilly/swarm/agent.py`
is invoked under that executor; it is not an independent model-launch path.

| Launch or operation | Source path | Boundary / outcome |
|---|---|---|
| Discussion and chief chat | `whilly/swarm/runtime.py` (`SwarmService.chat`), `whilly/swarm/product_workflow.py` (`discuss`), `whilly/swarm/agent.py` | Guarded read-only model call with discussion phase policy. Setup and executor blockers must remain named. |
| Planning and planner escalation | `whilly/swarm/product_workflow.py` (`plan`, retry/escalation flow), `whilly/swarm/runtime.py`, `whilly/swarm/agent.py` | Guarded read-only planner/escalation phases. Escalation follows the explicit exhausted-attempt policy; it is not a generic retry for every blocker. |
| Worker model | `whilly/swarm/runtime.py` (`_execute`, `_launch_worker_engine`), `whilly/swarm/agent.py`, `whilly/swarm/admission.py` | Guarded writable candidate-worktree phase, with admission reservation and configured limits. Named task outcomes include timeout, blocked/invalid result, and specific execution blockers. |
| Reviewer model | `whilly/swarm/runtime.py` (`_execute` review section), `whilly/swarm/agent.py` | Guarded read-only phase. Review failure, invalid result, and rejection remain distinct outcomes. |
| Verification commands | `whilly/swarm/runtime.py` (`Coordinator._verify`), `whilly/swarm/verification_runner.py`, `whilly/swarm/verification.py` | Test, lint, architecture, and task-local commands run through the guarded `verify` phase and produce structured evidence. Required missing/skipped/invalid evidence blocks acceptance. |
| Candidate Git worktree operations | `whilly/swarm/gitops.py` (`GuardedGit`, `create_worktree`, commit/status/diff helpers), called by `whilly/swarm/runtime.py` | Coordinator-supplied candidate operations use the guarded Git policy, rooted to the worktree and with network disabled. `gitops.git()` remains a direct read-only metadata helper used by base-ref resolution; `create_worktree()` also has a direct fallback when called without a policy. Those helpers are outside the candidate mutation boundary and must not be described as sandboxed. |
| BMAD host scripts | `whilly/swarm/bmad_host.py` (`_run`, workspace preparation/persistence) | Runs through `GuardedExecutor.run_sync` in the network-disabled `host_script` phase, with project/script validation and the spec workspace as the write root. Unsupported custom execution fails closed. |
| API product run and stop | `whilly/api/product_workflow.py`, `whilly/swarm/product_workflow.py`, `whilly/swarm/runtime.py` | API queues the workflow; it does not spawn a model itself. Run revalidates approval/bindings and enters the coordinator. Stop revokes approval, requests coordinator stop, cancels the local job, and reports whether the cross-instance lock drained. |
| CLI run, resume, and rerun | `whilly/cli/swarm.py`, `whilly/swarm/runtime.py` | These commands re-enter the same service/coordinator path. CLI `SIGINT`/`SIGTERM` requests graceful stop. They are not additional unguarded swarm launchers. |
| Issue scheduler/intake | `whilly/cli/scheduler.py`, `whilly/scheduler/worker.py`, `whilly/cli/plan.py` | Polls/imports ordinary tasks; it does not start a model. A later swarm run goes through the coordinator boundary. |
| Learning research, schedule, and export | `whilly/api/swarm_learning.py`, `whilly/swarm/learning/research.py`, `whilly/swarm/learning/schedules.py` | Research is fixture-only retrospective analysis. API reports `schedule_enabled: false` and `export_enabled: false`; these paths currently have no model/process launcher. |

### Explicit subprocesses outside the swarm candidate boundary

- Legacy agent modes in `whilly/agents/base.py`, `whilly/agents/claude.py`,
  `whilly/agents/opencode.py`, `whilly/cli/run.py`, and `whilly/cli/worker.py`
  can use direct subprocess APIs. They are separate legacy worker entry points;
  the swarm executor does not make them sandboxed.
- `whilly/swarm/gitops.py::git` invokes Git directly for metadata operations,
  including base commit resolution. A call to `create_worktree` with no policy
  uses that helper too. The coordinator's candidate mutation calls supply the
  guarded policy, but this is not a repository-wide ban on subprocesses.
- `whilly/swarm/registry.py::_git` performs best-effort Git metadata probing;
  probe failures become `None` and can lead to a named registry validation
  error.
- `whilly/swarm/execution.py::_read_claude_keychain_token` invokes the system
  keychain command only for subscription authentication provisioning. Failure
  maps to `provider_auth_scope_unavailable`; it is not candidate execution.
- `whilly/swarm/product_publication.py::run_git` is a direct helper, but the
  current product publish path requires the provisioned publication backend
  and bound change-set requests. The helper's existence does not prove that
  product publication uses it.

Accordingly, the accepted statement is: the coordinator's candidate model,
review, verification, BMAD host-script, and candidate Git mutation paths use
the guarded executor, subject to the explicit read-only metadata/auth helper
exceptions above. The statement “all Whilly subprocesses are guarded” is false.
The executor enforces phase/root/sandbox policy; this review does not claim a
universal executable allowlist or an OS sandbox for legacy agent modes.

## Gates before first live use

1. Complete the focused fake/offline acceptance path and the full recovery
   verification listed in the implementation plan. This proves contracts and
   fake-provider behavior only.
2. On the actual swarm host, verify the supported macOS sandbox backend is
   available and `GuardedExecutor.ready()` passes its outside-read and network
   denial probes. Missing isolation is a named unavailable result; do not fall
   back to an unsandboxed launch.
3. Verify the selected registry toolchains and provider authentication are
   ready without printing or storing credential values. Missing toolchain or
   auth is a stop condition.
4. For the disposable feature, bind the exact plan revision, registry digest,
   verification policy, and base commits; require explicit approval. Recheck
   that those bindings still match immediately before each model call.
5. Run one documentation-only task with one worker and the configured finite
   call/time/cost bounds. Before starting, inspect the actual `ProductWorkflow`
   wiring and prove its publication backend is absent or cannot become ready:
   `ProductWorkflow.execute()` calls `publish_feature()` automatically after
   successful tasks. Merely omitting an explicit publish request is not enough.
   If publication cannot be disabled and verified, stop before the live canary.
   Observe discussion, plan, approval, worker, review, and verification evidence;
   do not push the canary result.
6. Keep fake-adapter, disposable-repository, and real-provider receipts
   separate. A pass in one category does not stand in for another. Preserve
   unknown cost/usage as unknown rather than treating missing telemetry as
   zero.

## Stop and rollback behavior

- Stop immediately on auth, isolation, approval/binding, policy, or budget
  failure. Record the returned reason and stop; do not switch providers,
  increase limits, disable the sandbox, or retry a non-retryable blocker.
- Use the product Stop action/API or send `SIGINT`/`SIGTERM` to the CLI. The
  product stop path invalidates approval before requesting coordinator stop;
  spawned process groups are terminated on cancellation/timeout. `drained: true`
  is the evidence that the local/cross-instance feature lock is clear.
- If stop reports `drained: false`, treat shutdown as pending. Do not claim all
  remote work stopped, and do not kill an orphan by PID alone; the existing
  crash-cleanup path requires validated process identity.
- Keep the disposable worktree, candidate commits, logs, and redacted evidence
  for diagnosis. The stop path does not reverse local candidate commits, and
  it cannot undo a remote publication that has already happened. The product
  workflow attempts publication after a successful run when its backend is
  ready, so publication must be proven unavailable before starting this
  canary; if it cannot be, do not run the live canary.
- Resume only after the named blocker is corrected, bindings are rebuilt and
  re-approved, and the full offline gates pass again. Any promotion or
  publication remains a separate explicit workflow with its own approval and
  receipts.
