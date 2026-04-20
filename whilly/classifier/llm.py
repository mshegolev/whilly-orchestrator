"""LLM-backed classifier — primary path.

Uses the active agent backend (Claude / OpenCode via
:func:`whilly.agents.active_backend_from_env`) and a structured JSON
prompt. Falls back to :class:`~whilly.classifier.heuristic.HeuristicClassifier`
on any LLM error — the caller never gets a crash, it gets a low-confidence
result with a flag explaining the fallback.

Prompt is intentionally tight:

* expects strict JSON output (classification fails, fallback triggers),
* includes the 3-level definition inline (no prior-context prompt),
* asks for explicit complexity 1–10 and estimated children count,
* lets the model surface flags ("duplicate-suspected", "underspecified",
  "out-of-scope") — these drive human-review gates downstream.

Costs: one LLM call per classification (~1-2k input tokens, ~500 output).
At haiku pricing this is pennies per issue — an order of magnitude
cheaper than the TRIZ challenge stage downstream.
"""

from __future__ import annotations

import json
import logging
import re

from whilly.agents import active_backend_from_env
from whilly.classifier.base import ClassificationResult
from whilly.classifier.heuristic import HeuristicClassifier
from whilly.hierarchy.base import HierarchyLevel

log = logging.getLogger("whilly.classifier.llm")


DEFAULT_MODEL = "claude-haiku-4-5-20251001"
DEFAULT_TIMEOUT_S = 60


_CLASSIFIER_PROMPT = """\
Ты — классификатор задач. Твоя работа: прочитать title+body и решить, какой это
уровень в иерархии работы.

Три уровня:

- **EPIC** — стратегический intent, бизнес-цель, крупная инициатива. Обычно 2-5
  недель работы, декомпозируется на 3-10 Stories. Примеры: "Переход на v2 API",
  "KPI: уменьшить p95 latency на 40%".

- **STORY** — конкретная фича или сценарий пользователя. 2-5 дней работы,
  декомпозируется на 3-8 Tasks. Примеры: "Добавить OAuth через Google",
  "Поддержать экспорт отчётов в CSV".

- **TASK** — атомарная единица работы, один агент = один PR. Обычно
  single-file change или узкая сцепка из 2-3 файлов. Примеры: "Bump pytest
  to 8.0 in pyproject", "Fix typo in README.md", "Rename foo to bar in x.py".

Complexity 1-10:
- 1-3: тривиальное изменение, единственный файл, нет бизнес-логики
- 4-6: средняя работа, несколько файлов, обозримый scope
- 7-10: большой объём, требует дизайн-решений, пересекается с архитектурой

Estimated children — сколько ожидаешь items на один уровень ниже. Для TASK = 0.

Flags — список из нуля или более меток:
- "duplicate-suspected" — похоже на уже существующую задачу
- "underspecified" — слишком расплывчато, нужна доработка
- "out-of-scope" — не похоже на работу для этого проекта
- "non-english" — не англоязычный (информативно, не блокирует)

Входные данные:
```
title: {title}
body:
{body}
```

Выведи ТОЛЬКО один JSON-объект, без markdown-фенсов и пояснений. Первый символ = `{{`:
{{"level":"epic|story|task","confidence":0.0-1.0,"reasoning":"1-2 sentences","estimated_children":int,"complexity_score":1-10,"flags":[...]}}
"""


class LLMClassifier:
    """Primary classifier — one LLM call per input."""

    kind = "llm"

    def __init__(self, model: str = DEFAULT_MODEL, timeout_s: int = DEFAULT_TIMEOUT_S):
        self.model = model
        self.timeout_s = timeout_s
        self._fallback = HeuristicClassifier()

    def classify(self, title: str, body: str) -> ClassificationResult:
        prompt = _CLASSIFIER_PROMPT.format(title=title or "(empty)", body=(body or "(empty)")[:4000])
        try:
            backend = active_backend_from_env()
            result = backend.run(prompt, model=self.model, timeout=self.timeout_s)
        except Exception as exc:  # noqa: BLE001
            log.warning("LLM classifier raised %r — falling back to heuristic", exc)
            fallback = self._fallback.classify(title, body)
            fallback.flags = list(fallback.flags) + [f"llm-failed:{type(exc).__name__}"]
            return fallback

        if result.exit_code != 0 or not result.result_text:
            log.warning("LLM classifier non-zero exit (%s) — falling back", result.exit_code)
            fallback = self._fallback.classify(title, body)
            fallback.flags = list(fallback.flags) + [f"llm-exit:{result.exit_code}"]
            return fallback

        parsed = _parse_json(result.result_text)
        if not parsed:
            log.warning("LLM classifier returned unparseable JSON — falling back")
            fallback = self._fallback.classify(title, body)
            fallback.flags = list(fallback.flags) + ["llm-unparseable"]
            return fallback

        try:
            level = HierarchyLevel(str(parsed.get("level", "")).lower())
        except ValueError:
            log.warning("LLM returned unknown level %r — falling back", parsed.get("level"))
            fallback = self._fallback.classify(title, body)
            fallback.flags = list(fallback.flags) + ["llm-bad-level"]
            return fallback

        return ClassificationResult(
            level=level,
            confidence=parsed.get("confidence", 0.5),
            reasoning=parsed.get("reasoning", "") or "",
            estimated_children=parsed.get("estimated_children", 0),
            complexity_score=parsed.get("complexity_score", 5),
            flags=list(parsed.get("flags") or []),
        )


# ── JSON parsing — tolerant of the usual LLM quirks ─────────────────────────


_JSON_BLOB = re.compile(r"\{.*\}", re.DOTALL)


def _parse_json(raw: str) -> dict | None:
    """Extract the first JSON object from *raw*. Tolerant of markdown
    fences and leading prose (common LLM output failure modes)."""
    if not raw:
        return None
    candidate = raw.strip()
    # Strip fences if the LLM disobeyed the "no fences" instruction.
    if candidate.startswith("```"):
        candidate = re.sub(r"^```(?:json)?\s*", "", candidate)
        candidate = re.sub(r"\s*```\s*$", "", candidate)
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        pass
    match = _JSON_BLOB.search(candidate)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            return None
    return None
