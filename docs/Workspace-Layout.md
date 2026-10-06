# Workspace layout for the IC swarm

## Decision

Do not physically move the repositories into one directory. The ecosystem
already has a logical workspace at `/opt/develop/ic`: it is a stable hub of
symlinks, while Git repositories remain at their existing paths.

This is the safe arrangement because:

- `incident-copilot` is a bare repository and work must happen in worktrees;
- local virtual environments, caches and hooks already use absolute paths;
- `log-triage` is a local repository without an origin;
- some repositories are shared by consumers outside Incident Copilot;
- the Whilly registry already stores canonical absolute paths and dependencies.

Moving directories would create a large, unrelated migration with no product
benefit. It would also make stale paths look valid until a worker or CI job
uses them.

## Target hub

The hub should eventually expose one symlink per registry project:

| Registry project | Canonical path | Hub entry |
|---|---|---|
| incident-copilot | `/opt/develop/incident-copilot` | `/opt/develop/ic/incident-copilot` |
| opencode-fork | `/opt/develop/opencode-fork` | `/opt/develop/ic/opencode-fork` |
| investigate-suite | `/opt/develop/aiqa/investigate-suite` | `/opt/develop/ic/investigate-suite` |
| support_api_bot | `/opt/develop/support_api_bot` | `/opt/develop/ic/support_api_bot` |
| primary-diagnostics | `/opt/develop/primary-diagnostics` | `/opt/develop/ic/primary-diagnostics` |
| ordering-mcp | `/opt/develop/aiqa/ordering-mcp` | planned hub entry |
| mcp-pi | `/opt/develop/aiqa/mcps/mcp-pi` | planned hub entry |
| mcp-sptd | `/opt/develop/aiqa/mcps/mcp-sptd` | planned hub entry |
| langfuse-migrator | `/opt/develop/langfuse-migrator` | `/opt/develop/ic/langfuse-migrator` |
| log-triage | `/opt/develop/log-triage` | `/opt/develop/ic/log-triage` |
| aiqa-core | `/opt/develop/aiqa/aiqa-core` | planned hub entry |
| aiqa-etl | `/opt/develop/aiqa/aiqa-etl` | planned hub entry |
| aiqa-dashboards | `/opt/develop/aiqa/aiqa-dashboards` | planned hub entry |

Creating the missing symlinks is a separate, reversible workspace operation.
It must first verify that every target exists and that no same-named hub entry
would be overwritten. Until that operation is approved and executed, Whilly
must use the registry's canonical paths, not guessed hub paths.

## Guardrail rollout

Clean Architecture policies are added per repository, not copied blindly from
Whilly. The first repository gets a measured policy and a complete test/lint/
architecture evidence path; the next repository is enrolled only after that
path is proven. A missing policy remains a deliberate readiness blocker, not a
passing default.
