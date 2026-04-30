"""Unit tests for ``docker/llm_resource_picker.py``.

Picker — это standalone-скрипт для demo-контейнера, который выбирает LLM
под cgroup-лимиты. Мы импортируем его как модуль через ``importlib`` (а не
через ``import docker.llm_resource_picker``), потому что ``docker/`` — это
не Python-пакет (нет ``__init__.py``); это просто директория с docker-
ассетами, которые COPY'ятся в образ.

Покрываем:

* Tier-decision: пороги по памяти и CPU работают ровно как задокументированы.
* Min-bottleneck: 96 cores но 2GB RAM → tier берётся по памяти (TINY).
* LLM_TIER_OVERRIDE: ручное выставление tier пропускает auto-detect.
* LLM_MODEL: явный override полностью обходит picker.
* Provider map: каждый известный провайдер выдаёт модель для каждого tier.
* Unknown provider: SystemExit с понятным сообщением.
* CLI: ``python llm_resource_picker.py <provider>`` печатает модель в stdout
  и кодами выхода сигналит ошибки.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


@pytest.fixture(scope="module")
def picker():
    """Загрузить llm_resource_picker.py как модуль.

    Используем importlib потому что docker/ — не Python пакет; добавлять
    туда __init__.py именно ради тестов нежелательно (это испортит
    Dockerfile.demo COPY-логику и захламит layer'ы). importlib.spec
    читает файл напрямую и регистрирует модуль в sys.modules под именем
    ``llm_resource_picker`` — что совпадает с импортом из shim'а.
    """
    repo_root = Path(__file__).resolve().parents[2]
    path = repo_root / "docker" / "llm_resource_picker.py"
    spec = importlib.util.spec_from_file_location("llm_resource_picker", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_GB = 1024**3


class TestDetectTier:
    """Пороги: <4GB tiny, 4-8 small, 8-16 medium, ≥16 large.

    Те же пороги по CPU: <2 tiny, 2-4 small, 4-8 medium, ≥8 large. Конечный
    tier = min из mem-tier и cpu-tier (слабое звено решает).
    """

    @pytest.mark.parametrize(
        ("mem_gb", "cpu", "expected"),
        [
            # Чистые границы по памяти при щедром CPU
            (2, 16, "tiny"),
            (4, 16, "small"),
            (7.99, 16, "small"),
            (8, 16, "medium"),
            (15, 16, "medium"),
            (16, 16, "large"),
            (64, 16, "large"),
            # Чистые границы по CPU при щедрой памяти
            (32, 1, "tiny"),
            (32, 2, "small"),
            (32, 3, "small"),
            (32, 4, "medium"),
            (32, 7, "medium"),
            (32, 8, "large"),
            # Min-bottleneck: «96 cores но 2GB RAM» → ограничен памятью
            (2, 96, "tiny"),
            (16, 1, "tiny"),
            # Edge: 8GB / 2 cores → memory says medium, cpu says small → small
            (8, 2, "small"),
        ],
    )
    def test_thresholds(self, picker, mem_gb, cpu, expected):
        tier = picker.detect_tier(memory_bytes=int(mem_gb * _GB), cpu_count=cpu)
        assert tier.value == expected

    def test_override_via_env(self, picker, monkeypatch):
        monkeypatch.setenv("LLM_TIER_OVERRIDE", "large")
        # Память и CPU выставлены явно «нищие», но override побеждает
        tier = picker.detect_tier(memory_bytes=1 * _GB, cpu_count=1)
        assert tier.value == "large"

    def test_override_invalid_falls_back_to_autodetect(self, picker, monkeypatch, capsys):
        monkeypatch.setenv("LLM_TIER_OVERRIDE", "garbage")
        tier = picker.detect_tier(memory_bytes=1 * _GB, cpu_count=1)
        assert tier.value == "tiny"  # auto-detect отработал
        captured = capsys.readouterr()
        assert "invalid LLM_TIER_OVERRIDE" in captured.err


class TestPickModel:
    """Маппинг провайдер → tier → модель."""

    KNOWN = ("groq", "openrouter", "cerebras", "gemini", "ollama", "claude", "openai")

    @pytest.mark.parametrize("provider", KNOWN)
    def test_every_provider_resolves_for_every_tier(self, picker, provider):
        for tier_value in ("tiny", "small", "medium", "large"):
            tier = picker.SizeTier(tier_value)
            model = picker.pick_model(provider, tier=tier)
            assert isinstance(model, str) and model, f"{provider}/{tier_value} returned empty model"

    def test_explicit_llm_model_env_short_circuits_picker(self, picker, monkeypatch):
        monkeypatch.setenv("LLM_MODEL", "my-fine-tuned/whatever")
        # Даже unknown provider — мы должны вернуть env, не падать
        assert picker.pick_model("unknown-provider") == "my-fine-tuned/whatever"

    def test_unknown_provider_raises_systemexit(self, picker):
        with pytest.raises(SystemExit) as exc:
            picker.pick_model("not-a-provider")
        msg = exc.value.args[0]
        assert "unknown LLM_PROVIDER" in msg
        # Сообщение перечисляет известные провайдеры
        for provider in self.KNOWN:
            assert provider in msg

    def test_provider_case_insensitive(self, picker, monkeypatch):
        # Не должно зависеть от регистра — оператор может ляпнуть GROQ/Groq/groq
        monkeypatch.delenv("LLM_MODEL", raising=False)
        m1 = picker.pick_model("groq", tier=picker.SizeTier.SMALL)
        m2 = picker.pick_model("GROQ", tier=picker.SizeTier.SMALL)
        m3 = picker.pick_model("Groq", tier=picker.SizeTier.SMALL)
        assert m1 == m2 == m3

    def test_openai_codex_models(self, picker):
        """OpenAI/codex: tier→model шкала. TINY/SMALL — fast/cheap mini, MEDIUM/LARGE — flagship."""
        models = {
            tv: picker.pick_model("openai", tier=picker.SizeTier(tv)) for tv in ("tiny", "small", "medium", "large")
        }
        # Все из gpt-5.x семейства
        for tier_value, model in models.items():
            assert model.startswith("gpt-5"), f"{tier_value}={model} not gpt-5.x"
        # mini для tiny/small (быстрее/дешевле), flagship для medium/large
        assert "mini" in models["tiny"]
        assert "mini" in models["small"]
        assert "mini" not in models["medium"]
        assert "mini" not in models["large"]

    def test_ollama_models_scale_with_tier(self, picker):
        """Ollama: модели должны увеличиваться от TINY к LARGE.

        Это критично потому что для локальной inference выбор не из-за
        rate-limits, а из-за реального OOM. Если кто-то сломает порядок
        в карте — тест поймает.
        """
        sizes = []
        for tier_value in ("tiny", "small", "medium", "large"):
            tier = picker.SizeTier(tier_value)
            model = picker.pick_model("ollama", tier=tier)
            # имена вида "qwen2.5-coder:1.5b" / "...:7b" / "...:14b"
            tag = model.rsplit(":", 1)[-1].rstrip("b")
            sizes.append(float(tag))
        assert sizes == sorted(sizes), f"ollama models must grow monotonically: got {sizes}"


class TestCli:
    """``python llm_resource_picker.py <provider>`` — поведение."""

    def test_prints_model_to_stdout(self, picker, capsys, monkeypatch):
        monkeypatch.delenv("LLM_MODEL", raising=False)
        monkeypatch.setenv("LLM_TIER_OVERRIDE", "small")
        rc = picker.main(["groq"])
        assert rc == 0
        out = capsys.readouterr().out.strip()
        assert out == "llama-3.1-8b-instant"

    def test_unknown_provider_exits_2(self, picker, capsys):
        rc = picker.main(["nonexistent-provider"])
        assert rc == 2
        err = capsys.readouterr().err
        assert "unknown LLM_PROVIDER" in err

    def test_help(self, picker, capsys):
        rc = picker.main(["--help"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "LLM resource picker" in out

    def test_verbose_writes_diagnostics_to_stderr(self, picker, capsys, monkeypatch):
        monkeypatch.delenv("LLM_MODEL", raising=False)
        monkeypatch.setenv("LLM_TIER_OVERRIDE", "medium")
        rc = picker.main(["openrouter", "--verbose"])
        assert rc == 0
        out = capsys.readouterr()
        assert out.out.strip() == "meta-llama/llama-3.3-70b-instruct:free"
        assert "tier=medium" in out.err
        assert "model=meta-llama/llama-3.3-70b-instruct:free" in out.err
