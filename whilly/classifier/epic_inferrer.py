"""Epic inference — cluster orphan Stories into synthesised Epics.

The :func:`~whilly.classifier.rebuilder.rebuild_hierarchy` result often ends
up with Stories that have no matching Epic because no Epic was classified
in the input. This module closes the gap by asking an LLM to:

1. Look at the set of orphan Stories.
2. Cluster semantically related ones.
3. Propose an Epic title + short body that would parent each cluster.

The output is a list of :class:`InferredEpic` proposals — plain data, no
tracker mutation. Callers decide whether to materialise them
(via :meth:`whilly.hierarchy.HierarchyAdapter.create_at_level`).

Design mirrors :class:`whilly.classifier.llm.LLMClassifier` —
LLM-backed, graceful fallback to an empty-proposal list on any error,
honours the active backend via :func:`whilly.agents.active_backend_from_env`.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field

from whilly.agents import active_backend_from_env
from whilly.hierarchy.base import WorkItem

log = logging.getLogger("whilly.classifier.epic_inferrer")


DEFAULT_MODEL = "claude-haiku-4-5-20251001"
DEFAULT_TIMEOUT_S = 120
DEFAULT_MAX_EPICS = 5
MIN_STORIES_FOR_INFERENCE = 2


@dataclass
class InferredEpic:
    """One proposed Epic derived from a cluster of orphan Stories.

    Fields:
        title: short proposed Epic title (≤80 chars).
        body: longer description explaining the grouping rationale,
            used as the Epic's description on the tracker.
        child_story_ids: ids of the Stories this Epic would parent.
        confidence: 0.0–1.0 — how cohesive the cluster is. Low scores
            mean the grouping is forced; caller should flag for human
            review before materialising.
        reasoning: 1-2 sentences — "why these Stories belong together".
        applied: set True after the Epic has been created + children linked.
    """

    title: str
    body: str = ""
    child_story_ids: list[str] = field(default_factory=list)
    confidence: float = 0.0
    reasoning: str = ""
    applied: bool = False

    def __post_init__(self) -> None:
        self.confidence = max(0.0, min(1.0, float(self.confidence)))

    @property
    def is_high_confidence(self) -> bool:
        return self.confidence >= 0.65


_INFER_PROMPT = """\
Ты — архитектор-аналитик. Тебе дан список отдельных Stories без общего
Epic. Сгруппируй их в Epics: найди общие темы (бизнес-домен, модуль,
фича-кластер) и предложи Epic-title + краткое описание для каждой группы.

Правила:
- Каждая Story должна попасть максимум в ОДИН Epic.
- Минимум {min_stories} Stories на Epic — одиночные группы не
  создавай (Story останется orphan).
- Максимум {max_epics} Epics суммарно — если групп больше, объедини
  самые мелкие.
- Epic-title ≤80 символов, русский или английский — как у Stories.
- Body — 2-4 строки, объясняющих что объединяет эти Stories.
- confidence 0.0-1.0: насколько кластер когерентен. 1.0 — явно одна
  тема, 0.4 — притянуто за уши.
- Reasoning ≤120 символов — «почему эти Stories вместе».

Orphan Stories ({n} шт):
{stories_block}

Выведи ТОЛЬКО JSON, начинающийся с `{{`:
{{"epics":[{{"title":"...","body":"...","child_story_ids":["..."],"confidence":0.0-1.0,"reasoning":"..."}}]}}
"""


def infer_epics(
    orphan_stories: list[WorkItem],
    *,
    max_epics: int = DEFAULT_MAX_EPICS,
    min_stories_per_epic: int = MIN_STORIES_FOR_INFERENCE,
    model: str = DEFAULT_MODEL,
    timeout_s: int = DEFAULT_TIMEOUT_S,
) -> list[InferredEpic]:
    """Propose synthesised Epics that would parent *orphan_stories*.

    Args:
        orphan_stories: the input set — Stories without an Epic parent.
        max_epics: upper bound on number of proposals.
        min_stories_per_epic: minimum cluster size; singletons stay orphan.
        model / timeout_s: LLM knobs.

    Returns empty list when:

    * fewer than :attr:`MIN_STORIES_FOR_INFERENCE` stories supplied,
    * LLM transport fails,
    * LLM output can't be parsed.

    Never raises — rebuild pipelines must remain robust to flaky LLMs.
    """
    if len(orphan_stories) < MIN_STORIES_FOR_INFERENCE:
        return []

    # Build the stories block — keep each story short so the prompt
    # doesn't balloon on large inputs.
    block_lines = []
    for s in orphan_stories[:50]:  # hard cap prompt size at 50 stories
        snippet = (s.body or "").strip().splitlines()[:2]
        snippet_text = " / ".join(line.strip() for line in snippet)[:150]
        block_lines.append(f"- id: {s.id}\n  title: {s.title}\n  excerpt: {snippet_text}")
    stories_block = "\n".join(block_lines)

    prompt = _INFER_PROMPT.format(
        n=len(orphan_stories),
        stories_block=stories_block,
        max_epics=max_epics,
        min_stories=min_stories_per_epic,
    )

    try:
        backend = active_backend_from_env()
        result = backend.run(prompt, model=model, timeout=timeout_s)
    except Exception as exc:  # noqa: BLE001
        log.warning("epic inference LLM raised %r — returning no proposals", exc)
        return []

    if result.exit_code != 0 or not result.result_text:
        log.warning("epic inference non-zero exit %s — no proposals", result.exit_code)
        return []

    parsed = _parse_json(result.result_text)
    if not parsed:
        log.warning("epic inference returned unparseable JSON — no proposals")
        return []

    valid_ids = {s.id for s in orphan_stories}
    out: list[InferredEpic] = []
    for entry in (parsed.get("epics") or [])[:max_epics]:
        child_ids = [cid for cid in (entry.get("child_story_ids") or []) if cid in valid_ids]
        if len(child_ids) < min_stories_per_epic:
            continue
        title = (entry.get("title") or "").strip()
        if not title:
            continue
        out.append(
            InferredEpic(
                title=title[:80],
                body=(entry.get("body") or "").strip(),
                child_story_ids=child_ids,
                confidence=float(entry.get("confidence") or 0.0),
                reasoning=(entry.get("reasoning") or "")[:200],
            )
        )
    return out


# ── JSON parsing (shared pattern — could be extracted if repeated further) ──


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
