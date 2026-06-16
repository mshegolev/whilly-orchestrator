## Purpose

The gitlab-integration capability governs Whilly's two GitLab touch points: the
read-only `whilly gitlab smoke` CLI verb in `whilly/cli/gitlab.py` and the
mutating merge-request sink `open_mr_for_task` in `whilly/sinks/gitlab_mr.py`.
This capability covers how a GitLab API token is resolved, how the smoke verb
performs only authenticated read probes against a live instance, and how the MR
sink is the single path that pushes branches and opens merge requests — never
breaking the orchestration loop on failure.

## Requirements

### Requirement: GitLab token resolution and host derivation
The system SHALL resolve the GitLab token in the precedence order `GITLAB_TOKEN` → `GITLAB_API_TOKEN` → `WHILLY_GITLAB_API_TOKEN`, falling back to `glab config get token -h <host>` when no env var is set, and SHALL derive `<host>` from the repository URL rather than any hardcoded hostname.

#### Scenario: Environment token wins over CLI fallback
- **WHEN** `_resolve_gitlab_config_state` is called and `GITLAB_TOKEN` (or one of the two later env vars) is set
- **THEN** the system SHALL use that env-var token and SHALL NOT invoke `glab config get token`

#### Scenario: glab fallback when no env token
- **WHEN** none of `GITLAB_TOKEN`, `GITLAB_API_TOKEN`, or `WHILLY_GITLAB_API_TOKEN` is set and a host was derived from the repo URL
- **THEN** the system SHALL invoke `glab config get token -h <host>` and use its stdout as the token when the command succeeds with non-empty output

#### Scenario: Host derived from repository URL
- **WHEN** the MR sink resolves a token via `_infer_remote_host` / `_resolve_gitlab_token`
- **THEN** the host SHALL be extracted from `git config --get remote.origin.url` (or the smoke verb's `--repo-url`) and never read from a hardcoded deployment hostname in the resolution path

### Requirement: Credentials are redacted from all output
The system SHALL strip any `user:pass@` userinfo from URLs and MUST redact tokens and credentials so they never appear in smoke reports, error messages, check hints, or stdout.

#### Scenario: Userinfo stripped before any reporting
- **WHEN** a configured `GITLAB_URL` or `--repo-url` contains embedded `user:pass@` userinfo
- **THEN** the system SHALL pass the value through `_redact_url` so the report payload, summary line, and error hints contain only the redacted form

#### Scenario: HTTP error message carries only redacted URL
- **WHEN** `_gitlab_get` converts an `HTTPError` or `URLError` into a `RuntimeError`
- **THEN** the raised message SHALL embed the redacted URL only and SHALL NOT leak the bearer token

### Requirement: Smoke command is strictly read-only
The system SHALL restrict `whilly gitlab smoke` to authenticated Bearer GET probes — `GET /api/v4/user` to verify the token and `GET /api/v4/projects/<encoded path>` to confirm access — plus a local repo-hint comparison, and it MUST NOT mutate any GitLab state.

#### Scenario: Auth and project-access probes use GET only
- **WHEN** `_run_gitlab_smoke` executes its checks against a live instance
- **THEN** it SHALL issue only Bearer-authenticated GET requests to `/api/v4/user` and `/api/v4/projects/<encoded>`
- **AND** it SHALL NOT issue any POST, PUT, PATCH, or DELETE request

#### Scenario: Redacted JSON report written, no state changed
- **WHEN** the smoke checks complete (pass or fail)
- **THEN** the system SHALL write a redacted JSON report under the smoke report directory and return exit code 0 when all checks pass, 1 when a check failed, or 2 when configuration is missing
- **AND** no GitLab-side resource SHALL have been created, updated, or deleted

### Requirement: MR sink is the only mutating path and never breaks the loop
The system SHALL confine GitLab mutations to `open_mr_for_task` (git push to the worker-owned feature branch then `glab mr create`), MUST treat an up-to-date / nothing-to-push push as a structured `no_diff` outcome rather than a hard failure, and MUST return a `PRResult` with a `failure_mode` on any push or create error rather than raising into the orchestration loop.

#### Scenario: Push and create are the mutating operations
- **WHEN** `open_mr_for_task` runs for a done task with worktree commits
- **THEN** the system SHALL push the branch with `git push --force origin HEAD:<branch>` and open the MR with `glab mr create --target-branch <base> --source-branch <branch> --yes`

#### Scenario: Nothing to push is a no_diff, not a failure
- **WHEN** `git push` returns non-zero with output containing "up-to-date" or "nothing to push"
- **THEN** the system SHALL return `PRResult(ok=False, failure_mode="no_diff")` rather than treating it as a hard error

#### Scenario: Errors surface as structured failure_mode
- **WHEN** the push or `glab mr create` step fails, times out, or the worktree is missing
- **THEN** the system SHALL return a `PRResult` with `ok=False` and a specific `failure_mode` (for example `git_push_failed`, `git_push_timeout`, `mr_create_failed`, `mr_create_timeout`, or `worktree_missing`)
- **AND** the call SHALL NOT raise an exception into the caller
