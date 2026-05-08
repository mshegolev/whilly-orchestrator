"""Tests for the coding-agent subprocess environment contract."""

from __future__ import annotations

import pytest

from whilly.adapters.runner.env import (
    BASE_RUNNER_ENV_ALLOWLIST,
    build_runner_env,
    required_env_for_model,
)


HIDDEN_SECRET_NAMES = (
    "WHILLY_DATABASE_URL",
    "WHILLY_WORKER_TOKEN",
    "WHILLY_WORKER_BOOTSTRAP_TOKEN",
    "WHILLY_ADMIN_TOKEN",
    "GH_TOKEN",
    "GITHUB_TOKEN",
    "WHILLY_GH_TOKEN",
    "SLACK_ACCESS_TOKEN",
    "SLACK_BOT_TOKEN",
)


def test_base_runner_env_allowlist_contract_is_exact() -> None:
    assert BASE_RUNNER_ENV_ALLOWLIST == (
        "PATH",
        "HOME",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "TMPDIR",
        "XDG_CONFIG_HOME",
        "XDG_CACHE_HOME",
        "SSL_CERT_FILE",
        "REQUESTS_CA_BUNDLE",
        "NODE_EXTRA_CA_CERTS",
        "WHILLY_MODEL",
        "CLAUDE_BIN",
        "WHILLY_OPENCODE_BIN",
        "WHILLY_CLAUDE_SAFE",
        "WHILLY_AGENT_ALLOW_SHELL",
        "WHILLY_OPENCODE_SAFE",
        "WHILLY_HANDOFF_DIR",
        "WHILLY_HANDOFF_TIMEOUT",
    )


def test_build_runner_env_copies_only_allowlisted_base_names() -> None:
    parent = {
        "PATH": "/usr/bin",
        "HOME": "/home/operator",
        "LANG": "C.UTF-8",
        "WHILLY_MODEL": "claude-opus-4-6[1m]",
        "UNRELATED_FLAG": "do-not-forward",
        "WHILLY_DATABASE_URL": "postgres://user:pass@example/db",
    }

    env = build_runner_env(parent)

    assert env == {
        "HOME": "/home/operator",
        "LANG": "C.UTF-8",
        "PATH": "/usr/bin",
        "WHILLY_MODEL": "claude-opus-4-6[1m]",
    }
    assert list(env) == sorted(env)


@pytest.mark.parametrize(
    ("model", "backend", "expected"),
    [
        ("claude-opus-4-6[1m]", "claude", ("ANTHROPIC_API_KEY",)),
        ("anthropic/claude-sonnet-4-5", "opencode", ("ANTHROPIC_API_KEY",)),
        ("openai/gpt-5", "opencode", ("OPENAI_API_KEY",)),
        ("gpt-5", "opencode", ("OPENAI_API_KEY",)),
        ("groq/openai/gpt-oss-120b", "opencode", ("GROQ_API_KEY",)),
        ("gemini-2.5-pro", "opencode", ("GEMINI_API_KEY",)),
        ("google/gemini-2.5-pro", "opencode", ("GEMINI_API_KEY",)),
        ("openrouter/anthropic/claude-sonnet-4", "opencode", ("OPENROUTER_API_KEY",)),
        ("opencode/big-pickle", "opencode", ()),
        ("opencode/custom-model", "opencode", ("OPENCODE_API_KEY", "OPENCODE_ZEN_API_KEY")),
    ],
)
def test_required_env_for_model_infers_provider_credentials(
    model: str,
    backend: str,
    expected: tuple[str, ...],
) -> None:
    assert required_env_for_model(model, backend=backend) == expected


def test_build_runner_env_includes_required_provider_credentials_for_model() -> None:
    parent = {
        "PATH": "/usr/bin",
        "GROQ_API_KEY": "gsk_test",
        "OPENAI_API_KEY": "sk-test",
        "WHILLY_WORKER_TOKEN": "hidden-worker-token",
    }

    env = build_runner_env(parent, model="groq/openai/gpt-oss-120b", backend="opencode")

    assert env == {
        "GROQ_API_KEY": "gsk_test",
        "PATH": "/usr/bin",
    }


def test_build_runner_env_keeps_zero_key_opencode_big_pickle() -> None:
    parent = {
        "PATH": "/usr/bin",
        "OPENCODE_API_KEY": "should-not-be-needed",
        "OPENCODE_ZEN_API_KEY": "should-not-be-needed",
    }

    env = build_runner_env(parent, model="opencode/big-pickle", backend="opencode")

    assert env == {"PATH": "/usr/bin"}


def test_build_runner_env_excludes_hidden_operational_secrets_by_default() -> None:
    parent = {name: f"value-for-{name}" for name in HIDDEN_SECRET_NAMES}
    parent.update(
        {
            "PATH": "/usr/bin",
            "HOME": "/home/operator",
            "ANTHROPIC_API_KEY": "sk-ant-test",
        }
    )

    env = build_runner_env(parent, model="claude-opus-4-6[1m]", backend="claude")

    assert env == {
        "ANTHROPIC_API_KEY": "sk-ant-test",
        "HOME": "/home/operator",
        "PATH": "/usr/bin",
    }
    for name in HIDDEN_SECRET_NAMES:
        assert name not in env


def test_build_runner_env_can_forward_hidden_names_only_when_explicitly_required() -> None:
    parent = {
        "PATH": "/usr/bin",
        "GH_TOKEN": "explicit-gh-token",
        "SLACK_ACCESS_TOKEN": "explicit-slack-token",
        "WHILLY_DATABASE_URL": "postgres://user:pass@example/db",
    }

    env = build_runner_env(parent, required_env=("GH_TOKEN", "SLACK_ACCESS_TOKEN"))

    assert env == {
        "GH_TOKEN": "explicit-gh-token",
        "PATH": "/usr/bin",
        "SLACK_ACCESS_TOKEN": "explicit-slack-token",
    }
    assert "WHILLY_DATABASE_URL" not in env
