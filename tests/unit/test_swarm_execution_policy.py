"""Unit contracts for the pure swarm execution policy."""

from __future__ import annotations

import dataclasses

import pytest

from whilly.core.swarm_execution import ExecutionBlocked, ExecutionPolicy, SandboxResult


def _policy(**overrides: object) -> ExecutionPolicy:
    values: dict[str, object] = {
        "phase": "verify",
        "read_roots": ("/private/tmp/swarm-candidate",),
        "write_roots": ("/private/tmp/swarm-output",),
        "denied_roots": ("/private/tmp/swarm-secret",),
        "network": False,
        "timeout_seconds": 5,
        "max_output_bytes": 1024,
        "protected_write_roots": (),
    }
    values.update(overrides)
    return ExecutionPolicy(**values)


def test_policy_is_frozen_and_digest_is_deterministic() -> None:
    policy = _policy()

    assert dataclasses.is_dataclass(policy)
    assert policy.digest() == _policy().digest()
    with pytest.raises(dataclasses.FrozenInstanceError):
        policy.phase = "worker"  # type: ignore[misc]


@pytest.mark.parametrize("phase", ["", "unknown", "shell"])
def test_policy_rejects_unknown_phase(phase: str) -> None:
    with pytest.raises(ValueError, match="phase"):
        _policy(phase=phase)


@pytest.mark.parametrize("root", ["relative/root", "../escape", "tmp/child"])
def test_policy_rejects_relative_roots(root: str) -> None:
    with pytest.raises(ValueError, match="absolute"):
        _policy(read_roots=(root,))


@pytest.mark.parametrize("root", ["/", "/Users", "/Users/tester", "/private/var"])
def test_policy_rejects_broad_writable_roots(root: str) -> None:
    with pytest.raises(ValueError, match="write"):
        _policy(write_roots=(root,))


def test_policy_allows_isolated_home_nested_below_runtime_root() -> None:
    assert _policy(write_roots=("/private/var/folders/test/whilly/home",)).write_roots == (
        "/private/var/folders/test/whilly/home",
    )


@pytest.mark.parametrize("timeout", [0, -1])
def test_policy_rejects_nonpositive_timeout(timeout: int) -> None:
    with pytest.raises(ValueError, match="timeout"):
        _policy(timeout_seconds=timeout)


def test_policy_rejects_output_cap_above_one_mib() -> None:
    with pytest.raises(ValueError, match="output"):
        _policy(max_output_bytes=1024 * 1024 + 1)


@pytest.mark.parametrize(
    ("field", "value"),
    [("network", 1), ("timeout_seconds", True), ("max_output_bytes", True), ("max_output_bytes", float("nan"))],
)
def test_policy_rejects_non_strict_capability_types(field: str, value: object) -> None:
    with pytest.raises(ValueError, match=field):
        _policy(**{field: value})


@pytest.mark.parametrize("phase", ["verify", "git", "host_script"])
def test_offline_phases_reject_network_capability(phase: str) -> None:
    with pytest.raises(ValueError, match="network"):
        _policy(phase=phase, network=True)


def test_policy_digest_includes_denied_roots() -> None:
    assert _policy(denied_roots=("/private/tmp/one",)).digest() != _policy(denied_roots=("/private/tmp/two",)).digest()


def test_policy_digest_includes_protected_write_roots() -> None:
    assert _policy(protected_write_roots=("/private/tmp/swarm-output/.git",)).digest() != _policy().digest()


def test_execution_blocked_preserves_named_reason() -> None:
    error = ExecutionBlocked("execution_isolation_unavailable")
    assert str(error) == "execution_isolation_unavailable"


def test_protected_write_root_is_a_digest_bound_optional_capability() -> None:
    assert _policy(protected_write_roots=("/private/tmp/swarm-output/.git",)).protected_write_roots == (
        "/private/tmp/swarm-output/.git",
    )


def test_sandbox_result_is_a_pure_core_value() -> None:
    result = SandboxResult(None, "execution_isolation_unavailable", False, False, 0.0, "out", "err", "macos")
    assert result.reason == "execution_isolation_unavailable"
