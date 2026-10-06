"""Single guarded process service for every candidate-consuming launch."""

from __future__ import annotations

import os
import json
import shutil
import stat
import subprocess
import sys
import tempfile
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from whilly.adapters.runner.swarm_environment import build_swarm_environment
from whilly.adapters.runner.swarm_sandbox import SANDBOX_EXEC, probe_sandbox, run_sandboxed, run_sandboxed_sync
from whilly.core.swarm_execution import ExecutionBlocked, ExecutionPolicy
from whilly.swarm.execution_config import ExecutionProvisioning

if TYPE_CHECKING:
    from whilly.swarm.agent import ProcessOutcome

__all__ = ["GuardedExecutor"]


class GuardedExecutor:
    """Host-owned transport; no candidate code may use a direct subprocess path."""

    def __init__(self, provisioning: ExecutionProvisioning | None = None, *, runner=None, sync_runner=None) -> None:
        self.provisioning = provisioning or ExecutionProvisioning()
        self._runner = runner or run_sandboxed
        self._sync_runner = sync_runner or run_sandboxed_sync
        self._sequence = 0

    @classmethod
    def from_registry(cls, registry, *, runner=None, sync_runner=None) -> "GuardedExecutor":
        try:
            provisioning = ExecutionProvisioning.from_registry(registry)
        except ValueError as exc:
            raise ExecutionBlocked(str(exc)) from exc
        return cls(provisioning, runner=runner, sync_runner=sync_runner)

    def toolchain_for_phase(self, phase: str, *, fallback: str | None = None) -> str:
        toolchain_id = self.provisioning.phase_toolchains.get(phase) or fallback
        if not toolchain_id:
            raise ExecutionBlocked(f"execution_toolchain_required:{phase}")
        if toolchain_id not in self.provisioning.toolchains:
            raise ExecutionBlocked(f"toolchain_provisioning_missing:{toolchain_id}")
        return toolchain_id

    def toolchain_for_engine(self, phase: str, engine: str, *, fallback: str | None = None) -> str:
        """Toolchain for a model phase whose provider matches the engine.

        A phase maps to one toolchain; a task may still pick another engine
        (e.g. codex worker while the phase maps to the claude toolchain). Then
        the single toolchain of that provider is used; ambiguity fails closed.
        """
        toolchain_id = self.toolchain_for_phase(phase, fallback=fallback)
        provider = self.provisioning.toolchains[toolchain_id].provider
        if not provider or provider == engine:
            return toolchain_id
        matches = [key for key, value in self.provisioning.toolchains.items() if value.provider == engine]
        if len(matches) != 1:
            raise ExecutionBlocked(f"engine_toolchain_unresolved:{phase}:{engine}")
        return matches[0]

    def roots(self, toolchain_id: str) -> tuple[tuple[Path, ...], tuple[Path, ...]]:
        try:
            toolchain = self.provisioning.toolchains[toolchain_id]
        except KeyError as exc:
            raise ExecutionBlocked(f"toolchain_provisioning_missing:{toolchain_id}") from exc
        return toolchain.read_roots, toolchain.denied_roots

    def ready(self) -> dict[str, object]:
        """Report concrete readiness; unsupported backends remain unavailable."""
        if sys.platform != "darwin" or not Path(SANDBOX_EXEC).is_file():
            return {"ready": False, "backend": "macos-sandbox-exec", "reason": "execution_isolation_unavailable"}
        if not self.provisioning.toolchains:
            return {"ready": False, "backend": "macos-sandbox-exec", "reason": "toolchain_provisioning_required"}
        model_toolchains = {
            self.provisioning.phase_toolchains[phase]
            for phase in self.provisioning.phase_toolchains
            if phase not in {"verify", "git", "host_script"}
        }
        for toolchain_id in model_toolchains:
            toolchain = self.provisioning.toolchains[toolchain_id]
            if not toolchain.auth_ready and toolchain.provider:
                return {
                    "ready": False,
                    "backend": "macos-sandbox-exec",
                    "reason": toolchain.auth_reason or "provider_auth_scope_unavailable",
                    "toolchain_id": toolchain.toolchain_id,
                }
        toolchain = next(iter(self.provisioning.toolchains.values()))
        with tempfile.TemporaryDirectory(prefix="whilly-swarm-readiness-") as raw_root:
            root = Path(raw_root)
            allowed = root / "allowed"
            output = root / "output"
            outside = root / "outside"
            allowed.mkdir()
            output.mkdir()
            outside.write_text("synthetic-readiness-fixture", encoding="utf-8")
            evidence = probe_sandbox(
                ExecutionPolicy(
                    phase="verify",
                    read_roots=(
                        str(allowed),
                        str(output),
                        "/bin",
                        "/usr/bin",
                        *(str(item) for item in toolchain.read_roots),
                    ),
                    write_roots=(str(output),),
                    denied_roots=(str(outside),),
                    network=False,
                    timeout_seconds=3,
                    max_output_bytes=1024 * 1024,
                    protected_write_roots=(str(output / ".git"),),
                ),
                root,
            )
        checks = evidence.get("checks", {})
        if not evidence.get("available") or not checks.get("outside_read_denied") or not checks.get("network_denied"):
            return {
                "ready": False,
                "backend": "macos-sandbox-exec",
                "reason": "execution_isolation_unavailable",
                "deny_probe": evidence,
            }
        return {"ready": True, "backend": "macos-sandbox-exec", "deny_probe": evidence}

    def environment(
        self,
        *,
        toolchain_id: str,
        phase: str,
        identity: Mapping[str, str] = (),
        attempt_root: str | Path | None = None,
    ) -> dict[str, str]:
        try:
            toolchain = self.provisioning.toolchains[toolchain_id]
        except KeyError as exc:
            raise ExecutionBlocked(f"toolchain_provisioning_missing:{toolchain_id}") from exc
        offline = phase in {"verify", "git", "host_script"}
        if toolchain.provider and not offline and not toolchain.auth_ready:
            raise ExecutionBlocked(toolchain.auth_reason or "provider_auth_scope_unavailable")
        root = Path(attempt_root).resolve(strict=False) if attempt_root is not None else None
        if root is not None:
            root.mkdir(parents=True, exist_ok=True)
            launch = Path(tempfile.mkdtemp(prefix=f"launch-{uuid.uuid4().hex}-", dir=root))
            home = launch / "home"
            temporary = launch / "tmp"
            home.mkdir(parents=True, exist_ok=True)
            temporary.mkdir(parents=True, exist_ok=True)
        else:
            temporary_root = toolchain.temporary
            temporary_root.mkdir(parents=True, exist_ok=True)
            launch = Path(tempfile.mkdtemp(prefix=f"launch-{uuid.uuid4().hex}-", dir=temporary_root))
            home = launch / "home"
            temporary = launch / "tmp"
            home.mkdir()
            temporary.mkdir()
        auth_values = self._provision_auth(toolchain, home) if not offline and toolchain.provider else {}
        result = build_swarm_environment(
            phase=phase,
            base=self.provisioning.base_environment,
            home=home,
            temporary=temporary,
            provider=toolchain.provider,
            provider_values=toolchain.provider_values,
            identity=identity,
            path=toolchain.path,
        )
        if toolchain.auth_mode == "claude_subscription_dir":
            result["CLAUDE_CONFIG_DIR"] = str(toolchain.auth_source_path)
        if toolchain.auth_mode in {"claude_subscription_dir", "claude_subscription_keychain"}:
            result["CLAUDE_CODE_TMPDIR"] = result["TMPDIR"]
            result["CLAUDE_TMPDIR"] = result["TMPDIR"]
        result.update(auth_values)
        result["WHILLY_SWARM_TOOLCHAIN_ID"] = toolchain_id
        return result

    @staticmethod
    def _provision_auth(toolchain, home: Path) -> dict[str, str]:
        if toolchain.auth_mode is None:
            raise ExecutionBlocked(toolchain.auth_reason or "provider_auth_scope_unavailable")
        if toolchain.auth_mode == "codex_subscription_file":
            source = toolchain.auth_source_path
            if source is None or not source.is_file():
                raise ExecutionBlocked("provider_auth_scope_unavailable")
            destination = home / ".codex" / "auth.json"
            destination.parent.mkdir(mode=0o700)
            shutil.copyfile(source, destination)
            destination.chmod(0o600)
            return {}
        if toolchain.auth_mode == "claude_subscription_dir":
            source = toolchain.auth_source_path
            if source is None or not source.is_dir() or source.is_symlink():
                raise ExecutionBlocked("provider_auth_scope_unavailable")
            return {}
        if toolchain.auth_mode == "claude_subscription_keychain":
            return {"CLAUDE_CODE_OAUTH_TOKEN": GuardedExecutor._read_claude_keychain_token(toolchain.auth_service)}
        if toolchain.auth_mode == "api_key" and not toolchain.provider_values:
            raise ExecutionBlocked("provider_auth_scope_unavailable")
        return {}

    @staticmethod
    def _read_claude_keychain_token(service: str | None) -> str:
        if not service:
            raise ExecutionBlocked("provider_auth_scope_unavailable")
        completed = subprocess.run(
            ["/usr/bin/security", "find-generic-password", "-s", service, "-w"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if completed.returncode != 0:
            raise ExecutionBlocked("provider_auth_scope_unavailable")
        try:
            payload = json.loads(completed.stdout)
            token = payload["claudeAiOauth"]["accessToken"]
        except (KeyError, TypeError, ValueError):
            raise ExecutionBlocked("provider_auth_scope_unavailable") from None
        if not isinstance(token, str) or not token:
            raise ExecutionBlocked("provider_auth_scope_unavailable")
        return token

    def _selected_secrets(self, environment: Mapping[str, str], phase: str) -> tuple[str, ...]:
        if phase in {"verify", "git", "host_script"}:
            return self.provisioning.secret_values
        toolchain_id = environment.get("WHILLY_SWARM_TOOLCHAIN_ID")
        toolchain = self.provisioning.toolchains.get(toolchain_id or "")
        if toolchain is None:
            return self.provisioning.secret_values
        values = list(self.provisioning.secret_values)
        values.extend(value for value in toolchain.provider_values.values() if value)
        oauth_token = environment.get("CLAUDE_CODE_OAUTH_TOKEN")
        if oauth_token:
            values.append(oauth_token)
        source = toolchain.auth_source_path if toolchain.auth_mode == "codex_subscription_file" else None
        if source is not None and source.is_file():
            try:
                payload = json.loads(source.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                payload = None

            def collect(value: object) -> None:
                if isinstance(value, str) and value:
                    values.append(value)
                elif isinstance(value, dict):
                    for child in value.values():
                        collect(child)
                elif isinstance(value, list):
                    for child in value:
                        collect(child)

            collect(payload)
        return tuple(dict.fromkeys(values))

    @staticmethod
    def _redact(path: Path, secrets: Sequence[str]) -> None:
        if not secrets:
            return
        # The child never receives this directory as a writable root.  Still
        # open descriptor-first: a later change must not turn log redaction
        # into a symlink-following host write primitive.
        flags = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(path, flags)
        except FileNotFoundError:
            return
        try:
            file_stat = os.fstat(fd)
            if not stat.S_ISREG(file_stat.st_mode) or file_stat.st_nlink < 1:
                raise ExecutionBlocked("host_output_not_regular")
            data = os.read(fd, file_stat.st_size)
            for secret in secrets:
                if secret:
                    data = data.replace(secret.encode(), b"[REDACTED]")
            os.lseek(fd, 0, os.SEEK_SET)
            os.ftruncate(fd, 0)
            os.write(fd, data)
        except OSError as exc:
            raise ExecutionBlocked("host_output_redaction_failed") from exc
        finally:
            os.close(fd)

    @staticmethod
    def _validate_output_boundary(log_dir: Path, policy: ExecutionPolicy) -> None:
        output = log_dir.resolve(strict=False)
        for write_root in policy.write_roots:
            candidate = Path(write_root).resolve(strict=False)
            try:
                output.relative_to(candidate)
            except ValueError:
                continue
            raise ExecutionBlocked("host_output_nested_under_child_write_root")

    def _output_paths(self, log_dir: str | Path, phase: str, policy: ExecutionPolicy) -> tuple[Path, Path]:
        requested = Path(log_dir)
        if requested.is_symlink():
            raise ExecutionBlocked("host_output_symlink_detected")
        directory = requested.resolve(strict=False)
        self._validate_output_boundary(directory, policy)
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        output_dir = Path(tempfile.mkdtemp(prefix=f"{phase}-", dir=directory))
        output_dir.chmod(0o700)
        stdout = output_dir / "stdout.log"
        stderr = output_dir / "stderr.log"
        for path in (stdout, stderr):
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0), 0o600)
            os.close(fd)
        return stdout, stderr

    @staticmethod
    def _finalize_output(stdout: Path, stderr: Path, result, secrets: Sequence[str]) -> None:
        for path in (stdout, stderr):
            if path.is_symlink():
                raise ExecutionBlocked("host_output_symlink_detected")
        GuardedExecutor._redact(stdout, secrets)
        GuardedExecutor._redact(stderr, secrets)

    async def run(
        self,
        argv: Sequence[str],
        *,
        phase: str,
        cwd: str | Path,
        policy: ExecutionPolicy,
        environment: Mapping[str, str],
        log_dir: str | Path,
        stdin_path: str | Path | None = None,
        on_start: Callable[[int], Awaitable[None]] | None = None,
    ) -> "ProcessOutcome":
        if policy.phase != phase:
            raise ExecutionBlocked("execution_phase_mismatch")
        if not argv or not isinstance(argv[0], str) or not argv[0] or any(not isinstance(item, str) for item in argv):
            raise ValueError("argv must be a non-empty sequence of strings")
        root = Path(cwd).resolve(strict=False)
        allowed = tuple(Path(item).resolve(strict=False) for item in (*policy.read_roots, *policy.write_roots))
        if not any(os.path.commonpath((str(root), str(item))) == str(item) for item in allowed):
            raise ExecutionBlocked("cwd_outside_policy")
        stdout, stderr = self._output_paths(log_dir, phase, policy)
        result = await self._runner(
            tuple(argv),
            policy=policy,
            cwd=root,
            environment=environment,
            stdout_path=stdout,
            stderr_path=stderr,
            stdin_path=stdin_path,
            on_start=on_start,
        )
        self._finalize_output(stdout, stderr, result, self._selected_secrets(environment, phase))
        from whilly.swarm.agent import ProcessOutcome

        return ProcessOutcome(
            argv=tuple(argv),
            exit_code=result.exit_code,
            timed_out=result.timed_out,
            cancelled=result.cancelled,
            duration_seconds=result.duration_seconds,
            stdout_path=result.stdout_path,
            stderr_path=result.stderr_path,
            reason=result.reason,
            backend=result.backend,
        )

    def run_sync(self, *args, **kwargs):
        """Synchronous bridge for coordinator Git/BMAD callers."""
        phase = kwargs.get("phase")
        policy = kwargs.get("policy")
        if phase != policy.phase:
            raise ExecutionBlocked("execution_phase_mismatch")
        argv = tuple(args[0])
        cwd = kwargs["cwd"]
        environment = kwargs["environment"]
        stdout, stderr = self._output_paths(kwargs["log_dir"], phase, policy)
        result = self._sync_runner(
            argv,
            policy=policy,
            cwd=cwd,
            environment=environment,
            stdout_path=stdout,
            stderr_path=stderr,
            stdin_path=kwargs.get("stdin_path"),
        )
        self._finalize_output(stdout, stderr, result, self._selected_secrets(environment, phase))
        return result
