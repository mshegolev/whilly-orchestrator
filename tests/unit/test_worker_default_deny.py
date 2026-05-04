"""Default-deny worker agent across both Claude dispatch paths (M1 hardening).

Covers VAL-SEC-018 / -019 / -020 / -021 / -022 — the BREAKING change that
flips the worker agent's default permission posture from
``--dangerously-skip-permissions`` to a denylist of write/shell tools, with
a single legacy opt-in via ``WHILLY_AGENT_ALLOW_SHELL=1``.

The two dispatch paths under test:

* ``whilly.agents.claude.ClaudeBackend.build_command`` — the synchronous
  worker used by the tmux runner / agent_runner stack.
* ``whilly.adapters.runner.claude_cli.build_command`` — the async worker
  used by the v4 worker pipeline.

Tests assert on the captured argv: parity is enforced by parametrizing
across both paths so a regression in either one fails the suite.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator

import pytest

from whilly.adapters.runner import claude_cli
from whilly.agents.claude import ClaudeBackend


_DENYLIST_TOOLS: tuple[str, ...] = (
    "Write",
    "Edit",
    "MultiEdit",
    "NotebookEdit",
    "Bash",
)


def _sync_build(prompt: str = "hello") -> list[str]:
    return ClaudeBackend().build_command(prompt)


def _async_build(prompt: str = "hello") -> list[str]:
    return claude_cli.build_command(prompt, "claude-opus-4-6[1m]")


_DISPATCH_PATHS: tuple[tuple[str, Callable[..., list[str]]], ...] = (
    ("sync_claude_backend", _sync_build),
    ("async_claude_cli", _async_build),
)


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for key in (
        "CLAUDE_BIN",
        "WHILLY_MODEL",
        "WHILLY_CLAUDE_SAFE",
        "WHILLY_AGENT_ALLOW_SHELL",
    ):
        monkeypatch.delenv(key, raising=False)
    yield


def _disallowed_value(cmd: list[str]) -> str:
    """Return the argument value following the ``--disallowedTools`` flag."""
    assert "--disallowedTools" in cmd, f"--disallowedTools missing from argv: {cmd!r}"
    idx = cmd.index("--disallowedTools")
    assert idx + 1 < len(cmd), f"--disallowedTools is the final argv slot: {cmd!r}"
    return cmd[idx + 1]


@pytest.mark.parametrize(("name", "build"), _DISPATCH_PATHS, ids=[n for n, _ in _DISPATCH_PATHS])
def test_default_argv_denies_dangerous_tools(
    clean_env: None,
    name: str,
    build: Callable[..., list[str]],
) -> None:
    """VAL-SEC-018: default argv contains a denylist with all five tools."""
    cmd = build()
    value = _disallowed_value(cmd)
    tools = {tool.strip() for tool in value.split(",") if tool.strip()}
    for required in _DENYLIST_TOOLS:
        assert required in tools, f"{name}: {required!r} missing from disallowed list {value!r}"


@pytest.mark.parametrize(("name", "build"), _DISPATCH_PATHS, ids=[n for n, _ in _DISPATCH_PATHS])
def test_default_argv_omits_dangerously_skip_permissions(
    clean_env: None,
    name: str,
    build: Callable[..., list[str]],
) -> None:
    """VAL-SEC-019: default argv omits ``--dangerously-skip-permissions``."""
    cmd = build()
    assert "--dangerously-skip-permissions" not in cmd, (
        f"{name}: --dangerously-skip-permissions leaked into default argv: {cmd!r}"
    )


@pytest.mark.parametrize(("name", "build"), _DISPATCH_PATHS, ids=[n for n, _ in _DISPATCH_PATHS])
def test_allow_shell_env_restores_legacy_flag(
    clean_env: None,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    build: Callable[..., list[str]],
) -> None:
    """VAL-SEC-020: ``WHILLY_AGENT_ALLOW_SHELL=1`` returns the legacy flag and
    drops the denylist enforcement.
    """
    monkeypatch.setenv("WHILLY_AGENT_ALLOW_SHELL", "1")
    cmd = build()
    assert "--dangerously-skip-permissions" in cmd, (
        f"{name}: legacy flag missing under WHILLY_AGENT_ALLOW_SHELL=1: {cmd!r}"
    )
    assert "--disallowedTools" not in cmd, f"{name}: denylist still enforced under WHILLY_AGENT_ALLOW_SHELL=1: {cmd!r}"


@pytest.mark.parametrize(("name", "build"), _DISPATCH_PATHS, ids=[n for n, _ in _DISPATCH_PATHS])
def test_claude_safe_stacks_on_default_deny(
    clean_env: None,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    build: Callable[..., list[str]],
) -> None:
    """VAL-SEC-022: ``WHILLY_CLAUDE_SAFE=1`` adds ``--permission-mode acceptEdits``
    and stacks on top of the new default-deny denylist.
    """
    monkeypatch.setenv("WHILLY_CLAUDE_SAFE", "1")
    cmd = build()
    assert "--permission-mode" in cmd, f"{name}: missing --permission-mode: {cmd!r}"
    assert cmd[cmd.index("--permission-mode") + 1] == "acceptEdits"
    value = _disallowed_value(cmd)
    tools = {tool.strip() for tool in value.split(",") if tool.strip()}
    for required in _DENYLIST_TOOLS:
        assert required in tools, f"{name}: {required!r} missing under SAFE+default-deny: {value!r}"
    assert "--dangerously-skip-permissions" not in cmd


def test_dispatch_paths_share_default_permission_flags(clean_env: None) -> None:
    """VAL-SEC-021: synchronous and async dispatch paths produce equivalent
    permission flags for the same env (default-deny posture)."""
    sync_cmd = _sync_build("p")
    async_cmd = _async_build("p")

    sync_value = _disallowed_value(sync_cmd)
    async_value = _disallowed_value(async_cmd)
    assert {t.strip() for t in sync_value.split(",")} == {t.strip() for t in async_value.split(",")}
    assert "--dangerously-skip-permissions" not in sync_cmd
    assert "--dangerously-skip-permissions" not in async_cmd


def test_dispatch_paths_share_allow_shell_flags(
    clean_env: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """VAL-SEC-021 (legacy axis): both paths agree under WHILLY_AGENT_ALLOW_SHELL=1."""
    monkeypatch.setenv("WHILLY_AGENT_ALLOW_SHELL", "1")
    sync_cmd = _sync_build("p")
    async_cmd = _async_build("p")
    assert "--dangerously-skip-permissions" in sync_cmd
    assert "--dangerously-skip-permissions" in async_cmd
    assert "--disallowedTools" not in sync_cmd
    assert "--disallowedTools" not in async_cmd
