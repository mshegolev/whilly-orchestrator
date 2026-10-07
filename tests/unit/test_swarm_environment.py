"""Tests for explicit, task-scoped swarm child environments."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from whilly.adapters.runner.swarm_environment import build_swarm_environment


PHASES = ("discussion", "planner", "escalation", "worker", "review", "verify", "git", "host_script")
OFFLINE_PHASES = ("verify", "git", "host_script")
ONLINE_PHASES = tuple(phase for phase in PHASES if phase not in OFFLINE_PHASES)
PROVIDER_CASES = (("claude", "ANTHROPIC_API_KEY"), ("codex", "OPENAI_API_KEY"))


def _build(*, phase: str, provider: str | None = None) -> dict[str, str]:
    return build_swarm_environment(
        phase=phase,
        base={
            "PATH": "/ambient/bin",
            "HOME": "/ambient/home",
            "TMPDIR": "/ambient/tmp",
            "LANG": "C.UTF-8",
            "LC_ALL": "C",
            "TZ": "UTC",
            "WHILLY_DATABASE_URL": "db-secret",
            "DATABASE_URL": "database-secret",
            "WHILLY_ADMIN_TOKEN": "admin-secret",
            "ADMIN_TOKEN": "admin-token-secret",
            "ANTHROPIC_API_KEY": "ambient-secret",
            "OPENAI_API_KEY": "ambient-openai-secret",
            "GH_TOKEN": "github-secret",
            "GITHUB_TOKEN": "github-token-secret",
            "CLOUD_API_KEY": "cloud-secret",
            "SLACK_ACCESS_TOKEN": "publication-secret",
            "SLACK_BOT_TOKEN": "publication-bot-secret",
            "WHILLY_PUBLICATION_TOKEN": "publication-token-secret",
            "SSH_PRIVATE_KEY": "ssh-private-secret",
            "WHILLY_SSH_KEY": "whilly-ssh-secret",
            "GIT_SSH_COMMAND": "ssh-command-secret",
            "PYTHONPATH": "/ambient/python",
            "LD_PRELOAD": "/ambient/preload.so",
            "DYLD_INSERT_LIBRARIES": "/ambient/dyld.dylib",
        },
        home=Path("/host/home"),
        temporary=Path("/host/tmp"),
        provider=provider,
        provider_values={},
        identity={
            "WHILLY_SWARM_SESSION": "session-1",
            "WHILLY_SWARM_TASK": "task-2",
            "WHILLY_SWARM_REVIEW": "review-1",
            "WHILLY_SWARM_MAILBOX": "/host/mailbox",
        },
        path="/host/bin",
    )


@pytest.mark.parametrize("phase", PHASES)
def test_every_phase_has_only_explicit_host_values_and_no_canaries(phase: str) -> None:
    env = _build(phase=phase)

    assert env["PATH"] == "/host/bin"
    assert env["HOME"] == "/host/home"
    assert env["TMPDIR"] == "/host/tmp"
    assert env["LANG"] == "C.UTF-8"
    assert env["LC_ALL"] == "C"
    assert env["TZ"] == "UTC"
    assert env["WHILLY_SWARM_SESSION"] == "session-1"
    assert env["WHILLY_SWARM_TASK"] == "task-2"
    assert env["WHILLY_SWARM_REVIEW"] == "review-1"
    assert env["WHILLY_SWARM_MAILBOX"] == "/host/mailbox"
    assert (
        not {
            "WHILLY_DATABASE_URL",
            "DATABASE_URL",
            "WHILLY_ADMIN_TOKEN",
            "ADMIN_TOKEN",
            "ANTHROPIC_API_KEY",
            "OPENAI_API_KEY",
            "GH_TOKEN",
            "GITHUB_TOKEN",
            "CLOUD_API_KEY",
            "SLACK_ACCESS_TOKEN",
            "SLACK_BOT_TOKEN",
            "WHILLY_PUBLICATION_TOKEN",
            "SSH_PRIVATE_KEY",
            "WHILLY_SSH_KEY",
            "GIT_SSH_COMMAND",
            "PYTHONPATH",
            "LD_PRELOAD",
            "DYLD_INSERT_LIBRARIES",
        }
        & env.keys()
    )


@pytest.mark.parametrize(("provider", "credential"), PROVIDER_CASES)
@pytest.mark.parametrize("phase", ONLINE_PHASES)
def test_selected_provider_gets_only_its_host_supplied_credential(phase: str, provider: str, credential: str) -> None:
    env = build_swarm_environment(
        phase=phase,
        base={},
        home=Path("/home/host"),
        temporary=Path("/tmp/host"),
        provider=provider,
        provider_values={credential: f"{provider}-secret"},
        identity={},
        path="/bin",
    )

    assert env[credential] == f"{provider}-secret"
    assert set(env) == {"PATH", "HOME", "TMPDIR", credential}


@pytest.mark.parametrize(("provider", "credential"), PROVIDER_CASES)
@pytest.mark.parametrize("phase", ONLINE_PHASES)
def test_selected_provider_without_value_uses_no_ambient_credential(phase: str, provider: str, credential: str) -> None:
    env = build_swarm_environment(
        phase=phase,
        base={
            "PATH": "/ambient/bin",
            "HOME": "/ambient/home",
            "TMPDIR": "/ambient/tmp",
            "ANTHROPIC_API_KEY": "ambient-claude-secret",
            "OPENAI_API_KEY": "ambient-codex-secret",
        },
        home=Path("/host/home"),
        temporary=Path("/host/tmp"),
        provider=provider,
        provider_values={},
        identity={},
        path="/host/bin",
    )

    assert credential not in env
    assert "ANTHROPIC_API_KEY" not in env
    assert "OPENAI_API_KEY" not in env
    assert env == {"PATH": "/host/bin", "HOME": "/host/home", "TMPDIR": "/host/tmp"}


@pytest.mark.parametrize("phase", OFFLINE_PHASES)
@pytest.mark.parametrize(("provider", "credential"), PROVIDER_CASES)
def test_offline_phases_never_receive_provider_credentials(phase: str, provider: str, credential: str) -> None:
    env = build_swarm_environment(
        phase=phase,
        base={},
        home=Path("/home/host"),
        temporary=Path("/tmp/host"),
        provider=provider,
        provider_values={credential: "synthetic-secret"},
        identity={},
        path="/bin",
    )

    assert credential not in env


@pytest.mark.parametrize(
    ("argument", "value"),
    [
        ("phase", "unknown"),
        ("provider", "unknown"),
    ],
)
def test_unknown_phase_or_provider_is_rejected(argument: str, value: str) -> None:
    kwargs = dict(
        phase="discussion",
        base={},
        home=Path("/home/host"),
        temporary=Path("/tmp/host"),
        provider="claude",
        provider_values={},
        identity={},
        path="/bin",
    )
    kwargs[argument] = value

    with pytest.raises(ValueError, match=argument):
        build_swarm_environment(**kwargs)


def test_unknown_identity_and_provider_keys_are_rejected() -> None:
    with pytest.raises(ValueError, match="identity"):
        build_swarm_environment(
            phase="discussion",
            base={},
            home=Path("/home/host"),
            temporary=Path("/tmp/host"),
            provider=None,
            provider_values={},
            identity={"WHILLY_SWARM_UNKNOWN": "value"},
            path="/bin",
        )

    with pytest.raises(ValueError, match="provider"):
        build_swarm_environment(
            phase="discussion",
            base={},
            home=Path("/home/host"),
            temporary=Path("/tmp/host"),
            provider="claude",
            provider_values={"OPENAI_API_KEY": "wrong-provider-secret"},
            identity={},
            path="/bin",
        )


def test_fake_child_receives_built_environment_without_secret_diagnostic_output(tmp_path: Path) -> None:
    env = build_swarm_environment(
        phase="worker",
        base={"LANG": "C.UTF-8"},
        home=tmp_path / "home",
        temporary=tmp_path / "tmp",
        provider="claude",
        provider_values={"ANTHROPIC_API_KEY": "synthetic-secret"},
        identity={"WHILLY_SWARM_TASK": "task-2"},
        path="/host/bin",
    )
    child = subprocess.run(
        [
            sys.executable,
            "-c",
            "import json, os; print(json.dumps(sorted(k for k in os.environ if k.startswith(('WHILLY_', 'ANTHROPIC_', 'OPENAI_')))))",
        ],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )

    assert json.loads(child.stdout) == ["ANTHROPIC_API_KEY", "WHILLY_SWARM_TASK"]
    assert "synthetic-secret" not in child.stdout
