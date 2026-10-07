## ADDED Requirements

### Requirement: Independent candidate repositories

Candidate-consuming swarm Git operations SHALL materialize an ordinary or bare
source at the approved base commit into a repository with independent Git
objects, no imported publication remote, and no writes to the source checkout,
refs, or worktree metadata.

#### Scenario: Dirty source checkout

- **WHEN** the source checkout has ordinary dirty tracked and untracked files
- **THEN** materialization preserves those files in the source
- **AND** the candidate starts at the approved base SHA without importing the
  dirty files

### Requirement: Guarded Git and explicit hooks

Candidate-consuming Git and coordinator commits SHALL use the explicit offline
Git execution policy and environment. A required executable hook SHALL have
pinned bytes, source-relative dependency digests, `git` phase, and expected exit
zero in the approved hook policy before it can run.

#### Scenario: Hook approval or failure

- **WHEN** a required hook has no valid approval, fails, or writes outside the
  candidate policy roots
- **THEN** the operation blocks with a named policy or Git failure
- **AND** it does not silently skip the hook or publish the candidate

### Requirement: Candidate identity

The candidate identity SHALL expose its exact head SHA and deterministic tracked
tree digest through the guarded Git seam for later evidence binding.

#### Scenario: Exact candidate identity

- **WHEN** the coordinator captures a candidate identity
- **THEN** it receives the candidate HEAD SHA and tracked-tree digest from
  guarded Git reads
