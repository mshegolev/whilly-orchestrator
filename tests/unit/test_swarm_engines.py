from pathlib import Path
from types import SimpleNamespace

import pytest

from whilly.swarm.agent import run_engine
from whilly.swarm.engines import build_engine_argv, parse_engine_output
from whilly.swarm.registry import EngineConfig


def test_codex_argv_is_read_only_and_stdin_based():
    argv = build_engine_argv("codex", EngineConfig(("codex",), "gpt-5.6-luna"), mode="read_only")
    assert argv == [
        "codex",
        "exec",
        "--json",
        "--ephemeral",
        "--ignore-user-config",
        "--skip-git-repo-check",
        "-c",
        'model_reasoning_effort="low"',
        "--sandbox",
        "read-only",
        "-",
        "--model",
        "gpt-5.6-luna",
    ]


def test_codex_subscription_argv_forces_scoped_file_chatgpt_auth():
    argv = build_engine_argv(
        "codex", EngineConfig(("codex",), "gpt-5.6-luna"), mode="worker", auth_mode="codex_subscription_file"
    )
    assert "--no-daemon" not in argv
    assert 'forced_login="chatgpt"' in argv
    assert 'cli_auth_credentials_store="file"' in argv


@pytest.mark.asyncio
async def test_run_engine_forwards_subscription_auth_mode(tmp_path: Path):
    captured = []

    class Executor:
        async def run(self, argv, **kwargs):
            captured.extend(argv)
            stdout = tmp_path / "stdout.log"
            stderr = tmp_path / "stderr.log"
            stdout.write_text(
                '{"type":"item.completed","item":{"type":"agent_message","text":"ok"}}\n',
                encoding="utf-8",
            )
            stderr.write_text("", encoding="utf-8")
            return SimpleNamespace(exit_code=0, stdout_path=str(stdout), stderr_path=str(stderr))

    await run_engine(
        "codex",
        EngineConfig(("codex",), "gpt-5.6-luna"),
        mode="read_only",
        prompt="plan",
        cwd=tmp_path,
        log_dir=tmp_path / "logs",
        name="planner",
        timeout_seconds=5,
        auth_mode="codex_subscription_file",
        policy=SimpleNamespace(phase="planner"),
        executor=Executor(),
    )

    assert "--no-daemon" not in captured
    assert 'forced_login="chatgpt"' in captured


def test_codex_jsonl_extracts_final_and_unknown_cost():
    result = parse_engine_output(
        "codex",
        '{"type":"item.completed","item":{"type":"agent_message","text":"ok"}}\n{"type":"turn.completed","usage":{"input_tokens":4,"output_tokens":2}}\n',
    )
    assert result.output == "ok"
    assert result.usage.input_tokens == 4
    assert result.usage.cost_usd is None


def test_codex_error_only_is_failure():
    result = parse_engine_output("codex", '{"type":"turn.failed","error":"nope"}\n')
    assert result.error == "nope"


def test_claude_worker_is_explicitly_bounded_when_mcp_not_inherited():
    argv = build_engine_argv(
        "claude",
        EngineConfig(("ch-wrapper",), "haiku", inherit_mcp=False),
        mode="worker",
        empty_mcp_config="empty.json",
    )
    assert "Read,Grep,Glob,Bash,Edit,Write" in argv
    assert "--strict-mcp-config" in argv
    assert argv[argv.index("--setting-sources") + 1] == ""
    assert argv[argv.index("--permission-mode") + 1] == "acceptEdits"


def test_claude_error_and_missing_final_are_failures():
    assert parse_engine_output("claude", '{"is_error":true,"result":""}').error
    assert parse_engine_output("claude", '{"usage":{}}').error == "no final Claude message"


def test_codex_worker_runs_without_inner_sandbox_but_never_bypasses_approvals() -> None:
    # Codex's seatbelt cannot nest inside Whilly's sandbox-exec (writes were denied);
    # the outer Whilly sandbox bounds the worker instead.
    worker = build_engine_argv("codex", EngineConfig(("codex",), "gpt-5.6-luna"), mode="worker")
    reader = build_engine_argv("codex", EngineConfig(("codex",), "gpt-5.6-luna"), mode="read_only")

    assert worker[worker.index("--sandbox") + 1] == "danger-full-access"
    assert reader[reader.index("--sandbox") + 1] == "read-only"
    assert not any("bypass" in arg for arg in (*worker, *reader))
