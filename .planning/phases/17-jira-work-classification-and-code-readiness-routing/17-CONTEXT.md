# Phase 17 Context: Jira Work Classification And Code Readiness Routing

## Goal

Make Jira-driven Whilly work safe to operate by classifying incoming issues, choosing the correct
operator flow, preserving Jira history, rereading GitLab links, and proving code/test readiness
before autonomous workers run.

## Background

Whilly now has `whilly jira intake <key>` for one-off Jira import and repo routing. The next gap is
the long-running operator workflow: Whilly should be able to watch Jira, stay current with
description/comment/link changes, propose the next step in Jira comments, and only run when the
operator explicitly approves.

The design baseline is `docs/superpowers/specs/2026-05-11-jira-intake-system-design.md`.

## Work Classification Model

Classify every incoming issue into one primary kind:

- `feature`: new product behavior or user-visible capability.
- `bug`: non-urgent defect that needs reproduction and a regression test.
- `task`: bounded one-off work such as config, docs, cleanup, or migration support.
- `devops`: CI/CD, Docker, infrastructure, credentials, environment, deployment, or observability
  work.

Treat `hotfix` as urgency, not as a primary kind. Hotfix can apply to `bug`, `devops`, `task`, or
rarely `feature`, and changes the required safety checks.

Classification output:

```json
{
  "kind": "bug",
  "urgency": "hotfix",
  "confidence": "medium",
  "signals": ["Jira issue type Bug", "priority Highest", "label production"],
  "missing_context": ["rollback target", "smoke test command"],
  "recommended_flow": "hotfix_bug"
}
```

## Routing Profiles

- Feature flow: require acceptance criteria, impacted repo selection, PRD/context artifact, plan
  preflight, unit/integration test plan, then approval to run.
- Bug flow: require reproduction, expected/actual behavior, affected version, logs or stack traces,
  linked code context, and a regression test target.
- Hotfix urgency overlay: require minimal scope, risk statement, rollback plan, targeted tests,
  smoke test, and explicit approval before mutation.
- Task flow: require checklist, target repo/files, verification command, and stop if the task
  expands into product behavior.
- DevOps flow: require environment target, blast radius, credentials/auth check, dry-run where
  possible, rollback or restore path, and CI/infra validation.

## Jira Watch State

Persist task memory in Postgres so Jira comments and edits remain part of future context:

- session state: issue key, plan id, task id, repo target, classification, urgency, current flow,
  readiness state, pending question, lease owner, lease expiry.
- Jira cursors: issue updated timestamp, last seen comment id, last seen changelog id, last Whilly
  comment id.
- hashes: summary, description, normalized link set, classification input, readiness input.
- event history: imported issue, description changed, links changed, comment received, question
  asked, approval command received, readiness recalculated, worker started, worker paused.

Jira comments are the operator protocol. Supported commands should include:

- `/whilly classify <feature|bug|task|devops>`
- `/whilly urgency <normal|hotfix>`
- `/whilly prd`
- `/whilly plan`
- `/whilly run`
- `/whilly continue`
- `/whilly replan`
- `/whilly cancel`

Commands must be idempotent by Jira comment id.

## Jira And GitLab Refresh

Each poll must reread Jira summary, description, comments, changelog, issue links, and remote links.
GitLab and GitHub links should be normalized into repository hints with provider, repo full name,
clone URL, ref, ref type, source issue key, and source link.

Reuse or extract the existing `whilly.qa_release.collector` logic instead of creating a second Jira
link parser. It already handles issue links, remote links, URLs, and GitHub/GitLab repo hints.

When description or links change:

- if no worker is running, update the proposal and readiness summary.
- if a worker is running and repo/ref/scope changed, pause before further writes and ask whether to
  continue, replan, or cancel.

## Code And Test Readiness

Before Whilly comments that work is ready or starts autonomous execution, run a read-only readiness
probe for the selected repo/ref.

The probe should:

- clone/fetch the selected repo into a managed read-only workspace or reuse an existing prepared
  workspace for the same ref.
- detect repository language and test conventions from files such as `pyproject.toml`,
  `pytest.ini`, `tox.ini`, `package.json`, `pom.xml`, `build.gradle`, `go.mod`, and `Cargo.toml`.
- detect unit tests using language conventions: Python `tests/unit`, `test_*.py`, `*_test.py`;
  JS/TS `*.test.*` and package test scripts; Go `*_test.go`; Java/Kotlin test source roots; Rust
  `#[test]`.
- prefer project-config verification commands, then infer conservative commands and mark them as
  inferred.
- map Jira-mentioned paths and GitLab blob links to nearby tests when possible.

Readiness verdicts:

- `ready_for_testing`: repo/ref resolved, unit tests or a unit-test command exist, and the command
  is known.
- `needs_test_plan`: repo/ref resolved, but tests or a reliable command are missing.
- `needs_repo_choice`: no repo or multiple plausible repos need operator selection.
- `needs_human_context`: scope or acceptance criteria are too ambiguous.
- `blocked`: auth, clone/fetch, or referenced GitLab object access failed.

Autonomous `run` must stop on `needs_test_plan`, `needs_repo_choice`, `needs_human_context`, or
`blocked` unless the operator explicitly overrides the gate.

## Suggested Plan Breakdown

1. Work classification model and routing profiles.
2. Jira watch session persistence and comment command protocol.
3. Jira context refresh and GitLab/GitHub link hint extraction.
4. Read-only code readiness probe and unit-test detection.
5. CLI/operator docs and end-to-end tests for the full classified flow.

## Verification Scope

- Unit tests for classifier signal precedence, confidence, and override behavior.
- Unit tests for flow selection by kind plus hotfix urgency overlay.
- Unit tests for Jira comment command parsing and idempotency.
- Unit tests for GitLab/GitHub URL normalization, including MR, branch, commit, blob, and pipeline
  URLs.
- Unit tests for readiness verdicts, including no-unit-tests and no-verification-command cases.
- Integration tests for importing/watching a Jira issue, refreshing changed links, persisting
  session history, and stopping before worker run when readiness gates fail.
