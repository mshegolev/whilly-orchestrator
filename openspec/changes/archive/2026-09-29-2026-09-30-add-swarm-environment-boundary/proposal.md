# Explicit swarm process environment profiles

Add the pure `build_swarm_environment` contract for future swarm child-process
launch integration. The helper copies only explicit host values, identity
allowlist entries, and the selected provider credential; `verify`, `git`, and
`host_script` remain credential-free. Runtime integration and subscription-home
provisioning are deliberately deferred to Task 6.
