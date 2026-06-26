# gitlab-integration

## ADDED Requirements

### Requirement: Configurable GitLab host inference fallback

When the GitLab MR sink cannot parse a host from `remote.origin.url`, it SHALL
fall back to the `WHILLY_GITLAB_SSH_HOST` environment variable, defaulting to
the neutral host `gitlab.example.com` when unset or empty, rather than a
hardcoded internal host.

#### Scenario: Fallback honours the environment

- **WHEN** `remote.origin.url` cannot be parsed for a host **AND**
  `WHILLY_GITLAB_SSH_HOST` is set
- **THEN** the inferred host SHALL equal `WHILLY_GITLAB_SSH_HOST`

#### Scenario: Fallback default when unset

- **WHEN** `remote.origin.url` cannot be parsed for a host **AND**
  `WHILLY_GITLAB_SSH_HOST` is unset or empty
- **THEN** the inferred host SHALL be `gitlab.example.com`
