"""Host-owned provisioning for guarded swarm execution.

The registry names a toolchain; the host supplies the corresponding trusted
roots, PATH and isolated home.  Registry data and child argv never select
those capabilities implicitly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType

from whilly.core.swarm_execution import SWARM_PHASES
from typing import Mapping, TYPE_CHECKING

if TYPE_CHECKING:
    from whilly.swarm.registry import Registry


@dataclass(frozen=True)
class ToolchainProvision:
    """Explicit capability material supplied by the trusted host."""

    toolchain_id: str
    read_roots: tuple[Path, ...]
    path: str
    home: Path
    temporary: Path
    provider: str | None = None
    provider_values: Mapping[str, str] = field(default_factory=dict)
    denied_roots: tuple[Path, ...] = ()
    auth_ready: bool = False
    auth_reason: str | None = None
    auth_mode: str | None = None
    auth_source_path: Path | None = None
    auth_service: str | None = None

    def __post_init__(self) -> None:
        if not self.toolchain_id.strip():
            raise ValueError("toolchain_id_required")
        if not self.read_roots or not self.path:
            raise ValueError("toolchain_roots_and_path_required")
        if self.provider not in {None, "claude", "codex"}:
            raise ValueError("provider_unsupported")
        if self.auth_mode not in {
            None, "api_key", "codex_subscription_file", "claude_subscription_dir", "claude_subscription_keychain"
        }:
            raise ValueError("auth_mode_unsupported")
        if self.auth_mode == "codex_subscription_file" and self.provider != "codex":
            raise ValueError("auth_mode_provider_mismatch")
        if self.auth_mode == "codex_subscription_file" and self.auth_source_path is None:
            raise ValueError("auth_source_path_required")
        if self.auth_mode == "claude_subscription_dir" and self.provider != "claude":
            raise ValueError("auth_mode_provider_mismatch")
        if self.auth_mode == "claude_subscription_dir" and self.auth_source_path is None:
            raise ValueError("auth_source_path_required")
        if self.auth_mode == "claude_subscription_keychain" and self.provider != "claude":
            raise ValueError("auth_mode_provider_mismatch")
        if self.auth_mode == "claude_subscription_keychain" and not self.auth_service:
            raise ValueError("auth_service_required")
        object.__setattr__(self, "provider_values", MappingProxyType(dict(self.provider_values)))


@dataclass(frozen=True)
class ExecutionProvisioning:
    """All host-selected inputs used by one guarded executor."""

    toolchains: Mapping[str, ToolchainProvision] = field(default_factory=dict)
    base_environment: Mapping[str, str] = field(default_factory=dict)
    secret_values: tuple[str, ...] = ()
    phase_toolchains: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "toolchains", MappingProxyType(dict(self.toolchains)))
        object.__setattr__(self, "base_environment", MappingProxyType(dict(self.base_environment)))
        object.__setattr__(self, "phase_toolchains", MappingProxyType(dict(self.phase_toolchains)))

    @classmethod
    def from_registry(cls, registry: "Registry") -> "ExecutionProvisioning":
        """Build capabilities only from explicit, non-secret registry metadata."""
        raw = registry.raw.get("execution")
        if not isinstance(raw, dict):
            raise ValueError("execution_configuration_required")
        raw_toolchains = raw.get("toolchains")
        if not isinstance(raw_toolchains, dict) or not raw_toolchains:
            raise ValueError("execution_toolchains_required")
        toolchains: dict[str, ToolchainProvision] = {}
        for toolchain_id, value in raw_toolchains.items():
            if not isinstance(toolchain_id, str) or not isinstance(value, dict):
                raise ValueError("execution_toolchain_invalid")
            roots = value.get("read_roots")
            path = value.get("path")
            if not isinstance(roots, list) or not all(isinstance(item, str) and item.startswith("/") for item in roots):
                raise ValueError(f"execution_read_roots_required:{toolchain_id}")
            if not isinstance(path, str) or not path:
                raise ValueError(f"execution_path_required:{toolchain_id}")
            auth = value.get("auth", {})
            if not isinstance(auth, dict):
                raise ValueError(f"execution_auth_invalid:{toolchain_id}")
            if set(auth) - {"mode", "reason", "source_path", "service"}:
                raise ValueError(f"execution_auth_mode_required:{toolchain_id}")
            provider = value.get("provider")
            if "ready" in auth:
                raise ValueError(f"execution_auth_mode_required:{toolchain_id}")
            auth_mode = auth.get("mode")
            if auth_mode is not None and not isinstance(auth_mode, str):
                raise ValueError(f"execution_auth_mode_invalid:{toolchain_id}")
            source_path = auth.get("source_path")
            if source_path is not None and not isinstance(source_path, str):
                raise ValueError(f"execution_auth_source_path_invalid:{toolchain_id}")
            auth_reason = auth.get("reason")
            if auth_reason is not None and not isinstance(auth_reason, str):
                raise ValueError(f"execution_auth_reason_invalid:{toolchain_id}")
            auth_service = auth.get("service")
            if auth_service is not None and not isinstance(auth_service, str):
                raise ValueError(f"execution_auth_service_invalid:{toolchain_id}")
            toolchains[toolchain_id] = ToolchainProvision(
                toolchain_id,
                tuple(Path(item) for item in roots),
                path,
                Path(value.get("home", registry.resolved_state_dir() / "host-home" / toolchain_id)),
                Path(value.get("temporary", registry.resolved_state_dir() / "host-tmp" / toolchain_id)),
                provider=provider,
                auth_ready=bool(auth_mode),
                auth_reason=auth_reason,
                auth_mode=auth_mode,
                auth_source_path=Path(source_path) if source_path else None,
                auth_service=auth_service,
                denied_roots=tuple(Path(item) for item in value.get("denied_roots", ())),
            )
        phases = raw.get("phases", {})
        if not isinstance(phases, dict) or not all(
            isinstance(key, str) and isinstance(value, str) for key, value in phases.items()
        ):
            raise ValueError("execution_phases_invalid")
        phases = dict(phases)
        legacy_reviewer = phases.pop("reviewer", None)
        if legacy_reviewer is not None:
            if "review" in phases and phases["review"] != legacy_reviewer:
                raise ValueError("execution_phase_conflict:review")
            phases["review"] = legacy_reviewer
        unknown_phases = sorted(set(phases) - set(SWARM_PHASES))
        if unknown_phases:
            raise ValueError(f"execution_phase_invalid:{unknown_phases[0]}")
        return cls(toolchains=toolchains, phase_toolchains=phases)
