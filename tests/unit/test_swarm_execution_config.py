from pathlib import Path
from types import SimpleNamespace

import pytest

from whilly.swarm.execution_config import ExecutionProvisioning


def test_claude_subscription_requires_claude_provider_and_directory(tmp_path: Path) -> None:
    config = tmp_path / "claude"
    config.mkdir()
    registry = SimpleNamespace(
        raw={
            "execution": {
                "toolchains": {
                    "claude": {
                        "read_roots": [str(tmp_path)],
                        "path": "/bin",
                        "provider": "claude",
                        "auth": {"mode": "claude_subscription_dir", "source_path": str(config)},
                    }
                },
                "phases": {"planner": "claude"},
            }
        },
        resolved_state_dir=lambda: tmp_path / "state",
    )

    provisioning = ExecutionProvisioning.from_registry(registry)
    assert provisioning.toolchains["claude"].auth_source_path == config


def test_registry_execution_rejects_unknown_auth_keys(tmp_path: Path) -> None:
    registry = SimpleNamespace(
        raw={
            "execution": {
                "toolchains": {
                    "fixture": {
                        "read_roots": [str(tmp_path)],
                        "path": "/bin",
                        "auth": {"ready": True, "surprise": "no"},
                    }
                },
                "phases": {},
            }
        },
        resolved_state_dir=lambda: tmp_path / "state",
    )
    with pytest.raises(ValueError, match="auth_mode"):
        ExecutionProvisioning.from_registry(registry)


def test_registry_execution_normalizes_legacy_reviewer_phase_name(tmp_path: Path) -> None:
    registry = SimpleNamespace(
        raw={
            "execution": {
                "toolchains": {
                    "claude": {
                        "read_roots": [str(tmp_path)],
                        "path": "/bin",
                        "provider": "claude",
                        "auth": {"mode": "api_key"},
                    }
                },
                "phases": {"reviewer": "claude"},
            }
        },
        resolved_state_dir=lambda: tmp_path / "state",
    )

    provisioning = ExecutionProvisioning.from_registry(registry)

    assert provisioning.phase_toolchains["review"] == "claude"
    assert "reviewer" not in provisioning.phase_toolchains


def test_registry_execution_rejects_unknown_phase_name(tmp_path: Path) -> None:
    registry = SimpleNamespace(
        raw={
            "execution": {
                "toolchains": {"fixture": {"read_roots": [str(tmp_path)], "path": "/bin", "auth": {}}},
                "phases": {"surprise": "fixture"},
            }
        },
        resolved_state_dir=lambda: tmp_path / "state",
    )

    with pytest.raises(ValueError, match="execution_phase_invalid:surprise"):
        ExecutionProvisioning.from_registry(registry)
