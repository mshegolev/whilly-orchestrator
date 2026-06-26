# scheduling

## ADDED Requirements

### Requirement: Configurable GitLab SSH host for derived clone URLs

The scheduler intake SHALL read the GitLab SSH host from the
`WHILLY_GITLAB_SSH_HOST` environment variable when deriving the SSH clone URL
for a `gitlab:<full_name>` repo target that does not pin an explicit
`repo_clone_url`. When the variable is unset or empty it SHALL default to the
neutral host `gitlab.example.com`, so the real internal host lives in a
gitignored environment rather than in source.

#### Scenario: Host taken from the environment

- **WHEN** `WHILLY_GITLAB_SSH_HOST` is set and a rule pins
  `repo_target = "gitlab:<full_name>"` without `repo_clone_url`
- **THEN** the derived clone URL SHALL be
  `git@<WHILLY_GITLAB_SSH_HOST>:<full_name>.git`

#### Scenario: Neutral default when unset

- **WHEN** `WHILLY_GITLAB_SSH_HOST` is unset or empty
- **THEN** the derived clone URL SHALL use the neutral default host
  `gitlab.example.com`
