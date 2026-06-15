## Purpose

The worktree-isolation capability defines how Whilly gives each task an isolated
filesystem context so concurrent agents do not corrupt one another's working
trees. It covers two distinct layers grounded in `whilly/workspaces.py` and
`whilly/worktree_runner.py`: the v4 per-task workspace prepared by
`RepoTargetWorkspaceResolver` (wired into the live `whilly run` path), and the
per-task git-worktree lifecycle managed by `WorktreeManager`. This capability
governs where a task's agent runs and how its commits are reconciled.

## Requirements

### Requirement: Repo-target-less tasks reuse the process cwd
The `RepoTargetWorkspaceResolver.prepare` method SHALL return a workspace rooted
at the resolver's process working directory, with `reused_current_cwd` set true,
for any task that has no `repo_target_id`.

#### Scenario: Task without a repo target runs in the process cwd
- **WHEN** `RepoTargetWorkspaceResolver.prepare` is called for a task whose
  `repo_target_id` is empty
- **THEN** the returned `ResolvedWorkspace` SHALL have its path equal to the
  resolver's current working directory
- **AND** SHALL have `reused_current_cwd` set to true

### Requirement: Repo-targeted tasks resolve to a deterministic checkout
The `RepoTargetWorkspaceResolver.prepare` method SHALL resolve a task that has a
`repo_target_id` into a git checkout under the base directory from
`WHILLY_WORKSPACE_BASE` (default `.whilly_workspaces/repos`), laid out as
`<repo>/<plan>/<task>` and checked out on branch `whilly/<plan>/<task>`.

#### Scenario: Repo-targeted task checked out on its task branch
- **WHEN** `RepoTargetWorkspaceResolver.prepare` is called for a task with a
  registered `repo_target_id`
- **THEN** the returned workspace path SHALL be `<base>/<repo>/<plan>/<task>`
  under the configured workspace base
- **AND** the checkout SHALL be on branch `whilly/<plan>/<task>`

#### Scenario: Prepared workspace event is recorded
- **WHEN** a repo-targeted workspace is prepared successfully and the repository
  exposes a task-event recorder
- **THEN** the resolver SHALL record a `workspace.prepared` event carrying the
  repo target id, repo full name, branch, and workspace path

### Requirement: Unregistered repo target is rejected
The `RepoTargetWorkspaceResolver.prepare` method SHALL raise an error when a
task's `repo_target_id` does not resolve to a registered repo target.

#### Scenario: Unknown repo target raises
- **WHEN** `RepoTargetWorkspaceResolver.prepare` is called for a task whose
  `repo_target_id` is not registered in the repository
- **THEN** the method SHALL raise a `RuntimeError` and SHALL NOT return a
  workspace

### Requirement: Per-task worktree creation
The `WorktreeManager.create` method SHALL create a git worktree at
`<base_dir>/<task_id>` (default base `.whilly_worktrees`) on a new branch
`whilly/<task_id>`, removing any stale worktree at that path first.

#### Scenario: Worktree created on the task branch
- **WHEN** `WorktreeManager.create` is called with a task id and no worktree
  exists at its path
- **THEN** a git worktree SHALL be created at `.whilly_worktrees/<task_id>` on
  branch `whilly/<task_id>`
- **AND** the returned `Worktree` SHALL have `created` set to true

#### Scenario: Worktree creation failure is surfaced
- **WHEN** `WorktreeManager.create` runs `git worktree add` and the command exits
  non-zero
- **THEN** the method SHALL raise a `RuntimeError` carrying the git error output

### Requirement: Worktree merge-back via cherry-pick
The `WorktreeManager.merge_back` method SHALL cherry-pick the commits in range
`HEAD..<branch>` from the task's worktree branch back into the current branch,
aborting the cherry-pick and reporting a conflict when it fails.

#### Scenario: No commits to merge is a successful no-op
- **WHEN** `WorktreeManager.merge_back` finds no commits in `HEAD..<branch>` for
  the task's worktree
- **THEN** the method SHALL return a successful result with zero commits merged

#### Scenario: Cherry-pick conflict is aborted and reported
- **WHEN** `WorktreeManager.merge_back` runs the cherry-pick and it exits
  non-zero
- **THEN** the method SHALL abort the cherry-pick
- **AND** SHALL return a result with `conflict` set true and `success` set false

### Requirement: Per-task worktree cleanup
The `WorktreeManager.cleanup` method SHALL remove the task's worktree directory
and delete its `whilly/<task_id>` branch, and SHALL no-op for a task it does not
track.

#### Scenario: Tracked worktree is removed with its branch
- **WHEN** `WorktreeManager.cleanup` is called for a tracked task whose worktree
  path exists
- **THEN** the worktree SHALL be removed and the branch `whilly/<task_id>` SHALL
  be deleted

### Requirement: WorktreeManager is not wired into the live run path
The `WorktreeManager` SHALL be activated only by direct construction from a
caller and SHALL NOT be instantiated by the live `whilly run` path, and its
activation SHALL NOT be gated on any environment flag.

#### Scenario: Live run path does not construct a WorktreeManager
- **WHEN** the live `whilly run` path prepares per-task isolation
- **THEN** it SHALL use `RepoTargetWorkspaceResolver` for the workspace
- **AND** SHALL NOT construct a `WorktreeManager` and SHALL NOT read any
  `WHILLY_WORKTREE` or `WHILLY_USE_WORKSPACE` flag to enable it
