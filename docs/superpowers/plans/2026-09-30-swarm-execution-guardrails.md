# Guarded Swarm Execution Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make local swarm development fail closed on unsafe execution, missing verification, or stale approval.

**Architecture:** Pure execution policy and evidence types compose with filesystem, environment and macOS process adapters. A coordinator-owned execution service integrates them with existing approval, admission, verification and independent review. Candidate code never runs through an unguarded fallback.

**Tech Stack:** Existing Python 3.12, asyncio, pytest, Ruff, import-linter, OpenSpec; macOS sandbox-exec for the initial isolated backend. No new provider calls or package installation required for the first acceptance suite.

**Spec:** `docs/superpowers/specs/2026-09-30-swarm-execution-guardrails-design.md`

**Status:** Owner approved all seven tasks on 2026-09-30; execution in progress. Tasks 1–5 accepted; Task 6 in progress; Task 7 pending. Detailed evidence: `.superpowers/sdd/2026-09-30-swarm-execution-guardrails/progress.md`.

**Execution method:** User requested inexpensive execution. Use `gpt-5.6-luna`, medium, for scoped implementers and independent task reviewers. One implementer at a time; no automatic upgrade to expensive models. Controller integrates evidence and resolves interfaces. Maximum five agents globally; workers do not spawn children.

## Global Constraints

- Preserve existing worktrees and dirty changes; no staging of unrelated work, no push/MR/merge/deploy.
- Optional USD cap does not remove call/time limits.
- Empty project gates block development before model admission; discussion and planning remain available.
- Unsupported or ineffective isolation returns `execution_isolation_unavailable`; there is no direct-host fallback.
- Missing/unsupported hook policy returns `hook_policy_required`; never silently skip an existing required hook.
- Provider authentication that cannot be scoped returns `provider_auth_scope_unavailable`.
- Initial implementation supports local macOS only; other platforms fail closed for guarded execution.
- No real-model pilot until the earlier free-checks restriction is explicitly resolved.
- All examples/fixtures in this public repository use neutral names; operational project mapping stays private.
- Every runtime change carries its own OpenSpec delta. Do not archive unimplemented claims.
- Do not activate a partially integrated execution boundary on the live service.

## Review Focus

1. A mailbox directory changes across an awaited DB call: descriptor anchoring must prevent outside writes (Task 1).
2. Provider wrappers spawn grandchildren or depend on ambient HOME: isolation must hold or report readiness blocked (Tasks 2, 3, 6).
3. Paths contain spaces, quotes, symlinks or shared Git metadata: capability construction must not widen access (Tasks 3, 4).
4. A green verifier mutates tracked files or deletes tests: evidence cannot remain valid (Tasks 5, 6).
5. A restarted coordinator sees old approvals or exhausted budgets: it must not resume unauthorized work (Tasks 6, 7).

## Task 1: Descriptor-safe mailbox transport

**Files:** Create `whilly/adapters/filesystem/swarm_mailbox.py`, `tests/unit/test_swarm_mailbox_filesystem.py`; modify `whilly/swarm/mailbox.py`, `whilly/swarm/proposal_mailbox.py` and their three existing unit test files. Add a task-scoped OpenSpec proposal for mailbox hardening.

**Interfaces:** `MailboxDirectory(root: Path)` is a context manager owning root/outbox/receipt descriptors. Methods: `request_names(limit: int) -> tuple[str, ...]`, `read_request(name: str, max_bytes: int) -> bytes`, `write_receipt(name: str, payload: dict) -> None`, `remove_request(name: str) -> None`, `write_state(name: str, payload: dict) -> None`, `read_state(name: str, max_bytes: int) -> bytes`, `has_receipt(name: str) -> bool`. State names are only `inbox.json` and `status.json`; route `read_inbox` through `read_state`. Integrity errors raise `MailboxIntegrityError(ValueError)`; invalid individual requests raise `ValueError`.

- [x] Add failing tests: FIFO and symlink requests never block; malformed/oversize JSON cannot invoke the service; a valid message still receives a durable receipt.
- [x] Add a directory-swap test during an awaited fake service call; assert no file outside the opened mailbox directories is modified. Include safe cleanup through descriptors and a positive valid-directory control.
- [x] Run ` .venv/bin/python -m pytest -q tests/unit/test_swarm_mailbox_filesystem.py tests/unit/test_swarm_mailbox.py tests/unit/test_collaboration_mailbox.py tests/unit/test_proposal_mailbox.py`; record the intended red failures.
- [x] Implement no-follow, nonblocking descriptor reads plus fstat regular-file checks and bounded reads. Keep `MAX_BYTES=64000`, process at most 100 directory entries per tick, and avoid sorting/materializing the entire directory. Reject invalid names. Use descriptor-relative exclusive temporary files and atomic replacement for receipts/state.
- [x] Route all three mailbox consumers through this adapter; retain host identities, durable retries and proposal-only behavior. Expected DB failures must not delete unconfirmed requests. Directory-integrity failure stops that attempt, not unrelated tasks.
- [x] Route public `enqueue` and `read_inbox` through the same boundary; add `write_request(name: str, payload: dict) -> None` to the adapter. Keep public signatures and stable IDs unchanged. Use a separate bounded state-read cap of 4 MiB for inbox/status, not the per-request cap; oversized state is an explicit error, never silent truncation.
- [x] Re-run the four test files and Ruff on changed files; record green evidence, then request independent review. Keep this task's diff separate from existing untracked work.

## Task 2: Explicit process environment profiles

**Files:** Create `whilly/adapters/runner/swarm_environment.py`, `tests/unit/test_swarm_environment.py`. Runtime integration belongs to Task 6; do not partially replace call sites here.

**Interfaces:** `build_swarm_environment(*, phase: str, base: Mapping[str, str], home: Path, temporary: Path, provider: str | None, provider_values: Mapping[str, str], identity: Mapping[str, str], path: str) -> dict[str, str]`. `phase` is one of `discussion`, `planner`, `escalation`, `worker`, `review`, `verify`, `git`, `host_script`. Unknown phase/identity/provider keys raise `ValueError`. `verify`, `git` and `host_script` are credential-free offline phases.

- [x] Add failing table-driven tests covering every phase: DB/admin/cloud/publication/SSH canaries absent; provider credential appears only for the selected engine and never verify/git; ambient HOME/PYTHONPATH/LD_PRELOAD/DYLD_* cannot override host values.
- [x] Add an executable fake-child test showing the built environment, not a mocked function call. Use only synthetic values and assert diagnostic output never includes credential values.
- [x] Run `.venv/bin/python -m pytest -q tests/unit/test_swarm_environment.py` and record red evidence.
- [x] Implement an allowlist: safe locale keys `LANG`, `LC_ALL`, `LC_CTYPE`, `TZ`, plus explicit host `PATH`, `HOME`, `TMPDIR`. Identity keys are only `WHILLY_SWARM_SESSION`, `WHILLY_SWARM_TASK`, `WHILLY_SWARM_REVIEW`, `WHILLY_SWARM_MAILBOX`. Never copy ambient entries by prefix.
- [x] Provider-values allowlist: Claude `ANTHROPIC_API_KEY`; Codex `OPENAI_API_KEY`; values are supplied by the host, not fetched from ambient env here. Subscription auth files require a separately provisioned isolated home in Task 6. Do not infer that an API key and a subscription are interchangeable.
- [x] Re-run tests and Ruff; independent review. No claim of complete credential isolation until Task 6 removes every ambient call path.

## Task 3: Fail-closed execution policy and local sandbox

**Files:** Create `whilly/core/swarm_execution.py`, `whilly/adapters/runner/swarm_sandbox.py`, `tests/unit/test_swarm_execution_policy.py`, `tests/unit/test_swarm_sandbox.py`, `tests/local/test_swarm_sandbox_acceptance.py`; modify `.importlinter` only for the narrow new contract.

**Interfaces:** Frozen `ExecutionPolicy(phase: str, read_roots: tuple[str, ...], write_roots: tuple[str, ...], denied_roots: tuple[str, ...], network: bool, timeout_seconds: int, max_output_bytes: int)` with `digest() -> str`; `ExecutionBlocked(reason: str)`; adapter `sandbox_argv(argv: tuple[str, ...], policy: ExecutionPolicy) -> tuple[str, ...]`; `probe_sandbox(policy: ExecutionPolicy, fixture_root: Path) -> dict`. Pure types do not import filesystem/process/framework adapters.

The adapter uses host-owned `denied_roots` from policy when constructing grants, and rejects overlap in either direction between allowed and denied roots. These roots never come from worker input and participate in the policy digest. Add optional `protected_write_roots: tuple[str, ...] = ()` for readable, non-writable subtrees inside writable candidates (especially `.git`). Unlike full denied roots, these are explicit write exclusions and may be nested inside allowed roots; canonicalize them, include them in the digest, and deny write/rename/unlink of the protected subtree even when its parent is writable. Real OS tests must pair a successful sibling source edit with denied protected metadata modification/removal. This preserves full secret-root overlap rejection without making worker source editing incompatible with protected candidate Git metadata.

Extend `ProcessOutcome` in Task 6 with optional `reason: str | None = None` and `backend: str | None = None`; existing constructors remain compatible and `ok` is false for non-null failure reasons. `describe()` preserves named blockers.

Implement the actual bounded transport here, not only argv generation: async `run_sandboxed(argv, *, policy, cwd, environment, stdout_path, stderr_path, stdin_path=None, on_start=None) -> SandboxResult`, plus sync `run_sandboxed_sync` for `asyncio.to_thread` Git/BMAD callers. Pure `SandboxResult` records exit_code, reason, timed_out, cancelled, duration_seconds, stdout_path, stderr_path, backend. Stream bounded output through pipes, retain group timeout/cancellation behavior; the sync adapter drives the same coroutine in its own thread context. Task6 maps this result into backward-compatible ProcessOutcome. The adapter does not import the swarm runtime.

- [x] Add red validation tests for invalid phase, broad writable roots, overlapping secret roots, relative paths, nonpositive timeout and unsafe quoting. Trusted roots must be canonicalized by the adapter, not resolved by pure domain code.
- [x] Add real local positive/negative probes: ordinary file read works; outside synthetic secret read/write, loopback connection and grandchild outside-write are denied. Missing executable/unsupported OS must return the named blocker, never execute argv directly.
- [x] The network probe uses a disposable localhost listener with a successful unsandboxed control connection, then requires denial under the offline policy. Connection refused to a nonexistent server is not isolation evidence. Outside-file probes likewise require an existing readable/writable synthetic sentinel as their positive control; never use real credentials.
- [x] Run unit policy tests first; then explicitly run local acceptance tests. Unsupported platform is reported as unavailable, not accepted via pytest skip counts.
- [x] Implement deterministic policy digest and default-deny sandbox profile generation. Verification/git deny network and host sockets. Writable HOME/temp/cache live beneath disposable roots; read grants include only approved toolchain/runtime and candidate roots. No grant of the entire user home or project parent directory.
- [x] Bound stdout/stderr to 1 MiB each and terminate on overflow with `output_limit_exceeded`; this is an initial conservative limit, not a measured optimum. Preserve timeout and process-group cancellation. Interface with existing `ProcessOutcome` through the integration service in Task 6.
- [x] Verify path escaping with spaces, quotes and symlink aliases; test that descendant processes inherit restrictions. Run `.venv/bin/lint-imports` and scoped Ruff. Independent review must inspect real OS probe evidence, not only argv strings.

## Task 4: Independent candidate repositories and guarded Git

**Files:** Create `whilly/adapters/filesystem/swarm_workspace.py`, `tests/unit/test_swarm_workspace.py`, `tests/local/test_swarm_git_isolation.py`; modify `whilly/swarm/gitops.py` only at named integration helpers.

**Interfaces:** `materialize_candidate(source: Path, base_sha: str, destination: Path, branch: str, policy: ExecutionPolicy, *, environment: Mapping[str, str], hook_policy: Mapping | None = None) -> Path`; `resolve_hook_policy(source: Path, approved: Mapping[str, Mapping[str, object]]) -> dict`; `candidate_identity(root: Path, *, policy: ExecutionPolicy, environment: Mapping[str, str]) -> tuple[str, str]` returns head SHA and tracked-tree digest. Candidate-consuming operations use Task 3 policy and explicit Task2 environment, not direct host subprocess fallback. `hook_policy=None` permits only sources with no required hooks; it never means skip hooks.

The host approval map keys are hook names; each value contains `sha256`, `dependencies` (source-relative path to SHA256 mapping), `phase` (initially `git` only), and `expected_exit` (initially `0` only). Unsupported forms fail `hook_policy_required`. The resolved policy includes canonical hook entries, the effective hook/config fingerprint and a deterministic `digest`; Task5 binds that digest and Task6 rechecks it. Keep dependency validation bounded and do not copy unpinned executable hook dependencies. This makes the hook-byte/config/dependency requirement explicit instead of trying to encode it in an ambiguous string-only map.

Implementation ruling after Task4 review: bounded snapshots permit at most 32 hooks, 64 total dependencies, 4096-byte relative paths, 1 MiB per file and 4 MiB aggregate hook/dependency bytes. These are conservative initial limits, not measured optima. Copy verified bytes through no-follow descriptor-relative operations; source changes or destination symlinks must never substitute unapproved bytes or redirect host writes. Use the explicitly validated local Git toolchain for trusted inspection too.

Add `GuardedGit.run(root: Path, argv: tuple[str, ...], *, policy: ExecutionPolicy, environment: Mapping[str, str]) -> str` as the synchronous guarded Git seam for existing `asyncio.to_thread` callers. Materialization and coordinator commit consume it; Task 6 replaces runtime calls to `create_worktree` and `commit_task_changes`, not just their comments. An unguarded helper remains usable only for trusted read-only repository inspection.

- [x] Add red tests for bare and ordinary sources: base SHA preserved, dirty user checkout unchanged, independent Git directory, no shared alternates/hardlinks, no imported remotes allowing publication.
- [x] Add required-hook tests: configured hook without pinned policy blocks; approved success hook executes in sandbox; failing hook blocks; outside-write hook is denied. Do not silently treat an empty copied hooks directory as source policy.
- [x] Run `.venv/bin/python -m pytest -q tests/unit/test_swarm_workspace.py tests/unit/test_swarm_gitops.py`; capture intended failures.
- [x] Implement source-snapshot materialization without writing source refs. Pin hook bytes/config/dependencies before execution. Disable unapproved external Git helpers through an explicit policy error, not hidden weakening. Never grant workers write access to shared source metadata.
- [x] Implement coordinator-only stage/commit inside the guarded independent repository after worker reaping. Git environment comes from Task 2. Retain local candidate branch/logs on failure; no automatic destructive cleanup or remote publication.
- [x] Run local Git sandbox acceptance, unit tests and Ruff; independent review checks hooks and filters as executable inputs.

## Task 5: Mandatory project verification policy

**Files:** Create `whilly/swarm/verification.py`, `tests/unit/test_swarm_verification.py`; modify `whilly/swarm/registry.py`, `whilly/swarm/product_workflow.py`, `tests/unit/test_swarm_registry_plan.py`, `tests/unit/test_product_workflow.py`.

**Interfaces:** `VerificationPolicy(test: tuple[tuple[str, ...], ...], lint: tuple[tuple[str, ...], ...], architecture: tuple[tuple[str, ...], ...], protected_paths: tuple[str, ...], toolchain_id: str)` with `digest() -> str`; `require_project_verification(project: Project) -> VerificationPolicy`; `protected_changes(paths: Sequence[str], policy: VerificationPolicy) -> tuple[str, ...]`. Add optional `verification_policy` to Project/registry; legacy `verification` remains readable but cannot satisfy guarded readiness by itself.

Persist canonical verification and hook policy under the existing revision `registry_snapshot` JSON and feature specification's host-owned `execution_binding` (already digest-bound). Include resolved base SHA, policy digest and hook digest per project. Extend preparation/apply checks in `runtime.py`, `product_workflow.py` and `store.py` as necessary; reject old snapshots without the binding, rather than inventing defaults. Round-trip DB tests must prove persistence. This uses existing JSON columns and avoids depending on the unrelated unapplied migration040.

Define `GateEvidence(stage: str, category: str, argv: tuple[str, ...], outcome: str, exit_code: int | None, collected: int | None, passed: int | None, skipped: int | None, head_sha: str, policy_digest: str)` and `parse_gate_result(kind: str, process: ProcessOutcome, report: bytes, *, stage: str, category: str, argv: tuple[str, ...], head_sha: str, policy_digest: str) -> GateEvidence`. Outcomes include `passed`, `failed`, `not_run`, `dependency_missing`, `policy_changed`, `timed_out`, `isolation_unavailable`, `empty_discovery`, `skipped_required`. The host selects parser kind: pytest JUnit XML, Ruff JSON/exit status, or architecture JSON with nonzero rule count. Unsupported tools block until a trusted parser is implemented. Run baseline and candidate separately in Task 6, retain both records, and never accept a worker-supplied report as host proof.

- [x] Add failing tests that task-authored commands cannot fill absent project test/lint/architecture categories; validation happens before worker model admission. Discussion-only use remains available.
- [x] Add tests that registry/policy/base SHA changes invalidate approval; weakening test config, CI, instructions or dependencies requires renewed approval. A task cannot claim its own policy exemption.
- [x] Run `.venv/bin/python -m pytest -q tests/unit/test_swarm_verification.py tests/unit/test_swarm_registry_plan.py tests/unit/test_product_workflow.py`; record red failures.
- [x] Parse and freeze policy from trusted registry. Categories are nonempty argv arrays, never shell strings; toolchain ID is explicit. Default protected patterns include CI definitions, AGENTS/CLAUDE instructions, dependency/lock files and test/lint/architecture configuration; host may add patterns, not remove required defaults.
- [x] Preserve the existing approval digest machinery by including canonical policy in serialized registry and prepared feature binding. Execution rechecks the binding immediately before admission and before acceptance.
- [x] Tests must distinguish zero collected/skipped required checks from passed. Introduce structured verification outcomes and do not infer test collection success solely from exit code zero. Define per-tool parsers for the first enrolled project before declaring its gate accepted.
- [x] Re-run tests and Ruff; independent review before runtime integration.

## Task 6: Integrate one guarded execution service

**Files:** Create `whilly/swarm/execution.py`, `tests/unit/test_swarm_execution_integration.py`; modify `whilly/swarm/runtime.py`, `whilly/swarm/agent.py`, `whilly/swarm/admission.py`, `whilly/swarm/engines.py`, `whilly/swarm/bmad_host.py` and scoped existing tests.

**Interfaces:** `GuardedExecutor.run(argv, *, phase, cwd, policy, environment, log_dir, stdin_path=None, on_start=None) -> ProcessOutcome` async; `GuardedExecutor.ready() -> dict`. Compose Tasks 2–5 and the existing process-group/admission mechanisms. No alternate direct run_bounded path may consume candidate code.

Integration rulings: a strict optional `registry.execution` configuration carries host toolchain metadata and supported auth-source metadata, never secret contents; it participates in the registry approval hash. Non-injected production services must load that configuration, not remain permanently unconfigurable. Provider model toolchains are separate from project verification toolchains so discussion/planning do not require enrolled project gates. Read/write/denied roots are explicit, never inferred from PATH or child argv; HOME/temp are fresh per call. Readiness requires the actual Task3 deny probes, not executable presence. Use the existing per-profile timeouts and the 30-second BMAD bound. Supporting `execution_config.py` and `verification_runner.py` modules with focused tests may separate provisioning and gate orchestration from the single process service. Guarded publication remains explicitly unavailable before the legacy publication adapter can run candidate-consuming Git; no network-Git capability is introduced.

- [x] Add red integration tests with fake provider executables for discussion/planner/escalation/worker/review plus verification and Git phases. Every child must receive the right environment and isolation policy.
- [x] Add tests for timeout, output overflow, stop, grandchild cleanup and restart with stale approval. Reserve/release model admission exactly once on success/failure/cancellation.
- [x] Add a fake worker modifying tests after approval; assert outside writes fail and policy/protected-file changes invalidate acceptance. Verify candidate SHA/tree unchanged after tests and before independent review.
- [x] Provision task-specific HOME/temp and host-selected provider authentication. Do not read or copy real secrets during tests. If subscription auth cannot be scoped on the installed CLI, readiness stays blocked; do not silently switch to API billing or ambient HOME.
- [x] Route all runtime model entry points through GuardedExecutor and remove ambient-copy `_agent_env` behavior. Both Claude and Codex use coordinator-only guarded commits. Legacy direct CLI entry points must reject guarded execution without policy.
- [x] Cover BMAD `_run` as an offline host-script phase using the same guarded process backend (30-second bound retained); scripts selected from a project are executable input too. Reject unapproved `WHILLY_SWARM_AGENT` executable overrides after approval. Test helpers that currently configure FAKE_* via ambient env must use explicit fixture-local executable configuration instead of adding fake keys to production allowlists.
- [x] Run `.venv/bin/python -m pytest -q tests/unit/test_swarm_execution_integration.py tests/unit/test_swarm_agent_process.py tests/unit/test_swarm_engines.py tests/unit/test_swarm_admission.py tests/unit/test_product_workflow.py`; then run actual OS acceptance probes. Review scope includes every launcher call site.

## Task 7: Offline full-cycle acceptance and contract landing

**Files:** Create `tests/integration/test_guarded_swarm_runtime.py`, `docs/Guarded-Swarm-Execution.md`; extend existing product workflow/runtime test fixtures. Own `openspec/changes/add-guarded-swarm-execution/` and related capability deltas only.

- [x] Add end-to-end fake-model flow: discussion -> proposed plan -> explicit approval -> two scoped edits -> mandatory checks -> independent review -> local result packet. No provider/remote calls.
- [x] Add forced two-attempt failure -> escalation -> changed plan -> reapproval; send/receive/ack messages without granting task execution. Verify budget exhaustion and restart do not bypass approval.
- [x] Add a golden local result packet containing candidate path, base/head SHA, policy/backend fingerprints, verification outcomes and reviewer identity. Reject worker-supplied forged receipts.
- [x] Run the new integration tests against disposable DB fixtures if capacity is available. If not, name the blocked DB checks; do not replace them with mocked success or restart the live database.
- [x] Run scoped regression, local OS probes, lint/import gates and OpenSpec strict validation. Archive each completed delta only after implementation matches it. Record changed files, test counts, unsupported paths and rollback instructions.
- [x] Independent whole-change review on the requested inexpensive model; no expensive-model escalation without owner approval. Review used Codex `gpt-5.6-luna` in read-only mode. The first pass found network/publication, cancellation/thread-lifetime and legacy-apply risks; the follow-up pass confirmed all three are closed in the reviewed runtime paths.

## Downstream cutover work (separate acceptance, not claimed complete)

- [ ] Enroll the first real project from the private gate inventory: resolve base SHA, provision offline toolchain, add missing executable architecture checks, run baseline/candidate and deliberate failure probes. Only then populate its trusted policy. Current attempt resolved `aiqa-dashboards` through local commits `0bd04e1`, `e679e96`, `f002f88` and `8f57a24`; plain clean-clone verification passes 1523 tests, but the guarded macOS candidate still fails during collection with `BaselineKeyError` for `allure_regression_18`. Trusted policy was removed again until this sandbox-specific import/collection difference is explained and fixed.
- [ ] Enroll the other twelve projects individually. Two MCP projects need actual tests; lint-only entries must stay blocked. No blanket claim of Clean Architecture compliance.
- [ ] With explicit authorization, run the subscription-model pilot on disposable repos: two concurrent workers maximum, eight provider calls total, 30 minutes wall time, per-call timeouts, USD cap optional, no push. Approval of the pilot's actual feature plan remains required.
- [ ] After pilot acceptance, back up crontab and remove only the exact old dispatcher entry. Preserve the board/maintenance jobs. Import only selected open tasks as deduplicated drafts with source IDs.
- [ ] Confirm local result handoff. Remote publication is a separate authorized project/branch/CI-safety configuration; automatic merge/deploy remain excluded.

## Execution and evidence ledger

Tasks 1–5 are accepted; Tasks 6–7 offline implementation, disposable-DB acceptance, and the requested inexpensive whole-change review are complete. Source HEAD at planning: `271053d31500f31d2d497d397235b5accc5d23ac`; this worktree also contains substantial earlier uncommitted work. Capture task-specific before/after artifacts rather than treating the entire worktree diff as this change. Live rollout remains downstream-gated: first-project enrollment, real-provider pilot authorization, old-dispatcher cutover, and any remote publication configuration are not claimed complete.

Each task requires red/green evidence, independent spec/quality review and a bounded fix loop. Keep artifacts until user handoff; do not delete another plan's files. Commits are deferred until changes can be staged individually and repository hook behavior has been reviewed; no automatic `git add -A` or hook bypass.

Plan self-review: design sections 1–5 map to Tasks 1–6; all eight design acceptance points map to Tasks 1, 3–7. Downstream rollout remains explicit and gated. No task may mark the overall six-item cutover finished merely because its unit tests pass.
