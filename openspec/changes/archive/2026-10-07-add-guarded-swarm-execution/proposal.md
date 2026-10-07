# Integrate guarded swarm execution

Route every model, verification, Git and BMAD host-script launch used by the
local and product swarm through one host-provisioned guarded executor. Preserve
approval, admission and Store compatibility while failing closed when
isolation, toolchain provisioning, provider authorization or publication
capability is unavailable.

This delta intentionally does not add a network publication backend or permit
guarded push. Publication remains a named local-result blocker.
