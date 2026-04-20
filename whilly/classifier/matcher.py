"""Parent matcher — pick the best open parent for a new item.

Two impls ship:

* :class:`LLMParentMatcher` — asks the model which of the candidates is
  the closest fit, producing a ranked list with per-match reasoning.
* :class:`NoopParentMatcher` — always returns an empty list. Useful when
  the tracker doesn't support parent relationships, or when the caller
  wants to force "create orphan" (e.g., ``--no-autolink`` CLI flag).

Candidate lists come from :class:`whilly.hierarchy.HierarchyAdapter.list_at_level`
— matcher is tracker-agnostic on purpose.
"""

from __future__ import annotations

import json
import logging
import re

from whilly.agents import active_backend_from_env
from whilly.classifier.base import ParentMatch
from whilly.hierarchy.base import WorkItem

log = logging.getLogger("whilly.classifier.matcher")


DEFAULT_MODEL = "claude-haiku-4-5-20251001"
DEFAULT_TIMEOUT_S = 60


_MATCHER_PROMPT = """\
Ты — семантический маршрутизатор задач. Задача: найти подходящий parent
для входной задачи среди списка открытых кандидатов.

Входная задача:
```
title: {candidate_title}
body:
{candidate_body}
```

Кандидаты (всего {n_candidates}, показаны сокращённо):
{candidates_block}

Реши:
1. Для каждого кандидата оцени 0.0-1.0, насколько входная задача уместна
   как его child. 1.0 = точное попадание. 0.0 = нет связи.
2. Дай короткое обоснование (≤120 символов) почему.
3. Если НИ ОДИН кандидат не подходит лучше 0.4 — верни пустой список
   matches — сигнал router'у создавать orphan.

Выведи ТОЛЬКО JSON, начиная с `{{`:
{{"matches":[{{"id":"candidate-id","score":0.0-1.0,"reasoning":"..."}},...]}}

Сортируй matches по score descending. Возвращай максимум 3 штуки.
"""


class LLMParentMatcher:
    """LLM-based parent matcher."""

    kind = "llm"

    def __init__(self, model: str = DEFAULT_MODEL, timeout_s: int = DEFAULT_TIMEOUT_S):
        self.model = model
        self.timeout_s = timeout_s

    def find_matches(
        self,
        candidate_title: str,
        candidate_body: str,
        candidates: list[WorkItem],
        *,
        max_matches: int = 3,
    ) -> list[ParentMatch]:
        if not candidates:
            return []

        block_lines = []
        for item in candidates[:20]:  # hard cap on prompt size
            snippet = (item.body or "").strip().splitlines()[:3]
            snippet_text = " / ".join(line.strip() for line in snippet)[:200]
            block_lines.append(f"- id: {item.id}\n  title: {item.title}\n  excerpt: {snippet_text}")
        candidates_block = "\n".join(block_lines)

        prompt = _MATCHER_PROMPT.format(
            candidate_title=candidate_title or "(empty)",
            candidate_body=(candidate_body or "(empty)")[:2000],
            n_candidates=len(candidates),
            candidates_block=candidates_block,
        )

        try:
            backend = active_backend_from_env()
            result = backend.run(prompt, model=self.model, timeout=self.timeout_s)
        except Exception as exc:  # noqa: BLE001
            log.warning("matcher LLM raised %r — returning no matches", exc)
            return []

        if result.exit_code != 0 or not result.result_text:
            log.warning("matcher non-zero exit %s — returning no matches", result.exit_code)
            return []

        parsed = _parse_json(result.result_text)
        if not parsed:
            log.warning("matcher returned unparseable JSON — returning no matches")
            return []

        matches_by_id = {item.id: item for item in candidates}
        out: list[ParentMatch] = []
        for entry in (parsed.get("matches") or [])[:max_matches]:
            item_id = entry.get("id")
            parent = matches_by_id.get(item_id)
            if parent is None:
                continue
            out.append(
                ParentMatch(
                    parent=parent,
                    score=float(entry.get("score", 0.0) or 0.0),
                    reasoning=(entry.get("reasoning") or "")[:200],
                )
            )
        out.sort(key=lambda m: m.score, reverse=True)
        return out


class NoopParentMatcher:
    """Always returns no matches — "create everything as orphan"."""

    kind = "noop"

    def find_matches(
        self,
        candidate_title: str,
        candidate_body: str,
        candidates: list[WorkItem],
        *,
        max_matches: int = 3,
    ) -> list[ParentMatch]:
        return []


# ── JSON parsing ─────────────────────────────────────────────────────────────


_JSON_BLOB = re.compile(r"\{.*\}", re.DOTALL)


def _parse_json(raw: str) -> dict | None:
    if not raw:
        return None
    candidate = raw.strip()
    if candidate.startswith("```"):
        candidate = re.sub(r"^```(?:json)?\s*", "", candidate)
        candidate = re.sub(r"\s*```\s*$", "", candidate)
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        pass
    m = _JSON_BLOB.search(candidate)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            return None
    return None
