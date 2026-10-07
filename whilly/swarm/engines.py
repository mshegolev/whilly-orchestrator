"""Engine-specific argv and structured output adapters."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Sequence

from whilly.swarm.registry import EngineConfig


@dataclass(frozen=True)
class EngineUsage:
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cost_usd: float | None = None


@dataclass(frozen=True)
class EngineResult:
    output: str
    usage: EngineUsage
    error: str | None = None


def build_engine_argv(
    engine: str,
    config: EngineConfig,
    *,
    mode: str,
    model: str | None = None,
    max_turns: int | None = None,
    budget_usd: float | None = None,
    empty_mcp_config: str | None = None,
    add_dirs: Sequence[str] = (),
    auth_mode: str | None = None,
) -> list[str]:
    """Build argv without shell parsing; Codex is deliberately never given bypass flags."""
    selected = model or config.model
    if engine == "claude":
        argv = [*config.executable, "-p", "--output-format", "json"]
        if selected:
            argv += ["--model", selected]
        tools = "Read,Grep,Glob" if mode == "read_only" else "Read,Grep,Glob,Bash,Edit,Write"
        argv += ["--tools", tools]
        argv += list(config.worker_args)
        if mode == "read_only" or not config.inherit_mcp:
            argv += ["--setting-sources", ""]
            if empty_mcp_config:
                argv += ["--strict-mcp-config", "--mcp-config", empty_mcp_config]
        if max_turns is not None:
            argv += ["--max-turns", str(max_turns)]
        if budget_usd is not None:
            argv += ["--max-budget-usd", f"{budget_usd:.2f}"]
        if mode == "worker":
            argv += ["--permission-mode", "acceptEdits"]
            for directory in add_dirs:
                argv += ["--add-dir", directory]
        return argv
    if engine == "codex":
        argv = [
            *config.executable,
            "exec",
            "--json",
            "--ephemeral",
            "--ignore-user-config",
            "--skip-git-repo-check",
            "-c",
            f'model_reasoning_effort="{config.reasoning_effort or "low"}"',
            "--sandbox",
            # Codex's own seatbelt cannot nest inside Whilly's sandbox-exec: with
            # workspace-write every file write is denied. Whilly's outer sandbox
            # already bounds read/write roots and network, so the worker runs with
            # codex's inner sandbox off. Approval bypass flags are still never set.
            "read-only" if mode == "read_only" else "danger-full-access",
            "-",
        ]
        if auth_mode == "codex_subscription_file":
            argv[argv.index("-") : argv.index("-")] = [
                "-c",
                'forced_login="chatgpt"',
                "-c",
                'cli_auth_credentials_store="file"',
            ]
        elif auth_mode not in {None, "api_key"}:
            raise ValueError(f"unsupported codex auth mode: {auth_mode!r}")
        if selected:
            argv += ["--model", selected]
        for directory in add_dirs:
            argv += ["--add-dir", directory]
        return argv
    raise ValueError(f"unknown engine {engine!r}")


def parse_engine_output(engine: str, text: str, *, exit_code: int = 0) -> EngineResult:
    if engine == "claude":
        try:
            data = json.loads(text)
            if isinstance(data, list):
                data = next((x for x in reversed(data) if isinstance(x, dict) and x.get("type") == "result"), {})
            usage = data.get("usage", {}) if isinstance(data, dict) else {}
            output = str(data.get("result", ""))
            error = data.get("error") or data.get("is_error")
            if error:
                error = str(data.get("error") or "Claude reported an error")
            elif not output.strip():
                error = "no final Claude message"
            elif exit_code != 0:
                error = f"exit code {exit_code}"
            return EngineResult(
                output,
                EngineUsage(
                    usage.get("input_tokens"),
                    usage.get("output_tokens"),
                    usage.get("cache_read_input_tokens"),
                    data.get("total_cost_usd"),
                ),
                error,
            )
        except (ValueError, AttributeError) as exc:
            return EngineResult(text, EngineUsage(), f"malformed Claude JSON: {exc}")
    if engine != "codex":
        raise ValueError(f"unknown engine {engine!r}")
    final = ""
    usage = EngineUsage()
    error = None
    try:
        for line in text.splitlines():
            event = json.loads(line)
            if event.get("type") == "item.completed" and event.get("item", {}).get("type") == "agent_message":
                final = str(event["item"].get("text", ""))
            if event.get("type") == "turn.completed":
                u = event.get("usage", {})
                usage = EngineUsage(u.get("input_tokens"), u.get("output_tokens"), u.get("cached_input_tokens"), None)
            if event.get("type") in {"turn.failed", "error"}:
                error = str(event.get("error") or event.get("message") or "Codex turn failed")
    except (ValueError, TypeError) as exc:
        error = f"malformed Codex JSONL: {exc}"
    if not final and error is None:
        error = "no final Codex agent message"
    if exit_code != 0:
        error = error or f"exit code {exit_code}"
    return EngineResult(final, usage, error)
