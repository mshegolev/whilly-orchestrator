## ADDED Requirements

### Requirement: Swarm command family

The CLI MUST expose 13 top-level `whilly swarm` commands, with
`registry validate` as the nested executable leaf, for 14 executable leaves in
total. It MUST preserve exit codes 0 (success), 1 (execution failure), 2
(usage or validation error), and 4 (coordinator or revision conflict).

#### Scenario: Validate a registry

- **WHEN** an operator runs `whilly swarm registry validate --registry FILE`
- **THEN** the CLI validates the registry without creating a session or running
  an agent

### Requirement: Explicit execution

The CLI MUST keep planning separate from execution. `chat`, `propose`, and
interactive planning MUST NOT queue or execute tasks; execution MUST require an
explicit `run` or `resume` command.

#### Scenario: A plan is proposed

- **WHEN** a valid plan is proposed
- **THEN** its revision is durable and no queue task is executed until `run`
