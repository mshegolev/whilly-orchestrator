"""Decision Gate — short LLM-based pre-check before dispatching an agent.

Idea borrowed from `stepango/grkr`: before spending a full agent run on a task,
ask a cheap LLM gate "should we take this task or refuse?". On refuse, the task
is marked `skipped` and (optionally) labelled `needs-clarification` upstream.

Fail-open semantics: any error (timeout, parse failure, LLM down) defaults to
`proceed` so we never starve the loop on infrastructure flakes.

Programmatic:
    from whilly.decision_gate import evaluate
    decision = evaluate(task)  # returns Decision(decision="proceed"|"refuse", reason=..., cost_usd=...)
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Callable

from whilly.agent_runner import AgentResult, run_agent
from whilly.task_manager import Task

log = logging.getLogger("whilly")


DEFAULT_MODEL = "claude-haiku-4-5-20251001"
DEFAULT_TIMEOUT_S = 60
MIN_DESCRIPTION_LEN = 20

# Where decisions land. Public to keep tests friendly.
PROCEED = "proceed"
REFUSE = "refuse"


@dataclass
class Decision:
    decision: str  # "proceed" | "refuse"
    reason: str
    cost_usd: float = 0.0
    raw_text: str = ""  # for debug only


# ── Prompt ────────────────────────────────────────────────────────────────────


PROMPT_TEMPLATE = """Ты — gate-агент, проверяющий задачи перед исполнением.
Твоя цель — отсеять заведомо мусорные/неполные задачи, чтобы не тратить токены.

Задача:
- ID: {id}
- Priority: {priority}
- Описание:
{description}
- Acceptance criteria: {acceptance}
- key_files: {key_files}

Реши: брать в работу или отказаться?

Откажись (refuse) если:
- описание явно бессмысленное или короче 20 символов
- противоречивые требования
- нужны секреты / доступы которых нет
- acceptance отсутствует И описание неоднозначно

Возьмись (proceed) если:
- описание понятно и есть хотя бы 1 acceptance criterion
- ИЛИ описание простое и однозначное (badge, README fix, version bump)
- При сомнениях — proceed (false-refuse дороже false-proceed).

Ответ строго одной строкой валидного JSON, без пояснений и без блока ```:
{{"decision":"proceed"|"refuse","reason":"≤120 chars"}}
"""


def build_prompt(task: Task) -> str:
    acceptance = ", ".join(task.acceptance_criteria) if task.acceptance_criteria else "не задано"
    files = ", ".join(task.key_files) if task.key_files else "не указаны"
    return PROMPT_TEMPLATE.format(
        id=task.id,
        priority=task.priority,
        description=(task.description or "(пусто)").strip(),
        acceptance=acceptance,
        key_files=files,
    )


# ── Result parsing ────────────────────────────────────────────────────────────


_JSON_BLOB_RE = re.compile(r"\{[^{}]*\"decision\"\s*:\s*\"(?P<dec>proceed|refuse)\"[^{}]*\}", re.IGNORECASE)


def parse_decision(raw_text: str) -> tuple[str, str]:
    """Extract (decision, reason) from raw LLM output.

    Tolerant: accepts bare JSON, fenced JSON, JSON with extra text around.
    Returns (PROCEED, "...") on parse failure (fail-open).
    """
    if not raw_text:
        return PROCEED, "fail-open: empty LLM response"

    # 1) Try direct JSON parse first.
    candidate = raw_text.strip()
    try:
        parsed = json.loads(candidate)
        decision = (parsed.get("decision") or "").lower()
        reason = (parsed.get("reason") or "").strip()
        if decision in (PROCEED, REFUSE):
            return decision, reason or "no reason"
    except (json.JSONDecodeError, AttributeError):
        pass

    # 2) Search for an embedded JSON blob.
    m = _JSON_BLOB_RE.search(candidate)
    if m:
        try:
            sub = candidate[m.start() : m.end()]
            parsed = json.loads(sub)
            decision = (parsed.get("decision") or "").lower()
            reason = (parsed.get("reason") or "").strip()
            if decision in (PROCEED, REFUSE):
                return decision, reason or "no reason"
        except json.JSONDecodeError:
            pass

    # 3) Bare keyword fallback.
    low = candidate.lower()
    if "refuse" in low and "proceed" not in low:
        return REFUSE, "fallback keyword: refuse"
    return PROCEED, "fail-open: could not parse decision"


# ── Evaluation ────────────────────────────────────────────────────────────────


# Type alias for the runner — used to inject mocks in tests.
RunnerFn = Callable[[str, str, int | None], AgentResult]


def _default_runner(prompt: str, model: str, timeout: int | None) -> AgentResult:
    return run_agent(prompt, model=model, timeout=timeout)


def evaluate(
    task: Task,
    model: str = DEFAULT_MODEL,
    timeout_s: int = DEFAULT_TIMEOUT_S,
    runner: RunnerFn = _default_runner,
) -> Decision:
    """Run the decision gate for a task.

    Always returns a Decision (never raises). The default runner uses the
    `whilly.agent_runner.run_agent` underneath; tests inject a fake runner.
    """
    # Hard rule: descriptions shorter than MIN_DESCRIPTION_LEN are auto-refused
    # without spending an LLM call.
    desc = (task.description or "").strip()
    if len(desc) < MIN_DESCRIPTION_LEN:
        return Decision(
            decision=REFUSE,
            reason=f"description too short ({len(desc)} chars < {MIN_DESCRIPTION_LEN})",
            cost_usd=0.0,
        )

    prompt = build_prompt(task)
    try:
        result = runner(prompt, model, timeout_s)
    except Exception as exc:  # noqa: BLE001
        log.warning("Decision gate LLM call raised: %s", exc)
        return Decision(decision=PROCEED, reason=f"fail-open: runner exception {exc}", cost_usd=0.0)

    if result.exit_code != 0:
        log.warning("Decision gate non-zero exit (%s) for %s", result.exit_code, task.id)
        return Decision(
            decision=PROCEED,
            reason=f"fail-open: runner exit {result.exit_code}",
            cost_usd=result.usage.cost_usd,
            raw_text=result.result_text,
        )

    decision, reason = parse_decision(result.result_text)
    return Decision(
        decision=decision,
        reason=reason,
        cost_usd=result.usage.cost_usd,
        raw_text=result.result_text,
    )


# ── Optional GH label flip on refuse ──────────────────────────────────────────


def label_flip_for_gh_task(
    task: Task,
    decision: Decision,
    add_label: str = "needs-clarification",
    remove_label: str = "whilly:ready",
    add_comment: bool = False,
    runner: Callable[[list[str]], int] | None = None,
) -> bool:
    """If task originated from GitHub Issues source and was refused, flip labels.

    Returns True if a label flip was attempted, False if not applicable.
    `runner` (optional) accepts a list of CLI args and returns exit code; default
    invokes `gh` via subprocess.
    """
    if decision.decision != REFUSE:
        return False
    if not (task.id.startswith("GH-") and task.prd_requirement):
        return False

    # Extract issue number from prd_requirement URL.
    m = re.search(r"/issues/(\d+)", task.prd_requirement)
    if not m:
        return False
    issue_n = m.group(1)

    # owner/repo from the URL too.
    repo_m = re.search(r"github\.com/([^/]+/[^/]+)/issues/", task.prd_requirement)
    if not repo_m:
        return False
    repo = repo_m.group(1)

    args = [
        "issue",
        "edit",
        issue_n,
        "--repo",
        repo,
        "--add-label",
        add_label,
        "--remove-label",
        remove_label,
    ]

    if runner is None:
        import os as _os
        import subprocess

        env = dict(_os.environ)
        env.pop("GITHUB_TOKEN", None)
        env.pop("GH_TOKEN", None)
        proc = subprocess.run(["gh", *args], capture_output=True, text=True, env=env, check=False)
        if proc.returncode != 0:
            log.warning("gh issue edit (label flip) failed for %s: %s", task.id, proc.stderr.strip())
            return False
        if add_comment:
            comment_proc = subprocess.run(
                [
                    "gh",
                    "issue",
                    "comment",
                    issue_n,
                    "--repo",
                    repo,
                    "--body",
                    f"Whilly Decision Gate refused this task: {decision.reason}",
                ],
                capture_output=True,
                text=True,
                env=env,
                check=False,
            )
            if comment_proc.returncode != 0:
                log.warning("gh issue comment failed for %s: %s", task.id, comment_proc.stderr.strip())
        return True
    else:
        return runner(args) == 0
