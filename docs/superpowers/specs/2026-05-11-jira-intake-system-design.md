# Jira Intake System Design

## Purpose

`whilly jira intake <key>` is the operator-facing path for one Jira issue when
the Jira ticket does not uniquely identify the repository where work must be
done. The command turns the issue into a Whilly plan, binds that plan to a
repository target, and then lets the operator choose whether to refine context,
run preflight checks, or execute the task.

## Flow

1. Validate Jira configuration using the same layered config and interactive
   prompts as `whilly jira import`.
2. Fetch the Jira issue through `whilly.sources.jira.fetch_single_jira_issue`.
3. Write the stable `plan_id` convention: `jira-<lowercase-key>`.
4. Resolve a repository target:
   - `same`: use the current checkout's `origin` remote when available.
   - `new`: ask for the clone URL of the repo created for this work.
   - `other`: ask for the clone URL of an existing repo.
   - `skip`: keep the plan unrouted.
5. Patch the generated plan JSON with top-level `repo_targets` and task-level
   `repo_target_id`.
6. Refresh Jira/GitLab context before proposing execution:
   - reread the Jira description, comments, changelog, issue links, and remote
     links.
   - extract GitLab/GitHub repository hints from the issue body, linked issues,
     and Jira remote links.
   - run a read-only code readiness probe for the selected repository target
     when credentials and a concrete repo/ref are available.
   - report whether the task is ready for testing, missing unit-test coverage,
     blocked on repo/auth, or needs operator clarification.
7. Resolve the next action:
   - `prd`: write a context markdown file and store it in `origin.prd_file`.
   - `plan`: run `whilly plan apply --strict`, then `whilly plan triz --strict`.
   - `run`: run `whilly plan apply --strict`, then `whilly run --plan ...`.
   - `save`: stop after writing the plan JSON.

## Data Contract

Repository routing uses the same v4 plan fields as project-config generated
plans:

```json
{
  "repo_targets": [
    {
      "id": "gitlab:group/project",
      "provider": "gitlab",
      "repo_full_name": "group/project",
      "clone_url": "git@gitlab.example:group/project.git",
      "default_branch": "main"
    }
  ],
  "tasks": [
    {
      "repo_target_id": "gitlab:group/project"
    }
  ]
}
```

The context/PRD file is provenance, not a code-edit target, so it belongs in
`origin.prd_file` instead of `key_files`.

## Work Classification

Before Whilly chooses `prd`, `plan`, or `run`, it should classify the incoming
Jira issue into one primary work kind:

- `feature`: new product behavior or user-visible capability.
- `bug`: non-urgent defect that needs reproduction and regression coverage.
- `task`: bounded one-off work such as config, docs, cleanup, or migration
  support.
- `devops`: CI/CD, Docker, infrastructure, credentials, environments,
  deployment, or observability work.

`hotfix` is urgency, not a fifth kind. A hotfix can be a bug, DevOps change,
task, or rare feature change, and it adds stricter safety checks: minimal scope,
risk statement, rollback plan, targeted tests, smoke test, and explicit
approval before mutation.

Classifier output should include `kind`, `urgency`, `confidence`, `signals`,
`missing_context`, and `recommended_flow`. The operator can override
classification from Jira comments with commands such as
`/whilly classify bug` or `/whilly urgency hotfix`.

Routing rules:

- feature -> PRD/acceptance criteria, plan preflight, test plan, approval.
- bug -> reproduction, expected/actual result, affected version, regression
  test, approval.
- task -> checklist, target files/repo, verification command, approval.
- devops -> environment target, blast radius, dry-run when possible, rollback,
  infra/CI validation, approval.
- hotfix overlay -> shorter path, but stricter risk, rollback, smoke, and
  explicit approval gates.

## Jira And GitLab Refresh

The watch/intake refresh step must treat Jira as a changing source of truth, not
as a one-time import. Each poll should reread:

- the current summary, description, status, priority, labels, assignee, and
  `updated` timestamp.
- new and edited comments since the last seen comment id.
- changelog entries that affect status, assignee, description, labels, links,
  or priority.
- issue links and remote links, including GitLab merge requests, branches,
  commits, blobs, compare pages, pipelines, and repository URLs.

The existing `whilly.qa_release.collector` logic already extracts URLs, linked
issues, remote links, and GitHub/GitLab repository hints. This should be
extracted or reused as the shared Jira context collector instead of creating a
second parser.

Refresh stores hashes for `summary`, `description`, and the normalized link set.
If any hash changes, Whilly writes a history event and recalculates the proposed
next step before it asks for approval or continues work.

## Code And Test Readiness

After repo resolution, Whilly should run a read-only readiness probe before it
comments "ready to implement" or starts autonomous work. The probe should never
mutate the target repository.

The probe should:

- clone or fetch the selected GitLab/GitHub repo into a managed read-only
  workspace, or reuse an existing prepared workspace when the requested ref
  already exists locally.
- preserve the source ref from links when present: branch, tag, commit, merge
  request, blob path, or pipeline URL.
- identify build/test conventions from repo files such as `pyproject.toml`,
  `pytest.ini`, `tox.ini`, `package.json`, `pnpm-lock.yaml`, `pom.xml`,
  `build.gradle`, `go.mod`, `Cargo.toml`, and existing project config.
- detect unit tests by language conventions, including Python `tests/unit/`,
  `test_*.py`, `*_test.py`, JS/TS test scripts and `*.test.*` files, Go
  `*_test.go`, Java/Kotlin test source roots, and Rust `#[test]` modules.
- map Jira-mentioned paths, linked GitLab blobs, and likely changed modules to
  nearby tests when possible.
- derive verification commands from project config first, then from repository
  conventions, and mark inferred commands as inferred.

Readiness verdicts:

- `ready_for_testing`: repo/ref resolved, relevant unit tests or a unit-test
  command exist, and Whilly knows the command to run.
- `needs_test_plan`: repo/ref resolved, but no unit tests or reliable unit-test
  command were found.
- `needs_repo_choice`: Jira contains zero or multiple plausible repository
  targets and the operator has not selected one.
- `needs_human_context`: repo is known, but acceptance criteria or linked code
  context are too ambiguous to choose a safe plan.
- `blocked`: clone/fetch/auth failed, or the referenced GitLab object is not
  accessible.

Missing unit tests should not silently pass. For `prd` and `plan`, Whilly should
include "add or identify unit tests" as a proposed task/gap. For `run`, Whilly
should stop and ask for approval unless the operator explicitly allows running
without detected unit tests.

## Persistent State

The watch-mode Postgres state should keep a task-level memory of Jira and code
context so Whilly remains grounded in the latest comments and links:

- `jira_work_sessions`: `issue_key`, `plan_id`, `task_id`, `repo_target_id`,
  `state`, `issue_updated_at`, `summary_hash`, `description_hash`,
  `link_set_hash`, `readiness_state`, `readiness_summary`,
  `last_seen_comment_id`, `last_seen_changelog_id`, `last_whilly_comment_id`,
  `pending_question`, `lease_owner`, and `lease_until`.
- `jira_thread_events`: comments, description changes, link changes, approval
  commands, questions, answers, and readiness recalculations.
- `jira_code_readiness_snapshots`: `session_id`, `repo_target_id`,
  `provider`, `clone_url`, `ref`, `commit_sha`, `source_link_hash`,
  `tests_detected`, `verification_commands`, `readiness_state`, `gaps`, and
  `created_at`.

When Jira description or GitLab links change while work is not running, Whilly
updates the proposal and comments with the new readiness summary. When they
change during an active run, Whilly should pause before making further writes
and ask for an explicit `/whilly continue`, `/whilly replan`, or
`/whilly cancel`.

## Safety Rules

- The command never creates a remote repository; `new` only records the clone
  URL for a repo the operator has already created.
- `run` is gate-first: a strict apply failure stops execution before any worker
  claims work.
- `run` must also be readiness-first in watch mode: if the selected repo cannot
  be resolved or unit-test readiness is unknown, Whilly asks instead of starting
  workers.
- Unknown non-GitHub hosts default to `gitlab` because the repo target still
  carries an explicit `clone_url`; operators can override with
  `--repo-provider`.
- `jira import` remains the low-level non-interactive fetch/import/run command;
  `jira intake` is the safer daily driver when the repo is not implicit.

## Closed Gaps

- `run` no longer bypasses strict plan validation.
- `plan` now performs both Decision Gate and TRIZ/challenge preflight.
- PRD/context output is linked through plan origin instead of polluting
  `key_files`.
- GitHub and GitLab clone/browser URLs are converted into canonical
  `repo_targets` automatically.

## Current Boundaries

- No remote repo creation.
- The implemented `jira intake` command still does not clone repositories. The
  read-only GitLab/code readiness probe is a required watch-mode extension and
  should be added before enabling autonomous Jira polling.
- No embedded LLM chat loop inside `jira intake`; the PRD action prepares the
  context artifact for the operator/agent conversation.
