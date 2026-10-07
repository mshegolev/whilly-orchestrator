## ADDED Requirements

### Requirement: Explicit complete product publication policies
The system SHALL require a policy for every registered project when the explicit product registry loader is requested, while legacy registry loading remains valid without product policies.

#### Scenario: Missing policy or remote in one project
- **WHEN** any declared project lacks required publication policy fields
- **THEN** the whole product snapshot is blocked rather than silently omitting that project

#### Scenario: Thirteen project inventory
- **WHEN** thirteen valid projects form a dependency graph
- **THEN** all thirteen policies and their dependencies remain in the immutable snapshot

### Requirement: Strict inert product policy declarations
The system SHALL validate canonical remote and unique GitLab identities, protected-target and branch-prefix declarations, bounded local argv and CI checks, ownership and relative allowed paths, artifact digest rules, stage/prod delivery observations, manual-decision boundaries and revert-MR compensation without executing commands or contacting remotes.

#### Scenario: Invalid graph or untrusted literal
- **WHEN** dependencies contain cycles or unknown projects, identities conflict, JSON keys repeat or a secret-looking literal is present
- **THEN** parsing fails with a bounded error without echoing registry paths or credentials

#### Scenario: Declarative protection
- **WHEN** a target is declared protected in a valid registry
- **THEN** the snapshot records that policy requirement without claiming it was observed remotely
- **AND** prod delivery remains conditional on release approval and compensation never declares history rewriting

### Requirement: Canonical product policy approval binding
The system SHALL expose separate canonical policy and complete registry bytes and digests, bind both to approval input, and reject a changed snapshot after verification before it can be used for a later external effect.

#### Scenario: Policy or profile changes after verification
- **WHEN** remote, target, checks, dependencies, artifact, delivery, compensation or full-registry profile content changes
- **THEN** the registry binding and approval digest change and snapshot revalidation blocks continuation

#### Scenario: Formatting-only change
- **WHEN** JSON key order or whitespace changes without changing resolved content
- **THEN** canonical digests remain equal
- **AND** returned dictionaries cannot mutate the frozen snapshot
