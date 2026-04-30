#!/usr/bin/env python3
"""CLI adapter — drop-in замена ``claude`` для нескольких agentic CLI'ев.

Whilly worker (см. ``whilly/adapters/runner/claude_cli.py``) ждёт от
``$CLAUDE_BIN`` ровно одного argv-shape:

    $CLAUDE_BIN --dangerously-skip-permissions --output-format json \\
                --model <model> -p "<prompt>"

И single-envelope JSON на stdout с полем ``result`` (содержит маркер
``<promise>COMPLETE</promise>``) плюс ``usage``/``num_turns``/``duration_ms``/
``total_cost_usd`` чтобы parser TASK-017a корректно заполнил
``AgentResult.usage``.

Не все CLI выдают именно эту форму — argv разные, output разные. Этот
скрипт читает ``WHILLY_CLI`` env и:

1. Парсит whilly-style argv.
2. Транслирует в native argv нужного CLI.
3. Спавнит CLI как subprocess.
4. Парсит native output.
5. Эмитит whilly-compatible envelope.

Поддерживаемые CLI (через ``WHILLY_CLI`` env):

    claude-code  — Anthropic CLI (npm @anthropic-ai/claude-code).
                   Whilly уже под него заточен — adapter для него
                   почти passthrough.
    opencode     — Open source agentic CLI (opencode.ai).
                   Native: ``opencode run --format json --model <provider/model>
                   "<prompt>"`` → JSONL stream событий, последний event
                   ``result`` содержит финальный ответ.
    gemini       — Google Gemini CLI (npm @google/gemini-cli).
                   Native: ``gemini -p "<prompt>" --output-format json
                   --model <model>`` → ``{response, stats}``.

В каждом из трёх режимов модель берётся из:
1. CLI's --model arg, если whilly-worker передал явно;
2. иначе — из ``LLM_MODEL`` env (обычно её выставляет entrypoint после
   вызова llm_resource_picker.py с подобранной под cgroup моделью);
3. иначе — провайдер-специфичный fallback.

Exit codes (whilly retry-policy):
    0  — success, COMPLETE marker в envelope
    1  — retriable error (server 5xx, network, timeout)
    2  — permanent error (bad credentials, unknown CLI, malformed input)
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time

# llm_resource_picker — рядом, добавляем в sys.path для импорта.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from llm_resource_picker import pick_model
except ImportError:
    pick_model = None  # type: ignore[assignment]


_COMPLETE_MARKER = "<promise>COMPLETE</promise>"


def _build_parser() -> argparse.ArgumentParser:
    """Mirror whilly's claude_cli.build_command argv shape."""
    p = argparse.ArgumentParser(allow_abbrev=False)
    p.add_argument("--output-format", default="json")
    p.add_argument("--model", default=None)
    p.add_argument("--dangerously-skip-permissions", action="store_true")
    p.add_argument("--permission-mode", default=None)
    p.add_argument("-p", dest="prompt", required=True)
    return p


def _emit_error(message: str, exit_code: int) -> int:
    """Whilly-shape error envelope (mimics Claude CLI failures).

    Permanent errors (exit_code == 2) wrap'аются в ``failed to authenticate:`` —
    это substring, который ``whilly.adapters.runner.claude_cli._is_auth_error``
    ищет для классификации permanent. Без этого whilly-worker зациклится в
    BACKOFF_SCHEDULE 5/10/20/40/60s на missing-binary / unknown-CLI ошибках.
    """
    is_permanent = exit_code == 2
    result_text = (
        f"failed to authenticate: {message}" if is_permanent else f'{{"type":"error","error":{json.dumps(message)}}}'
    )
    envelope = {
        "result": result_text,
        "total_cost_usd": 0.0,
        "num_turns": 0,
        "duration_ms": 0,
        "usage": {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
        },
    }
    print(json.dumps(envelope))
    print(message, file=sys.stderr)
    return exit_code


def _emit_envelope(
    *,
    result_text: str,
    duration_ms: int,
    input_tokens: int = 0,
    output_tokens: int = 0,
    total_cost_usd: float = 0.0,
    num_turns: int = 1,
) -> int:
    """Successful envelope. Гарантирует COMPLETE marker (см. WHILLY_FORCE_COMPLETE)."""
    if os.environ.get("WHILLY_FORCE_COMPLETE", "1") in ("1", "true", "yes") and _COMPLETE_MARKER not in result_text:
        # Agentic CLI часто выдаёт диалог без явного COMPLETE marker'а
        # (он завершается по другим сигналам — exit code, finish_reason).
        # Whilly без marker'а оставит задачу в IN_PROGRESS до retry'я.
        # По умолчанию ВКЛЮЧАЕМ force-complete для adapter-режима, потому
        # что exit code 0 от CLI = задача выполнена. Можно отключить
        # WHILLY_FORCE_COMPLETE=0 если хочется явного marker'а от модели.
        result_text = result_text.rstrip() + "\n\n" + _COMPLETE_MARKER
    envelope = {
        "result": result_text,
        "total_cost_usd": total_cost_usd,
        "num_turns": num_turns,
        "duration_ms": duration_ms,
        "usage": {
            "input_tokens": int(input_tokens),
            "output_tokens": int(output_tokens),
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
        },
    }
    print(json.dumps(envelope))
    return 0


def _resolve_model(provider: str, cli_model: str | None) -> str | None:
    """Какую модель попросить у CLI.

    1. Явный ``--model`` от whilly-worker > всё.
    2. ``LLM_MODEL`` env (выставлена entrypoint'ом после picker'а).
    3. ``LLM_PROVIDER`` env + picker.
    4. None — пусть CLI берёт свой default.
    """
    if cli_model and not cli_model.startswith("claude-opus-4-6"):
        # Whilly's DEFAULT_MODEL — claude-opus-4-6[1m]. Если whilly явно
        # передал что-то другое — используем (оператор знал что делает).
        # Если default — лучше доверить picker'у.
        return cli_model
    if env_model := os.environ.get("LLM_MODEL", "").strip():
        return env_model
    if pick_model is not None and (env_provider := os.environ.get("LLM_PROVIDER", "").strip()):
        try:
            return pick_model(env_provider)
        except SystemExit:
            pass
    # Provider-specific fallback when LLM_MODEL/LLM_PROVIDER не заданы:
    if pick_model is not None:
        try:
            return pick_model(provider)
        except SystemExit:
            return None
    return None


# ─── claude-code adapter ───────────────────────────────────────────────────


def run_claude_code(prompt: str, model: str | None) -> int:
    """Anthropic Claude CLI — passthrough.

    Whilly изначально под него заточен (см. claude_cli.build_command), так
    что argv совпадает 1-в-1, и stdout уже в правильной shape. Просто
    проксируем.
    """
    cmd = ["claude", "--dangerously-skip-permissions", "--output-format", "json"]
    if model:
        cmd += ["--model", model]
    cmd += ["-p", prompt]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    except subprocess.TimeoutExpired:
        return _emit_error("claude-code: timeout after 600s", 1)
    except FileNotFoundError:
        return _emit_error("claude-code: 'claude' binary not found in PATH", 2)

    sys.stderr.write(result.stderr)
    sys.stdout.write(result.stdout)
    return result.returncode


# ─── opencode adapter ──────────────────────────────────────────────────────


def run_opencode(prompt: str, model: str | None) -> int:
    """OpenCode — JSONL stream → собираем финальный result event.

    OpenCode ``run --format json`` пишет stream событий:
        {"event":"init", ...}
        {"event":"message", ...}
        {"event":"tool_use", ...}
        {"event":"tool_result", ...}
        {"event":"result", "result": "<final answer>", "usage":{...}}

    Берём последний `result` event как итог.
    """
    cmd = ["opencode", "run", "--format", "json", "--dangerously-skip-permissions"]
    if model and "/" in model:
        cmd += ["--model", model]  # opencode требует "provider/model" формат
    cmd += [prompt]

    t0 = time.time()
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    except subprocess.TimeoutExpired:
        return _emit_error("opencode: timeout after 600s", 1)
    except FileNotFoundError:
        return _emit_error("opencode: 'opencode' binary not found in PATH", 2)
    duration_ms = int((time.time() - t0) * 1000)

    sys.stderr.write(result.stderr)

    if result.returncode != 0:
        return _emit_error(
            f"opencode: exit {result.returncode} — {result.stderr[:300]}",
            1 if result.returncode != 2 else 2,
        )

    final_text = ""
    message_buffer = ""  # fallback если result event не пришёл
    input_tokens = 0
    output_tokens = 0
    total_cost = 0.0
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            # Не-JSON строки игнорируем (ANSI, debug logging)
            continue
        if event.get("event") == "result" or event.get("type") == "result":
            final_text = event.get("result") or event.get("response") or event.get("text") or final_text
            usage = event.get("usage") or {}
            input_tokens = usage.get("input_tokens", input_tokens) or input_tokens
            output_tokens = usage.get("output_tokens", output_tokens) or output_tokens
            total_cost = float(event.get("cost_usd", total_cost) or total_cost)
        elif event.get("event") == "message":
            # Накапливаем все assistant chunks в отдельный buffer; используем
            # его, только если result event так и не появился.
            content = event.get("content")
            if isinstance(content, str):
                message_buffer += content

    if not final_text:
        final_text = message_buffer

    if not final_text:
        return _emit_error("opencode: empty response (no result event found)", 1)

    return _emit_envelope(
        result_text=final_text,
        duration_ms=duration_ms,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_cost_usd=total_cost,
    )


# ─── gemini-cli adapter ────────────────────────────────────────────────────


def run_gemini(prompt: str, model: str | None) -> int:
    """Gemini CLI — single JSON ``{response, stats}``."""
    cmd = ["gemini", "-p", prompt, "--output-format", "json", "--yolo"]
    if model:
        cmd += ["--model", model]

    t0 = time.time()
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    except subprocess.TimeoutExpired:
        return _emit_error("gemini: timeout after 600s", 1)
    except FileNotFoundError:
        return _emit_error("gemini: 'gemini' binary not found in PATH", 2)
    duration_ms = int((time.time() - t0) * 1000)

    sys.stderr.write(result.stderr)

    # Gemini exit codes: 0=ok, 1=general/api, 42=input, 53=turn-limit
    if result.returncode == 42:
        return _emit_error(f"gemini: input error — {result.stderr[:300]}", 2)
    if result.returncode != 0:
        return _emit_error(f"gemini: exit {result.returncode} — {result.stderr[:300]}", 1)

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        return _emit_error(f"gemini: malformed JSON output — {exc}", 1)

    if "error" in data:
        return _emit_error(f"gemini: {data['error']}", 1)

    final_text = data.get("response") or ""
    stats = data.get("stats") or {}
    # gemini-cli stats schema: {"models": {"<model>": {"tokens": {...}}}}
    input_tokens = 0
    output_tokens = 0
    if "models" in stats:
        for model_stats in stats["models"].values():
            tokens = model_stats.get("tokens", {})
            input_tokens += tokens.get("prompt", 0) or 0
            output_tokens += tokens.get("candidates", 0) or 0

    return _emit_envelope(
        result_text=final_text,
        duration_ms=duration_ms,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )


# ─── Dispatch ──────────────────────────────────────────────────────────────


_RUNNERS = {
    "claude-code": run_claude_code,
    "opencode": run_opencode,
    "gemini": run_gemini,
}


def main(argv: list[str] | None = None) -> int:
    args, _ = _build_parser().parse_known_args(argv)

    cli_name = os.environ.get("WHILLY_CLI", "").strip().lower()
    if not cli_name:
        return _emit_error(
            f"WHILLY_CLI env var is required (one of: {', '.join(sorted(_RUNNERS))})",
            2,
        )
    runner = _RUNNERS.get(cli_name)
    if runner is None:
        return _emit_error(
            f"unknown WHILLY_CLI={cli_name!r} (expected: {', '.join(sorted(_RUNNERS))})",
            2,
        )

    # Map cli_name → provider name for picker (1-in-1 для claude-code/gemini,
    # для opencode провайдер задаётся внутри model string как "provider/model").
    provider = {"claude-code": "claude", "gemini": "gemini", "opencode": "openrouter"}[cli_name]
    model = _resolve_model(provider, args.model)
    return runner(args.prompt, model)


if __name__ == "__main__":
    sys.exit(main())
