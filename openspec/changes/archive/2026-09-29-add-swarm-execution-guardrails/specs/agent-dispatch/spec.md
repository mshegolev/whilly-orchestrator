## ADDED Requirements

### Requirement: Fail-closed local execution
The swarm execution adapter MUST run supported local verification commands inside a host-enforced macOS sandbox with explicit policy roots and no direct-host fallback.

#### Scenario: Unsupported isolation
- **WHEN** macOS `sandbox-exec` is unavailable or the host is not macOS
- **THEN** execution returns the named `execution_isolation_unavailable` blocker
- **AND** the requested argv is not executed directly

### Requirement: Bounded sandbox transport
The swarm execution adapter MUST bound stdout and stderr independently, terminate the complete process group on timeout or cancellation, and return a named result for output overflow.

#### Scenario: Child output exceeds the policy cap
- **WHEN** a sandboxed child exceeds the configured output limit
- **THEN** the adapter terminates the process group
- **AND** returns `output_limit_exceeded` without waiting indefinitely for descendants

### Requirement: Real isolation evidence
The local acceptance probe MUST verify allowed file access and denial of synthetic outside-file, descendant-write, and localhost-network attempts using successful unsandboxed positive controls.

#### Scenario: Offline verification probe
- **WHEN** the disposable fixture contains readable and writable outside sentinels and a reachable localhost listener
- **THEN** unsandboxed controls succeed
- **AND** the sandboxed policy allows the approved read while denying outside reads, writes, descendant writes, and network access

### Requirement: Explicit execution roots
The sandbox profile MUST derive every read, write, executable and protected-write grant from canonicalized roots explicitly present in the execution policy.

#### Scenario: Relative executable resolution
- **WHEN** a caller supplies a relative executable and an explicit child `PATH`
- **THEN** the adapter resolves it against that `PATH` and `cwd`
- **AND** rejects execution when `PATH` is absent or the resolved executable is outside policy read roots

### Requirement: Fail-closed probe evidence
The local probe MUST use only fresh disposable fixture targets and MUST report `execution_isolation_unavailable` when any control or denial probe is not a completed concrete result.

#### Scenario: Backend failure during a negative probe
- **WHEN** a denial attempt has no exit code, a named spawn failure, a timeout, or cancellation
- **THEN** the probe reports unavailable
- **AND** it does not count the attempt as isolation denial
