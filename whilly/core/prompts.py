"""Prompt construction for Whilly agents (PRD FR-1.6, Module structure).

This module belongs to the ``whilly.core`` layer (Hexagonal architecture, PRD
TC-8 / SC-6): no I/O, no networking, no file reading, no cwd manipulation. The
:func:`build_task_prompt` function is deterministic — given the same
:class:`~whilly.core.models.Task`, :class:`~whilly.core.models.Plan`, and prompt
guard environment it always produces the same string. Side effects (writing
prompts to disk, sending them over a transport, emitting block events) belong
in adapters/.

Compared with the v3 prompt builder in ``whilly/cli.py`` this version
deliberately drops all references to ``@tasks.json`` / ``@progress.txt``: the
worker transport (TASK-022) carries the task payload over HTTP, so the agent
need not know any host paths. That keeps the prompt portable across local and
remote workers and removes the cwd-magic the v3 loop relied on.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
import hashlib
import os
import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Final

from whilly.core.models import Plan, Task
from whilly.security.prompt_sanitizer import GUARD_SENTENCE, sanitize_external_text

PROMISE_MARKER = "<promise>COMPLETE</promise>"
PROMPT_INJECTION_BLOCKED_EVENT_TYPE: Final[str] = "prompt_injection_blocked"
PROMPT_INJECTION_FAIL_REASON: Final[str] = "prompt_injection_blocked"
PROMPT_DENY_ENV: Final[str] = "WHILLY_PROMPT_DENY_PATTERNS"

_DESC_MARKER_PREFIX: Final[str] = "WHILLY-DESC"
_REDACTED_MARKER: Final[str] = "[blocked]"
_MAX_REDACTED_EXCERPT: Final[int] = 80

_BASELINE_PROMPT_DENY_PATTERNS: tuple[tuple[str, str], ...] = (
    ("ignore-previous-instructions", r"\bIgnore previous instructions\b"),
    ("system-tag", r"</?\s*system\b[^>]*>"),
    # Cyrillic homoglyphs that visually approximate "system": ѕ у ѕ т е м.
    ("system-tag-cyrillic-homoglyph", r"</?\s*[sѕ][yу][sѕ][tт][eе][mм]\b[^>]*>"),
    ("inst-template", r"\[INST\]"),
    ("im-start-template", r"<\|im_start\|>"),
    ("im-end-template", r"<\|im_end\|>"),
)


@dataclass(frozen=True)
class PromptGuardMatch:
    """Structured prompt-guard finding safe to persist in audit payloads."""

    matched_marker: str
    pattern_name: str
    task_id: str
    plan_id: str
    redacted_excerpt: str

    def event_payload(self) -> dict[str, str]:
        return {
            "event_type": PROMPT_INJECTION_BLOCKED_EVENT_TYPE,
            "matched_marker": self.matched_marker,
            "pattern_name": self.pattern_name,
            "task_id": self.task_id,
            "plan_id": self.plan_id,
            "redacted_excerpt": self.redacted_excerpt,
        }


class PromptInjectionBlocked(ValueError):
    """Raised when task description text contains prompt-injection markers."""

    def __init__(self, match: PromptGuardMatch) -> None:
        self.match = match
        super().__init__(
            f"{PROMPT_INJECTION_BLOCKED_EVENT_TYPE}: task={match.task_id} "
            f"plan={match.plan_id} marker={match.matched_marker!r}"
        )

    @property
    def event_payload(self) -> dict[str, str]:
        return self.match.event_payload()


def _normalized(text: str) -> str:
    return unicodedata.normalize("NFKC", text)


def _custom_prompt_patterns() -> tuple[tuple[str, str], ...]:
    raw = os.environ.get(PROMPT_DENY_ENV, "")
    patterns: list[tuple[str, str]] = []
    for idx, fragment in enumerate(raw.split(","), start=1):
        fragment = fragment.strip()
        if fragment:
            patterns.append((f"custom-{idx}", fragment))
    return tuple(patterns)


def _compiled_prompt_patterns() -> tuple[tuple[str, re.Pattern[str]], ...]:
    return tuple(
        (name, re.compile(pattern, re.IGNORECASE | re.MULTILINE))
        for name, pattern in (*_BASELINE_PROMPT_DENY_PATTERNS, *_custom_prompt_patterns())
    )


def _redacted_excerpt(normalized_text: str, match: re.Match[str]) -> str:
    start = max(0, match.start() - 24)
    end = min(len(normalized_text), match.end() + 24)
    excerpt = normalized_text[start:end]
    rel_start = match.start() - start
    rel_end = match.end() - start
    redacted = f"{excerpt[:rel_start]}{_REDACTED_MARKER}{excerpt[rel_end:]}"
    redacted = " ".join(redacted.split())
    if len(redacted) <= _MAX_REDACTED_EXCERPT:
        return redacted
    return redacted[: _MAX_REDACTED_EXCERPT - 3].rstrip() + "..."


def prompt_description_nonce(*, task_id: str, plan_id: str) -> str:
    """Return the deterministic 16-hex prompt envelope nonce for one task."""

    digest = hashlib.blake2s(f"{plan_id}\0{task_id}".encode("utf-8"), digest_size=8).hexdigest()
    return digest


def scan_description_for_prompt_injection(
    description: str,
    *,
    task_id: str,
    plan_id: str,
) -> PromptGuardMatch | None:
    """Return a prompt-guard finding for blocked description text, else ``None``."""

    text = _normalized(description)
    for pattern_name, pattern in _compiled_prompt_patterns():
        match = pattern.search(text)
        if match is None:
            continue
        return PromptGuardMatch(
            matched_marker=match.group(0)[:_MAX_REDACTED_EXCERPT],
            pattern_name=pattern_name,
            task_id=task_id,
            plan_id=plan_id,
            redacted_excerpt=_redacted_excerpt(text, match),
        )
    return None


def wrap_description_in_envelope(description: str, *, task_id: str, plan_id: str) -> str:
    """Wrap task description text in a nonce-delimited prompt-isolation envelope."""

    body = description if description else "(описание не указано)"
    match = scan_description_for_prompt_injection(body, task_id=task_id, plan_id=plan_id)
    if match is not None:
        raise PromptInjectionBlocked(match)
    safe_body = sanitize_external_text(body, scope="task_description")
    nonce = prompt_description_nonce(task_id=task_id, plan_id=plan_id)
    return "\n".join(
        (
            f"BEGIN-{_DESC_MARKER_PREFIX}-{nonce}",
            safe_body,
            f"END-{_DESC_MARKER_PREFIX}-{nonce}",
        )
    )

PR_REVIEW_COMMENT_SCOPE = "pr_review_comment"
PR_DIFF_SCOPE = "pr_diff"


def build_task_prompt(task: Task, plan: Plan) -> str:
    """Construct the agent prompt for ``task`` within ``plan``.

    The returned string:

    * Names the assigned task by ID and pins the agent to it.
    * Includes the task's ``description`` inside a prompt-isolation envelope,
      plus ``acceptance_criteria`` and ``test_steps`` so the agent does not
      have to fetch them.
    * Surfaces ``priority``, ``dependencies``, and ``prd_requirement`` for
      context — these are part of the domain model and cheap to inline.
    * Demands ``<promise>COMPLETE</promise>`` on success (PRD FR-1.6).

    No I/O, no time-dependent values; deterministic for deterministic inputs
    and prompt-guard environment. ``mypy --strict`` clean per PRD NFR-4.
    """
    lines: list[str] = []
    lines.append(f"План: **{plan.name}** (id={plan.id})")
    lines.append(f"Задача: **{task.id}**")
    lines.append(f"Приоритет: {task.priority.value}")
    if task.prd_requirement:
        lines.append("PRD requirement: " + sanitize_external_text(task.prd_requirement, scope="task_prd_requirement"))
    lines.append("")
    lines.append(GUARD_SENTENCE)
    lines.append("")

    lines.append("## Описание")
    lines.append(wrap_description_in_envelope(task.description, task_id=task.id, plan_id=plan.id))
    lines.append("")

    if task.dependencies:
        lines.append("## Зависимости (должны быть DONE до старта)")
        for dep in task.dependencies:
            lines.append(f"- {dep}")
        lines.append("")

    if task.acceptance_criteria:
        lines.append("## Acceptance criteria")
        for idx, criterion in enumerate(task.acceptance_criteria, start=1):
            lines.append(f"{idx}. {sanitize_external_text(criterion, scope='task_acceptance_criterion')}")
        lines.append("")

    if task.test_steps:
        lines.append("## Test steps")
        for idx, step in enumerate(task.test_steps, start=1):
            lines.append(f"{idx}. {sanitize_external_text(step, scope='task_test_step')}")
        lines.append("")

    if task.key_files:
        lines.append("## Ключевые файлы")
        for path in task.key_files:
            lines.append(f"- {path}")
        lines.append("")

    lines.append("## Правила")
    lines.append(f"- Работай ТОЛЬКО над задачей {task.id}; не трогай другие задачи плана.")
    lines.append("- Закрой все acceptance criteria и пройди все test steps.")
    lines.append(f"- На финише, ТОЛЬКО при полном успехе, выведи `{PROMISE_MARKER}`.")
    lines.append("- Если не можешь завершить — опиши проблему и НЕ выводи promise-маркер.")

    return "\n".join(lines)


def build_pr_fix_prompt(
    task: Task,
    plan: Plan,
    review_comments: Iterable[Mapping[str, Any]],
    diff: str,
) -> str:
    """Construct the agent prompt for fixing a PR's review comments.

    The returned string:

    * Names the originating task by id and pins the agent to it.
    * Surfaces the PR URL (read from ``task.prd_requirement`` — the
      M2 re-iterate path stores the PR URL there).
    * Embeds every review-comment body inside an
      ``<UNTRUSTED kind=pr_review_comment>...</UNTRUSTED>`` envelope
      via :func:`whilly.security.prompt_sanitizer.sanitize_external_text`,
      preceded by the canonical do-not-follow-instructions guard
      sentence.
    * Embeds the supplied diff inside an
      ``<UNTRUSTED kind=pr_diff>...</UNTRUSTED>`` envelope via the
      same sanitizer.
    * Instructs the agent to fix only what reviewers asked for and
      re-push to the SAME branch (single-task scope).
    * Demands ``<promise>COMPLETE</promise>`` on success — preserving
      the existing completion contract from :func:`build_task_prompt`
      so the orchestrator's COMPLETE detector keeps working.

    The function is pure and deterministic: identical inputs produce
    identical output. Sanitization is idempotent — feeding already-fenced
    text back in returns byte-identical content (see
    :func:`whilly.security.prompt_sanitizer.sanitize_external_text`).
    """
    lines: list[str] = []
    lines.append(f"План: **{plan.name}** (id={plan.id})")
    lines.append(f"Задача: **{task.id}** (PR fix iteration)")
    lines.append(f"Приоритет: {task.priority.value}")
    if task.prd_requirement:
        lines.append("PR URL: " + sanitize_external_text(task.prd_requirement, scope="task_prd_requirement"))
    lines.append("")
    lines.append(GUARD_SENTENCE)
    lines.append("")

    lines.append("## Комментарии ревьюера")
    has_comment = False
    for entry in review_comments:
        if not isinstance(entry, Mapping):
            continue
        body = entry.get("body")
        if body is None:
            continue
        body_text = body if isinstance(body, str) else str(body)
        path = entry.get("path") or ""
        line_no = entry.get("line")
        author = entry.get("author") or ""
        meta_bits: list[str] = []
        if path:
            meta_bits.append(f"file={path}")
        if line_no is not None and line_no != "":
            meta_bits.append(f"line={line_no}")
        if author:
            meta_bits.append(f"author={author}")
        meta_suffix = f" ({', '.join(meta_bits)})" if meta_bits else ""
        lines.append(f"- Comment{meta_suffix}:")
        lines.append(sanitize_external_text(body_text, scope=PR_REVIEW_COMMENT_SCOPE))
        has_comment = True
    if not has_comment:
        lines.append("(no review comments supplied)")
    lines.append("")

    lines.append("## PR diff")
    lines.append(sanitize_external_text(diff, scope=PR_DIFF_SCOPE))
    lines.append("")

    lines.append("## Правила")
    lines.append(
        f"- Работай ТОЛЬКО над задачей {task.id}; адресуй ТОЛЬКО то, "
        "что просили ревьюеры в комментариях выше — не вноси несвязанных "
        "изменений (single-task review-only scope)."
    )
    lines.append(
        "- После исправления отправь правки в ТУ ЖЕ ВЕТКУ (push to the same branch) "
        "ассоциированную с этим PR; не открывай новый PR."
    )
    lines.append("- Прогоняй `make lint` / `make test` локально перед push.")
    lines.append(f"- На финише, ТОЛЬКО при полном успехе, выведи `{PROMISE_MARKER}`.")
    lines.append("- Если не можешь завершить — опиши проблему и НЕ выводи promise-маркер.")

    return "\n".join(lines)


__all__ = [
    "PR_DIFF_SCOPE",
    "PR_REVIEW_COMMENT_SCOPE",
    "PROMISE_MARKER",
    "PROMPT_DENY_ENV",
    "PROMPT_INJECTION_BLOCKED_EVENT_TYPE",
    "PROMPT_INJECTION_FAIL_REASON",
    "PromptGuardMatch",
    "PromptInjectionBlocked",
    "build_pr_fix_prompt",
    "build_task_prompt",
    "prompt_description_nonce",
    "scan_description_for_prompt_injection",
    "wrap_description_in_envelope",
]
