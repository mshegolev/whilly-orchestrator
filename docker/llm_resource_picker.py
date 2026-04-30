#!/usr/bin/env python3
"""LLM resource picker — выбирает модель под доступные контейнеру ресурсы.

Идея: оператор указывает только провайдера (``LLM_PROVIDER=groq``), а shim
сам подбирает конкретную модель под cgroup-лимиты контейнера. На больших
машинах включаются 70B-модели, на маленьких — 7-8B, на совсем тонких
ноутбуках/CI-runner'ах — самые лёгкие. Для облачных провайдеров (Groq,
OpenRouter, Cerebras, Gemini) логика чуть искусственная — модель всё
равно крутится на сервере провайдера, но более жирная модель потребляет
больше токенов из free-tier rate-limit'а; для **локальной Ollama** это
вопрос «вообще запустится или OOM».

Detection: предпочитаем cgroup v2 (modern Linux, Docker Desktop, K8s),
с fallback'ом на cgroup v1 и в самом крайнем случае на хост-уровневый
``/proc/meminfo`` + ``os.cpu_count()``. На macOS/Windows контейнер всегда
бежит внутри Linux VM (Docker Desktop / Colima), так что cgroup-чтения
будут работать даже когда пишут «контейнер на маке».

Использование:

    # Stand-alone — печатает выбранную модель в stdout, провайдер в argv
    $ python llm_resource_picker.py groq
    llama-3.3-70b-versatile

    # Из Python
    >>> from llm_resource_picker import pick_model, detect_tier
    >>> detect_tier()
    SizeTier.MEDIUM
    >>> pick_model("groq")
    'llama-3.3-70b-versatile'

Опционально через env:
    LLM_TIER_OVERRIDE=tiny|small|medium|large — пропускает auto-detect
    LLM_MODEL=...                              — полностью обходит picker
"""

from __future__ import annotations

import enum
import os
import sys
from pathlib import Path


class SizeTier(str, enum.Enum):
    """Грубая категоризация (память — главное, CPU — вторично)."""

    TINY = "tiny"  # <4GB RAM или <2 CPU — only самые мелкие модели
    SMALL = "small"  # 4-8GB, 2-4 CPU — 7-8B модели
    MEDIUM = "medium"  # 8-16GB, 4-8 CPU — 14-32B или 70B на быстрых API
    LARGE = "large"  # 16+GB, 8+ CPU — топовые модели

    @property
    def order(self) -> int:
        return {"tiny": 0, "small": 1, "medium": 2, "large": 3}[self.value]


# Provider → tier → model. Источники моделей актуальны на 2026-04 — если
# провайдер переименует/деприкейтит, замените и пушните PR. Все модели
# здесь либо free-tier, либо полностью бесплатные (см. DEMO.md).
PROVIDER_MODEL_MAP: dict[str, dict[SizeTier, str]] = {
    "groq": {
        SizeTier.TINY: "llama-3.1-8b-instant",
        SizeTier.SMALL: "llama-3.1-8b-instant",
        SizeTier.MEDIUM: "llama-3.3-70b-versatile",
        SizeTier.LARGE: "llama-3.3-70b-versatile",
    },
    "openrouter": {
        SizeTier.TINY: "meta-llama/llama-3.2-3b-instruct:free",
        SizeTier.SMALL: "meta-llama/llama-3.1-8b-instruct:free",
        SizeTier.MEDIUM: "meta-llama/llama-3.3-70b-instruct:free",
        SizeTier.LARGE: "deepseek/deepseek-chat-v3.1:free",
    },
    "cerebras": {
        SizeTier.TINY: "llama-3.1-8b",
        SizeTier.SMALL: "llama-3.1-8b",
        SizeTier.MEDIUM: "llama-3.3-70b",
        SizeTier.LARGE: "llama-3.3-70b",
    },
    "gemini": {
        SizeTier.TINY: "gemini-2.0-flash-lite",
        SizeTier.SMALL: "gemini-2.0-flash-lite",
        SizeTier.MEDIUM: "gemini-2.0-flash-exp",
        SizeTier.LARGE: "gemini-2.0-flash-exp",
    },
    # OpenAI Codex CLI — gpt-5.x семейство. По умолчанию для API-key auth,
    # gpt-5.5 требует ChatGPT auth и поэтому не используется (он lock'нут на
    # ChatGPT Plus/Pro), gpt-5.4 — флагман для API. mini — fast/cheap,
    # codex — coding-specialized snapshot. Источник: developers.openai.com/
    # codex/models (2026-04).
    "openai": {
        SizeTier.TINY: "gpt-5.4-mini",
        SizeTier.SMALL: "gpt-5.4-mini",
        SizeTier.MEDIUM: "gpt-5.4",
        SizeTier.LARGE: "gpt-5.4",
    },
    # Локальная Ollama — критично подбирать под RAM, иначе OOM.
    # Числа в имени = миллиарды параметров; квантованные q4 ≈ N×0.55 GB.
    "ollama": {
        SizeTier.TINY: "qwen2.5-coder:1.5b",
        SizeTier.SMALL: "qwen2.5-coder:7b",
        SizeTier.MEDIUM: "qwen2.5-coder:14b",
        SizeTier.LARGE: "qwen2.5-coder:32b",
    },
    # Anthropic Claude (если оператор включил Path 1 с реальным claude
    # бинарём) — все модели на сервере Anthropic, но цена/скорость зависит
    # от модели. На слабом железе нет смысла платить за Opus.
    "claude": {
        SizeTier.TINY: "claude-haiku-4-5",
        SizeTier.SMALL: "claude-haiku-4-5",
        SizeTier.MEDIUM: "claude-sonnet-4-5",
        SizeTier.LARGE: "claude-opus-4-5",
    },
}

# Пороги в байтах (память) и cores (CPU). Hand-tuned под распространённые
# дев-машины и CI-runner'ы. Если резкие границы создают «дребезг» — можно
# вручную задать LLM_TIER_OVERRIDE.
_GB = 1024**3
_MEM_THRESHOLDS = {
    SizeTier.TINY: 4 * _GB,  # <4GB → TINY
    SizeTier.SMALL: 8 * _GB,  # 4-8GB → SMALL
    SizeTier.MEDIUM: 16 * _GB,  # 8-16GB → MEDIUM
    # ≥16GB → LARGE
}
_CPU_THRESHOLDS = {
    SizeTier.TINY: 2,
    SizeTier.SMALL: 4,
    SizeTier.MEDIUM: 8,
}


def _read_int(path: Path) -> int | None:
    """Читаем число из cgroup-файла. ``max`` (cgroup v2) → None — лимита нет.

    cgroup-файлы маленькие (один-два числа на строку), errno-ошибки
    ловим тихо — picker должен **никогда** не падать; в худшем случае
    fallback в TINY (безопасно, всегда влезает).
    """
    try:
        text = path.read_text().strip()
    except OSError:
        return None
    if not text or text == "max":
        return None
    # cgroup v2 cpu.max — две цифры через пробел: "100000 100000" (quota period)
    first = text.split()[0]
    try:
        return int(first)
    except ValueError:
        return None


def detect_memory_bytes() -> int:
    """Вернуть байты доступной контейнеру памяти.

    Порядок:
    1. cgroup v2: ``/sys/fs/cgroup/memory.max``
    2. cgroup v1: ``/sys/fs/cgroup/memory/memory.limit_in_bytes``
    3. Хост: ``MemTotal`` из ``/proc/meminfo``
    4. Defensive: 1 GB (TINY tier)

    Cgroup v1 на безлимитном контейнере выдаёт огромное «64-битное» число
    (``9223372036854771712`` или близко) — это значит «лимита нет», берём
    хост-память.
    """
    # cgroup v2
    v2 = _read_int(Path("/sys/fs/cgroup/memory.max"))
    if v2 is not None and v2 > 0:
        return v2

    # cgroup v1
    v1 = _read_int(Path("/sys/fs/cgroup/memory/memory.limit_in_bytes"))
    # Эвристика «безлимит»: значение >= 1 PB → лимит не задан
    if v1 is not None and 0 < v1 < 1024**5:
        return v1

    # Хост MemTotal
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemTotal:"):
                kb = int(line.split()[1])
                return kb * 1024
    except (OSError, ValueError, IndexError):
        pass

    return 1 * _GB


def detect_cpu_count() -> int:
    """Вернуть число CPU, которое cgroup разрешает контейнеру.

    cgroup v2 ``cpu.max`` имеет шейп ``"<quota> <period>"`` — эффективное
    число CPU = quota / period (round up). cgroup v1 — два файла
    ``cpu.cfs_quota_us`` / ``cpu.cfs_period_us``. Fallback —
    ``os.cpu_count()`` (он сам cgroup-aware на современных Linux).
    """
    # cgroup v2: cpu.max — "<quota> <period>" or "max <period>"
    try:
        text = Path("/sys/fs/cgroup/cpu.max").read_text().strip()
        parts = text.split()
        if len(parts) == 2 and parts[0] != "max":
            quota = int(parts[0])
            period = int(parts[1])
            if quota > 0 and period > 0:
                return max(1, (quota + period - 1) // period)
    except (OSError, ValueError):
        pass

    # cgroup v1
    try:
        quota = int(Path("/sys/fs/cgroup/cpu/cpu.cfs_quota_us").read_text().strip())
        period = int(Path("/sys/fs/cgroup/cpu/cpu.cfs_period_us").read_text().strip())
        if quota > 0 and period > 0:
            return max(1, (quota + period - 1) // period)
    except (OSError, ValueError):
        pass

    return os.cpu_count() or 1


def detect_tier(
    *,
    memory_bytes: int | None = None,
    cpu_count: int | None = None,
) -> SizeTier:
    """Определить SizeTier из (mem, cpu). Параметры — для тестируемости.

    Логика: берём min из «mem-tier» и «cpu-tier» — слабое место решает.
    Так не получим сюрприза «96 cores но 2GB RAM → попытались LLama-70B».
    """
    if mem_override := os.environ.get("LLM_TIER_OVERRIDE", "").strip().lower():
        try:
            return SizeTier(mem_override)
        except ValueError:
            print(
                f"warning: invalid LLM_TIER_OVERRIDE={mem_override!r} — "
                "expected one of tiny/small/medium/large; auto-detecting",
                file=sys.stderr,
            )

    mem = detect_memory_bytes() if memory_bytes is None else memory_bytes
    cpu = detect_cpu_count() if cpu_count is None else cpu_count

    if mem < _MEM_THRESHOLDS[SizeTier.TINY]:
        mem_tier = SizeTier.TINY
    elif mem < _MEM_THRESHOLDS[SizeTier.SMALL]:
        mem_tier = SizeTier.SMALL
    elif mem < _MEM_THRESHOLDS[SizeTier.MEDIUM]:
        mem_tier = SizeTier.MEDIUM
    else:
        mem_tier = SizeTier.LARGE

    if cpu < _CPU_THRESHOLDS[SizeTier.TINY]:
        cpu_tier = SizeTier.TINY
    elif cpu < _CPU_THRESHOLDS[SizeTier.SMALL]:
        cpu_tier = SizeTier.SMALL
    elif cpu < _CPU_THRESHOLDS[SizeTier.MEDIUM]:
        cpu_tier = SizeTier.MEDIUM
    else:
        cpu_tier = SizeTier.LARGE

    return mem_tier if mem_tier.order <= cpu_tier.order else cpu_tier


def pick_model(provider: str, *, tier: SizeTier | None = None) -> str:
    """Подобрать модель для провайдера. Бросает SystemExit если provider unknown.

    Если ``LLM_MODEL`` уже выставлена в env — обходим логику и возвращаем её
    как есть; это даёт оператору escape-hatch (например, для своих
    fine-tuned моделей или превью-моделей не из карты).
    """
    if model := os.environ.get("LLM_MODEL", "").strip():
        return model

    provider_norm = provider.strip().lower()
    if provider_norm not in PROVIDER_MODEL_MAP:
        known = ", ".join(sorted(PROVIDER_MODEL_MAP))
        raise SystemExit(f"unknown LLM_PROVIDER={provider!r}; known providers: {known}")

    actual_tier = tier or detect_tier()
    return PROVIDER_MODEL_MAP[provider_norm][actual_tier]


def describe(provider: str) -> str:
    """Diagnostic string — что задетектили и какую модель выбрали."""
    mem_gb = detect_memory_bytes() / _GB
    cpu = detect_cpu_count()
    tier = detect_tier()
    model = pick_model(provider, tier=tier)
    return f"resource-picker: provider={provider} mem={mem_gb:.1f}GB cpu={cpu} tier={tier.value} model={model}"


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if not args or args[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    provider = args[0]
    verbose = "--verbose" in args[1:] or "-v" in args[1:]
    if verbose:
        print(describe(provider), file=sys.stderr)
    try:
        print(pick_model(provider))
        return 0
    except SystemExit as exc:
        # SystemExit from pick_model carries the human message in args[0]
        message = exc.args[0] if exc.args else str(exc)
        print(message, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
