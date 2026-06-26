# Proposal: Configurable GitLab SSH host (de-hardcode internal host)

## Why

The internal GitLab SSH host `gitlab.example.com` was hardcoded in two
shipping modules — `whilly/scheduler/intake.py` (derived clone URLs) and
`whilly/sinks/gitlab_mr.py` (remote-host inference fallback). Baking a
company-internal hostname into source means it ships to the public repository.
The real host should live in a gitignored environment, with a neutral default
in code.

## What Changes

- **MODIFIED** `scheduling` → clone-URL derivation: when a rule pins a
  `gitlab:<full_name>` repo target without an explicit `repo_clone_url`, the
  derived SSH clone URL SHALL use the host from `WHILLY_GITLAB_SSH_HOST`,
  defaulting to the neutral `gitlab.example.com` when unset.
- **MODIFIED** `gitlab-integration` → remote-host inference: when the MR sink
  cannot parse a host from `remote.origin.url`, it SHALL fall back to
  `WHILLY_GITLAB_SSH_HOST` (default `gitlab.example.com`) instead of a
  hardcoded internal host.

## Impact

- Operators set `WHILLY_GITLAB_SSH_HOST=<real host>` in their gitignored
  `.env`; the public source carries only the neutral default.
- Behavior change: the in-code default host changes from the former internal
  host to `gitlab.example.com`. Deployments relying on the old hardcoded
  default must set the env var.
