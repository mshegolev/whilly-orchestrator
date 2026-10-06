from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from whilly.core.swarm_execution import ExecutionPolicy, SandboxResult
from whilly.swarm.agent import run_engine
from whilly.swarm.execution import ExecutionBlocked, GuardedExecutor
from whilly.swarm.execution_config import ExecutionProvisioning, ToolchainProvision
from whilly.swarm.registry import EngineConfig
from whilly.swarm.runtime import _await_blocking
from whilly.swarm.verification import parse_gate_result
from whilly.swarm.verification_runner import VerificationRunner


def _policy(root: Path, phase: str) -> ExecutionPolicy:
    return ExecutionPolicy(
        phase=phase,
        read_roots=(str(root),),
        write_roots=(str(root / "work"),),
        denied_roots=(),
        network=phase in {"discussion", "planner", "escalation", "worker", "review"},
        timeout_seconds=2,
        max_output_bytes=1024,
    )


@pytest.fixture
def provision(tmp_path: Path) -> ExecutionProvisioning:
    home = tmp_path / "home"
    temporary = tmp_path / "tmp"
    (tmp_path / "work").mkdir()
    (tmp_path / "logs").mkdir()
    home.mkdir()
    temporary.mkdir()
    return ExecutionProvisioning(
        toolchains={
            "fixture-toolchain": ToolchainProvision(
                "fixture-toolchain", (tmp_path,), "/bin", home, temporary, auth_ready=True
            )
        },
        secret_values=("synthetic-secret",),
    )


def _fake_runner(calls: list[tuple[str, ...]]):
    async def run(argv, *, policy, cwd, environment, stdout_path, stderr_path, stdin_path=None, on_start=None):
        calls.append(tuple(argv))
        Path(stdout_path).write_text(f"synthetic-secret phase={policy.phase}\n", encoding="utf-8")
        Path(stderr_path).write_text("clean\n", encoding="utf-8")
        if on_start is not None:
            await on_start(123)
        return SandboxResult(0, None, False, False, 0.001, str(stdout_path), str(stderr_path), "fixture")

    return run


@pytest.mark.asyncio
async def test_blocking_runtime_operation_finishes_before_cancellation_propagates() -> None:
    started = threading.Event()
    release = threading.Event()

    def blocking_operation() -> str:
        started.set()
        release.wait(timeout=2)
        return "done"

    task = asyncio.create_task(_await_blocking(blocking_operation))
    await asyncio.to_thread(started.wait, 2)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_guarded_executor_runs_every_phase_with_explicit_fixture_configuration(
    tmp_path: Path, provision: ExecutionProvisioning
) -> None:
    calls: list[tuple[str, ...]] = []
    executor = GuardedExecutor(provision, runner=_fake_runner(calls))
    environment = executor.environment(
        toolchain_id="fixture-toolchain",
        phase="worker",
        identity={"WHILLY_SWARM_TASK": "task-1"},
    )
    for phase in ("discussion", "planner", "escalation", "worker", "review", "verify", "git", "host_script"):
        policy = _policy(tmp_path, phase)
        result = await executor.run(
            ("fixture-provider", phase),
            phase=phase,
            cwd=tmp_path,
            policy=policy,
            environment=environment,
            log_dir=tmp_path / "logs" / phase,
        )
        assert result.ok
        assert "synthetic-secret" not in Path(result.stdout_path).read_text()
        assert "[REDACTED]" in Path(result.stdout_path).read_text()
    assert len(calls) == 8


@pytest.mark.asyncio
async def test_guarded_executor_preserves_named_backend_blocker(
    tmp_path: Path, provision: ExecutionProvisioning
) -> None:
    async def blocked(*args, **kwargs):
        stdout = Path(kwargs["stdout_path"])
        stderr = Path(kwargs["stderr_path"])
        stdout.write_bytes(b"")
        stderr.write_bytes(b"")
        return SandboxResult(
            None, "execution_isolation_unavailable", False, False, 0.0, str(stdout), str(stderr), "fixture"
        )

    executor = GuardedExecutor(provision, runner=blocked)
    result = await executor.run(
        ("fixture",),
        phase="verify",
        cwd=tmp_path,
        policy=_policy(tmp_path, "verify"),
        environment=executor.environment(toolchain_id="fixture-toolchain", phase="verify"),
        log_dir=tmp_path / "logs",
    )
    assert result.reason == "execution_isolation_unavailable"
    evidence = parse_gate_result(
        "ruff-json",
        result,
        b"[]",
        stage="candidate",
        category="lint",
        argv=("fixture",),
        head_sha="a" * 40,
        policy_digest="b" * 64,
    )
    assert evidence.outcome == "isolation_unavailable"


@pytest.mark.asyncio
async def test_legacy_engine_entry_point_requires_policy(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="execution_policy_required"):
        await run_engine(
            "claude",
            EngineConfig(("fixture",)),
            mode="worker",
            prompt="x",
            cwd=tmp_path,
            log_dir=tmp_path / "logs",
            name="legacy",
            timeout_seconds=1,
        )


def test_ready_does_not_claim_unsupported_real_backend() -> None:
    result = GuardedExecutor().ready()
    assert isinstance(result["ready"], bool)
    if result["ready"] is False:
        assert result["reason"] in {"execution_isolation_unavailable", "toolchain_provisioning_required"}


def test_registry_provisioning_selects_model_phase_without_project_gate(tmp_path: Path) -> None:
    (tmp_path / "fixture-auth").write_text('{"synthetic": true}', encoding="utf-8")
    registry = SimpleNamespace(
        raw={
            "execution": {
                "phases": {"planner": "planner-fixture", "verify": "verify-fixture"},
                "toolchains": {
                    "planner-fixture": {
                        "read_roots": [str(tmp_path)],
                        "path": "/bin",
                        "provider": "codex",
                        "auth": {"mode": "codex_subscription_file", "source_path": str(tmp_path / "fixture-auth")},
                    },
                    "verify-fixture": {"read_roots": [str(tmp_path)], "path": "/bin"},
                },
            }
        },
        resolved_state_dir=lambda: tmp_path / "state",
    )
    executor = GuardedExecutor.from_registry(registry)
    assert executor.toolchain_for_phase("planner") == "planner-fixture"
    assert executor.toolchain_for_phase("verify") == "verify-fixture"
    planner_env = executor.environment(
        toolchain_id="planner-fixture", phase="planner", attempt_root=tmp_path / "attempt"
    )
    assert Path(planner_env["HOME"]).parent.parent == tmp_path / "attempt"
    assert Path(planner_env["TMPDIR"]).parent.parent == tmp_path / "attempt"
    assert "fixture-auth" not in str(executor.provisioning.toolchains["planner-fixture"].provider_values)


def test_claude_subscription_uses_explicit_read_only_config_directory(tmp_path: Path) -> None:
    config = tmp_path / "claude-config"
    config.mkdir()
    registry = SimpleNamespace(
        raw={
            "execution": {
                "phases": {"planner": "claude-local"},
                "toolchains": {
                    "claude-local": {
                        "read_roots": [str(tmp_path)],
                        "path": "/bin",
                        "provider": "claude",
                        "auth": {"mode": "claude_subscription_dir", "source_path": str(config)},
                    }
                },
            }
        },
        resolved_state_dir=lambda: tmp_path / "state",
    )

    executor = GuardedExecutor.from_registry(registry)
    environment = executor.environment(
        toolchain_id="claude-local", phase="planner", attempt_root=tmp_path / "attempt"
    )

    assert environment["CLAUDE_CONFIG_DIR"] == str(config)
    assert environment["CLAUDE_CODE_TMPDIR"] == environment["TMPDIR"]
    assert environment["CLAUDE_TMPDIR"] == environment["TMPDIR"]
    assert environment["HOME"] != str(config.parent)
    assert "HTTP_PROXY" not in environment
    assert "HTTPS_PROXY" not in environment


def test_claude_keychain_auth_is_host_selected_and_redactable(tmp_path: Path, monkeypatch) -> None:
    registry = SimpleNamespace(
        raw={
            "execution": {
                "phases": {"planner": "claude-local"},
                "toolchains": {
                    "claude-local": {
                        "read_roots": [str(tmp_path)],
                        "path": "/bin",
                        "provider": "claude",
                        "auth": {"mode": "claude_subscription_keychain", "service": "fixture-service"},
                    }
                },
            }
        },
        resolved_state_dir=lambda: tmp_path / "state",
    )
    monkeypatch.setattr(GuardedExecutor, "_read_claude_keychain_token", staticmethod(lambda service: "secret-token"))

    executor = GuardedExecutor.from_registry(registry)
    environment = executor.environment(
        toolchain_id="claude-local", phase="planner", attempt_root=tmp_path / "attempt"
    )

    assert environment["CLAUDE_CODE_OAUTH_TOKEN"] == "secret-token"
    assert environment["CLAUDE_CODE_TMPDIR"] == environment["TMPDIR"]
    assert "secret-token" in executor._selected_secrets(environment, "planner")


def test_environment_allocates_distinct_home_and_tmp_for_repeated_launches(tmp_path: Path, provision) -> None:
    executor = GuardedExecutor(provision)
    first = executor.environment(toolchain_id="fixture-toolchain", phase="verify", attempt_root=tmp_path / "attempt")
    second = executor.environment(toolchain_id="fixture-toolchain", phase="verify", attempt_root=tmp_path / "attempt")
    assert first["HOME"] != second["HOME"]
    assert first["TMPDIR"] != second["TMPDIR"]
    assert Path(first["HOME"]).parent.parent == tmp_path / "attempt"
    assert Path(second["TMPDIR"]).parent.parent == tmp_path / "attempt"


def test_offline_phase_does_not_require_model_auth(tmp_path: Path) -> None:
    home = tmp_path / "host-home"
    temporary = tmp_path / "host-tmp"
    provisioning = ExecutionProvisioning(
        toolchains={"codex": ToolchainProvision("codex", (tmp_path,), "/bin", home, temporary, provider="codex")},
        phase_toolchains={"verify": "codex"},
    )
    executor = GuardedExecutor(provisioning)
    environment = executor.environment(toolchain_id="codex", phase="verify", attempt_root=tmp_path / "attempt")
    assert environment["HOME"]


def test_registry_rejects_boolean_auth_ready_without_scoped_mode(tmp_path: Path) -> None:
    registry = SimpleNamespace(
        raw={
            "execution": {
                "phases": {"planner": "planner"},
                "toolchains": {
                    "planner": {
                        "read_roots": [str(tmp_path)],
                        "path": "/bin",
                        "provider": "claude",
                        "auth": {"ready": True},
                    }
                },
            }
        },
        resolved_state_dir=lambda: tmp_path / "state",
    )
    from whilly.core.swarm_execution import ExecutionBlocked

    with pytest.raises(ExecutionBlocked, match="auth_mode"):
        GuardedExecutor.from_registry(registry)


def test_verification_runner_wraps_gate_count_without_confusing_acceptance_bool() -> None:
    evidence = VerificationRunner.wrap_evidence(
        stage="candidate",
        category="architecture",
        argv=("fixture",),
        head_sha="a" * 40,
        policy_digest="b" * 64,
        outcome="passed",
        collected=3,
        passed=1,
        skipped=2,
    )
    assert evidence["passed"] is True
    assert evidence["evidence"]["passed"] == 1
    assert evidence["evidence"]["skipped"] == 2


def test_toolchain_for_engine_switches_to_the_engine_provider(tmp_path: Path) -> None:
    (tmp_path / "fixture-auth").write_text('{"synthetic": true}', encoding="utf-8")
    codex = {
        "read_roots": [str(tmp_path)],
        "path": "/bin",
        "provider": "codex",
        "auth": {"mode": "codex_subscription_file", "source_path": str(tmp_path / "fixture-auth")},
    }
    registry = SimpleNamespace(
        raw={
            "execution": {
                "phases": {"worker": "claude-fixture", "verify": "verify-fixture"},
                "toolchains": {
                    "claude-fixture": {
                        "read_roots": [str(tmp_path)],
                        "path": "/bin",
                        "provider": "claude",
                        "auth": {"mode": "claude_subscription_dir", "source_path": str(tmp_path)},
                    },
                    "codex-fixture": codex,
                    "verify-fixture": {"read_roots": [str(tmp_path)], "path": "/bin"},
                },
            }
        },
        resolved_state_dir=lambda: tmp_path / "state",
    )
    executor = GuardedExecutor.from_registry(registry)
    assert executor.toolchain_for_engine("worker", "claude") == "claude-fixture"
    assert executor.toolchain_for_engine("worker", "codex") == "codex-fixture"
    assert executor.toolchain_for_engine("verify", "codex") == "verify-fixture"
    with pytest.raises(ExecutionBlocked, match="engine_toolchain_unresolved"):
        executor.toolchain_for_engine("worker", "gemini")
