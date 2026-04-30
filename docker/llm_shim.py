#!/usr/bin/env python3
"""LLM shim — drop-in замена ``claude`` CLI для любого OpenAI-compatible API.

Whilly worker (см. ``whilly/adapters/runner/claude_cli.py``) спавнит
``$CLAUDE_BIN`` с ровно таким argv:

    $CLAUDE_BIN --dangerously-skip-permissions --output-format json \\
                --model <model> -p "<prompt>"

И ждёт на stdout single-envelope JSON с полем ``result`` (внутри которого
есть маркер ``<promise>COMPLETE</promise>``), плюс ``usage`` /
``total_cost_usd`` / ``num_turns`` / ``duration_ms`` чтобы parser TASK-017a
заполнил ``AgentResult.usage`` вместо raw-text fallback.

Этот shim делает ровно то же — но вместо реального Claude дёргает любой
OpenAI-совместимый endpoint (Groq, OpenRouter, Cerebras, локальная Ollama,
Google AI Studio через openai-compat layer и т.д.).

ENV переменные (все читаются на старте):

    LLM_BASE_URL    — обязательно. Полный путь до /v1, без слэша на конце.
                      Пример: https://api.groq.com/openai/v1
    LLM_API_KEY     — обязательно. Bearer token провайдера.
    LLM_PROVIDER    — опционально. Имя провайдера (``groq``, ``openrouter``,
                      ``cerebras``, ``gemini``, ``ollama``, ``claude``).
                      Если задано И ``LLM_MODEL`` не задана — модель
                      выбирается через ``llm_resource_picker.pick_model``
                      под cgroup-лимиты текущего контейнера.
    LLM_MODEL       — опционально. Жёсткий override; если задана,
                      пропускает picker.
    LLM_TIER_OVERRIDE — опционально. Принудительно выставить tier
                      (``tiny|small|medium|large``) и пропустить cgroup
                      detection. Удобно для тестов и edge-кейсов.
    LLM_TIMEOUT     — опционально. Секунды, default 120.
    LLM_TEMPERATURE — опционально. Float, default 0 (детерминизм важнее
                      креатива для агентских задач).
    LLM_HTTP_REFERER, LLM_X_TITLE — опционально. OpenRouter их хочет
                      видеть; для остальных провайдеров не мешает.
    LLM_FORCE_COMPLETE — опционально, "1" для включения. Если модель забыла
                      выдать <promise>COMPLETE</promise>, добавит его в
                      конец. По умолчанию OFF — пусть whilly retry-логика
                      работает естественно.

Exit codes:
    0  — успех, JSON envelope в stdout
    1  — упстрим вернул HTTP-ошибку или невалидный JSON (whilly будет
         retry-ить через BACKOFF_SCHEDULE 5/10/20/40/60s)
    2  — конфигурация: LLM_BASE_URL / LLM_API_KEY не заданы (permanent)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

import httpx

# llm_resource_picker лежит рядом — добавляем директорию shim'а в sys.path
# чтобы импорт работал и при ``python /path/to/llm_shim.py``, и при
# ``CLAUDE_BIN=/opt/whilly/docker/llm_shim.py``.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from llm_resource_picker import pick_model  # type: ignore[import-not-found]
except ImportError:  # picker отсутствует — резервный путь без auto-pick
    pick_model = None  # type: ignore[assignment]

# System prompt: подсказывает модели, что мы агент, и просит обязательный
# COMPLETE marker. ``parse_output`` в whilly/adapters/runner/result_parser.py
# ищет именно этот литерал; без него задача не помечается DONE даже при
# успешном exit code.
SYSTEM_PROMPT = (
    "You are a coding agent inside a distributed task runner. "
    "Read the task description carefully, complete the work, and at the very "
    "end of your reply include the literal string <promise>COMPLETE</promise> "
    "exactly once when you are confident the task is done. "
    "If you cannot complete the task, explain the blocker but do NOT include "
    "the COMPLETE marker — the orchestrator will retry or mark the task FAILED."
)


def _build_parser() -> argparse.ArgumentParser:
    """Mirror Claude CLI argv enough to not crash on whilly's invocation.

    ``allow_abbrev=False`` чтобы ``--mod`` случайно не матчился в ``--model``
    (whilly никогда так не зовёт, но защита от перетаскивания флагов).
    Все незнакомые флаги принимаются через ``parse_known_args`` — Claude CLI
    мог добавить новые опции, мы их игнорируем без сбоя.
    """
    p = argparse.ArgumentParser(allow_abbrev=False)
    p.add_argument("--output-format", default="json")
    p.add_argument("--model", default=None)
    p.add_argument("--dangerously-skip-permissions", action="store_true")
    p.add_argument("--permission-mode", default=None)
    p.add_argument("-p", dest="prompt", required=True)
    return p


def _emit_error_envelope(message: str, exit_code: int) -> int:
    """Print error envelope to stdout AND exit with given code.

    Whilly's ``parse_output`` matches ``API Error: 5xx`` and ``"type":"error"``
    substrings to decide retriability. We mirror that shape so retry logic
    behaves as if it were talking to Claude itself.
    """
    envelope = {
        "result": f'{{"type":"error","error":{json.dumps(message)}}}',
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
    return exit_code


def main(argv: list[str] | None = None) -> int:
    args, _ = _build_parser().parse_known_args(argv)

    base_url = os.environ.get("LLM_BASE_URL", "").strip()
    api_key = os.environ.get("LLM_API_KEY", "").strip()
    if not base_url or not api_key:
        # exit 2 = config error. Whilly's retry policy treats non-zero exit
        # AND non-auth as retriable, so we emit a permanent-looking error
        # envelope ("failed to authenticate") that prevents retries.
        print("LLM_BASE_URL and LLM_API_KEY env vars are required", file=sys.stderr)
        return _emit_error_envelope("failed to authenticate: missing LLM credentials", 2)

    # Model resolution priority:
    # 1. LLM_MODEL env (explicit override от оператора)
    # 2. LLM_PROVIDER + cgroup auto-pick через llm_resource_picker
    # 3. ``--model`` из argv (то что whilly считает дефолтом)
    # 4. Жёсткий fallback на распространённую free модель
    explicit = os.environ.get("LLM_MODEL", "").strip()
    provider = os.environ.get("LLM_PROVIDER", "").strip()
    if explicit:
        model = explicit
    elif provider and pick_model is not None:
        try:
            model = pick_model(provider)
        except SystemExit as exc:
            return _emit_error_envelope(f"failed to authenticate: {exc.args[0] if exc.args else exc}", 2)
    else:
        model = args.model or "llama-3.3-70b-versatile"
    timeout = float(os.environ.get("LLM_TIMEOUT", "120"))
    try:
        temperature = float(os.environ.get("LLM_TEMPERATURE", "0"))
    except ValueError:
        temperature = 0.0

    headers: dict[str, str] = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    referer = os.environ.get("LLM_HTTP_REFERER", "").strip()
    if referer:
        headers["HTTP-Referer"] = referer
    title = os.environ.get("LLM_X_TITLE", "").strip()
    if title:
        headers["X-Title"] = title

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": args.prompt},
        ],
        "temperature": temperature,
    }

    url = base_url.rstrip("/") + "/chat/completions"
    t0 = time.time()
    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.post(url, json=payload, headers=headers)
        resp.raise_for_status()
        data = resp.json()
    except httpx.HTTPStatusError as exc:
        # Match Claude CLI shape so whilly's _is_retriable_error / _is_auth_error
        # in claude_cli.py classifies us correctly:
        #   401/403 → auth (no retry)
        #   5xx     → API Error (retry)
        status = exc.response.status_code
        body = exc.response.text[:300]
        if status in (401, 403):
            return _emit_error_envelope(f"failed to authenticate: HTTP {status} — {body}", 1)
        return _emit_error_envelope(f"API Error: {status} — {body}", 1)
    except httpx.HTTPError as exc:
        return _emit_error_envelope(f"API Error: {exc.__class__.__name__}: {exc}", 1)
    except json.JSONDecodeError as exc:
        return _emit_error_envelope(f"API Error: malformed JSON — {exc}", 1)

    duration_ms = int((time.time() - t0) * 1000)

    try:
        message = data["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, TypeError):
        return _emit_error_envelope(
            f"API Error: malformed response shape — {json.dumps(data)[:200]}",
            1,
        )

    if (
        os.environ.get("LLM_FORCE_COMPLETE", "0") in ("1", "true", "yes")
        and "<promise>COMPLETE</promise>" not in message
    ):
        message = message.rstrip() + "\n\n<promise>COMPLETE</promise>"

    usage = data.get("usage") or {}
    envelope = {
        "result": message,
        "total_cost_usd": float(usage.get("cost", 0.0)),
        "num_turns": 1,
        "duration_ms": duration_ms,
        "usage": {
            "input_tokens": int(usage.get("prompt_tokens", 0)),
            "output_tokens": int(usage.get("completion_tokens", 0)),
            "cache_read_input_tokens": int(
                usage.get("prompt_cache_hit_tokens", 0) or usage.get("cached_tokens", 0) or 0
            ),
            "cache_creation_input_tokens": 0,
        },
    }
    print(json.dumps(envelope))
    return 0


if __name__ == "__main__":
    sys.exit(main())
