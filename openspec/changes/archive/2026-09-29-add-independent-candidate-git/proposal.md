# Why

Swarm workers must edit an independent candidate repository so dirty user
checkouts, shared Git metadata, refs, and remotes cannot be changed by a
candidate attempt. Git hooks are executable inputs and therefore need explicit
byte, configuration, dependency, phase, and result approval before a guarded
coordinator commit.

# What Changes

- Materialize ordinary and bare sources at an approved base SHA into a local
  repository with independent Git objects and no publication remote.
- Route candidate-consuming Git through the Task3 guarded execution seam with
  the explicit Task2 environment and offline Git policy.
- Require deterministic hook policy resolution and run approved hooks inside
  the guarded Git phase; missing, malformed, unpinned, failing, or outside-write
  hooks block the operation.
- Retain local candidate state and do not add runtime integration in this task.
