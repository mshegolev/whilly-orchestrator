"""Heuristic (no-LLM) classifier — length + keyword rules.

Cheap, fast, no network. Used:

* in CI / headless tests (no ANTHROPIC_API_KEY),
* as a fallback when the LLM classifier raises / times out,
* in ``--dry-run`` preview mode where spending tokens is wasteful.

Accuracy target: correct 70-80% of the time on short-to-medium input.
Not a replacement for the LLM classifier — meant as a "no-cost first
pass" that the router can layer over.
"""

from __future__ import annotations

import re

from whilly.classifier.base import ClassificationResult
from whilly.hierarchy.base import HierarchyLevel


# Keyword buckets — membership tips the scale for a level.
# Tuned by eye on ~30 real issue titles/bodies; YMMV on other domains.
_EPIC_CUES: tuple[re.Pattern, ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\b(kpi|okr|okrs|north[-\s]star|strategy|strategic|quarterly|annual)\b",
        r"\b(epic|initiative|roadmap|milestone|program)\b",
        r"\b(rewrite|replatform|migration|reorg|overhaul)\b",
    )
)

_STORY_CUES: tuple[re.Pattern, ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\b(feature|support\s+for|add\s+(the\s+)?ability|new\s+\w+\s+(flow|page|screen))\b",
        r"\b(user\s+can|as\s+a\s+user|as\s+an?\s+admin)\b",
        r"\b(introduce|implement\s+[A-Z])\b",
        r"\b(integration\s+with|integrate\s+\w+)\b",
    )
)

_TASK_CUES: tuple[re.Pattern, ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\b(fix|typo|rename|refactor|dedupe|cleanup|tidy)\b",
        r"\b(add\s+(a\s+)?(test|logging|comment|docstring))\b",
        r"\b(bump|upgrade|pin)\s+\w+",
        r"\b(badge|README|CHANGELOG)\b",
    )
)


_MIN_LEN_FOR_STORY = 80
_MIN_LEN_FOR_EPIC = 250


class HeuristicClassifier:
    """No-LLM classifier — length + regex keyword buckets.

    Confidence is intentionally modest (max 0.6) — this is a *fallback*,
    not a first-class answer. The router's threshold of 0.75 for
    auto-apply means heuristic verdicts always flag for human review,
    which is the right default.
    """

    kind = "heuristic"

    def classify(self, title: str, body: str) -> ClassificationResult:
        text = f"{title}\n{body}".strip()
        length = len(text)
        flags: list[str] = []

        # Count cue hits per bucket.
        epic_hits = sum(bool(p.search(text)) for p in _EPIC_CUES)
        story_hits = sum(bool(p.search(text)) for p in _STORY_CUES)
        task_hits = sum(bool(p.search(text)) for p in _TASK_CUES)

        # Length-based priors. Short inputs skew toward Task; long + cue-rich
        # toward Epic.
        length_bonus_epic = 1 if length >= _MIN_LEN_FOR_EPIC else 0
        length_bonus_story = 1 if length >= _MIN_LEN_FOR_STORY else 0

        epic_score = epic_hits * 2 + length_bonus_epic
        story_score = story_hits * 2 + length_bonus_story
        task_score = task_hits * 2 + (1 if length < _MIN_LEN_FOR_STORY else 0)

        best = max(
            (epic_score, HierarchyLevel.EPIC),
            (story_score, HierarchyLevel.STORY),
            (task_score, HierarchyLevel.TASK),
            key=lambda t: t[0],
        )
        _winning_score, level = best

        # Complexity: rough proxy — length buckets.
        if length < 120:
            complexity = 2
        elif length < 400:
            complexity = 5
        else:
            complexity = 8

        # Estimated children — we don't know from rules alone. Use
        # order-of-magnitude defaults.
        estimated = {HierarchyLevel.EPIC: 5, HierarchyLevel.STORY: 3, HierarchyLevel.TASK: 0}[level]

        # Confidence — scaled by how dominant the winning bucket was.
        all_hits = epic_hits + story_hits + task_hits
        if all_hits == 0:
            confidence = 0.3  # no signal at all
            flags.append("no-keywords-matched")
        else:
            dominance = {
                HierarchyLevel.EPIC: epic_hits,
                HierarchyLevel.STORY: story_hits,
                HierarchyLevel.TASK: task_hits,
            }[level] / all_hits
            confidence = min(0.6, 0.3 + 0.3 * dominance)

        if length < 20:
            flags.append("below-length-threshold")
            confidence = min(confidence, 0.2)

        reasoning = (
            f"heuristic: {level.value} "
            f"(epic_cues={epic_hits}, story_cues={story_hits}, task_cues={task_hits}, length={length})"
        )

        return ClassificationResult(
            level=level,
            confidence=confidence,
            reasoning=reasoning,
            estimated_children=estimated,
            complexity_score=complexity,
            flags=flags,
        )
