"""Jira work classification, comment commands, and code-readiness probes.

This module is intentionally read-only. It does not call Jira, GitLab, git, or
the database; callers pass already-fetched issue/plan data and local repo paths.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

WORK_KINDS: tuple[str, ...] = ("feature", "bug", "task", "devops")
URGENCIES: tuple[str, ...] = ("normal", "hotfix")
READINESS_VERDICTS: tuple[str, ...] = (
    "ready_for_testing",
    "needs_test_plan",
    "needs_repo_choice",
    "needs_human_context",
    "blocked",
)


@dataclass(frozen=True)
class JiraWorkClassification:
    """Routing decision for one incoming Jira work item."""

    kind: str
    urgency: str
    confidence: str
    signals: tuple[str, ...]
    missing_context: tuple[str, ...]
    recommended_flow: str

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["signals"] = list(self.signals)
        data["missing_context"] = list(self.missing_context)
        return data


@dataclass(frozen=True)
class JiraCommentCommand:
    """A supported operator command parsed from a Jira comment."""

    action: str
    value: str = ""
    raw: str = ""

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass(frozen=True)
class CodeReadinessResult:
    """Read-only assessment of whether a local repo has test evidence."""

    repo_path: str
    verdict: str
    test_commands: tuple[str, ...] = ()
    detected_files: tuple[str, ...] = ()
    test_files: tuple[str, ...] = ()
    missing_context: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["test_commands"] = list(self.test_commands)
        data["detected_files"] = list(self.detected_files)
        data["test_files"] = list(self.test_files)
        data["missing_context"] = list(self.missing_context)
        return data


@dataclass(frozen=True)
class _IssueFields:
    key: str
    summary: str
    description: str
    issue_type: str
    priority: str
    labels: tuple[str, ...]
    acceptance_criteria: tuple[str, ...]
    test_steps: tuple[str, ...]


_COMMAND_RE = re.compile(r"(?im)(?:^|\s)/whilly\s+([a-z][a-z_-]*)(?:\s+([^\n#]+))?")
_COMMENT_ACTIONS = {"classify", "urgency", "prd", "plan", "run", "continue", "replan", "cancel"}

_FEATURE_TYPES = {"story", "feature", "epic", "new feature"}
_BUG_TYPES = {"bug", "defect"}
_TASK_TYPES = {"task", "sub-task", "subtask", "chore"}
_DEVOPS_TYPES = {"devops", "infrastructure", "infra"}
_HOTFIX_PRIORITIES = {"highest", "blocker", "critical", "p0", "p1"}

_FEATURE_KEYWORDS = (
    "feature",
    "story",
    "acceptance criteria",
    "implement",
    "build",
    "add ",
    "new ",
    "user can",
)
_BUG_KEYWORDS = (
    "bug",
    "defect",
    "regression",
    "actual",
    "expected",
    "reproduce",
    "steps to reproduce",
    "stack trace",
    "exception",
    "error",
    "fails",
    "failure",
)
_TASK_KEYWORDS = (
    "task",
    "chore",
    "one-off",
    "one off",
    "rename",
    "docs",
    "documentation",
    "migration",
    "update",
    "configure",
)
_DEVOPS_KEYWORDS = (
    "ci",
    "cd",
    "pipeline",
    "deploy",
    "deployment",
    "docker",
    "compose",
    "kubernetes",
    "k8s",
    "helm",
    "terraform",
    "infra",
    "infrastructure",
    "secret",
    "environment",
)
_HOTFIX_KEYWORDS = (
    "hotfix",
    "incident",
    "production",
    "prod down",
    "sev1",
    "sev-1",
    "p0",
    "urgent",
    "critical",
    "outage",
)


def classify_jira_work(issue: Mapping[str, Any]) -> JiraWorkClassification:
    """Classify Jira work into IT delivery flows used by Whilly."""

    fields = _extract_issue_fields(issue)
    text = _classification_text(fields)
    scores = {"feature": 0, "bug": 0, "task": 0, "devops": 0}
    signals: list[str] = []

    issue_type = fields.issue_type.lower()
    if issue_type in _FEATURE_TYPES:
        scores["feature"] += 3
        signals.append(f"jira_type:{issue_type}")
    if issue_type in _BUG_TYPES:
        scores["bug"] += 4
        signals.append(f"jira_type:{issue_type}")
    if issue_type in _TASK_TYPES:
        scores["task"] += 3
        signals.append(f"jira_type:{issue_type}")
    if issue_type in _DEVOPS_TYPES:
        scores["devops"] += 4
        signals.append(f"jira_type:{issue_type}")

    _score_keywords(text, _FEATURE_KEYWORDS, "feature", scores, signals)
    _score_keywords(text, _BUG_KEYWORDS, "bug", scores, signals)
    _score_keywords(text, _TASK_KEYWORDS, "task", scores, signals)
    _score_keywords(text, _DEVOPS_KEYWORDS, "devops", scores, signals)

    label_text = " ".join(label.lower() for label in fields.labels)
    if label_text:
        _score_keywords(label_text, _DEVOPS_KEYWORDS, "devops", scores, signals, prefix="label")
        _score_keywords(label_text, _BUG_KEYWORDS, "bug", scores, signals, prefix="label")
        _score_keywords(label_text, _FEATURE_KEYWORDS, "feature", scores, signals, prefix="label")

    kind = _select_kind(scores)
    top_score = scores[kind]
    confidence = "high" if top_score >= 4 else "medium" if top_score >= 2 else "low"

    urgency = "normal"
    if fields.priority.lower() in _HOTFIX_PRIORITIES:
        urgency = "hotfix"
        signals.append(f"priority:{fields.priority.lower()}")
    if _contains_any(text, _HOTFIX_KEYWORDS) or _contains_any(label_text, _HOTFIX_KEYWORDS):
        urgency = "hotfix"
        signals.append("hotfix_signal")

    missing_context = _missing_context(kind=kind, urgency=urgency, fields=fields, text=text)
    return JiraWorkClassification(
        kind=kind,
        urgency=urgency,
        confidence=confidence,
        signals=tuple(dict.fromkeys(signals)),
        missing_context=tuple(missing_context),
        recommended_flow=_recommended_flow(kind, urgency),
    )


def parse_whilly_comment_command(body: str) -> JiraCommentCommand | None:
    """Parse the first supported ``/whilly`` command from a Jira comment."""

    match = _COMMAND_RE.search(body or "")
    if match is None:
        return None
    action = match.group(1).strip().lower().replace("-", "_")
    value = (match.group(2) or "").strip().lower()
    if action not in _COMMENT_ACTIONS:
        return None
    if action == "classify" and value not in WORK_KINDS:
        raise ValueError(f"unsupported /whilly classify value {value!r}; expected one of {WORK_KINDS!r}")
    if action == "urgency" and value not in URGENCIES:
        raise ValueError(f"unsupported /whilly urgency value {value!r}; expected one of {URGENCIES!r}")
    if action not in {"classify", "urgency"}:
        value = ""
    return JiraCommentCommand(action=action, value=value, raw=match.group(0).strip())


def jira_context_hashes(issue: Mapping[str, Any], links: Sequence[str] | None = None) -> dict[str, Any]:
    """Return stable hashes for change detection over Jira text and links."""

    fields = _extract_issue_fields(issue)
    normalized_links = sorted({str(link).strip() for link in links or () if str(link).strip()})
    summary_hash = _digest(fields.summary)
    description_hash = _digest(fields.description)
    link_set_hash = _digest(normalized_links)
    return {
        "summary_hash": summary_hash,
        "description_hash": description_hash,
        "link_set_hash": link_set_hash,
        "combined_hash": _digest(
            {
                "summary_hash": summary_hash,
                "description_hash": description_hash,
                "link_set_hash": link_set_hash,
            }
        ),
        "links": normalized_links,
    }


def release_context_repo_targets(context: Any) -> list[dict[str, str]]:
    """Convert QA release Git repo hints into plan-compatible repo targets."""

    targets: list[dict[str, str]] = []
    seen: set[str] = set()
    for hint in getattr(context, "repo_hints", ()) or ():
        provider = str(getattr(hint, "provider", "") or "").strip()
        repo_full_name = str(getattr(hint, "repo_full_name", "") or "").strip()
        if not provider or not repo_full_name:
            continue
        target_id = f"{provider}:{repo_full_name}"
        if target_id in seen:
            continue
        seen.add(target_id)
        targets.append(
            {
                "id": target_id,
                "provider": provider,
                "repo_full_name": repo_full_name,
                "clone_url": str(getattr(hint, "clone_url", "") or ""),
                "default_branch": "",
            }
        )
    return targets


def probe_code_readiness(repo_path: str | Path) -> CodeReadinessResult:
    """Inspect a local checkout for test commands and unit-test evidence."""

    root = Path(repo_path).expanduser()
    if not root.exists() or not root.is_dir():
        return CodeReadinessResult(
            repo_path=str(root),
            verdict="blocked",
            missing_context=("repo_path",),
        )

    detected_files = _detected_repo_files(root)
    test_commands = _test_commands(root)
    test_files = _test_files(root)
    missing: list[str] = []
    if not test_commands:
        missing.append("test_command")
    if not test_files:
        missing.append("unit_tests")
    verdict = "ready_for_testing" if not missing else "needs_test_plan"
    return CodeReadinessResult(
        repo_path=str(root),
        verdict=verdict,
        test_commands=tuple(test_commands),
        detected_files=tuple(detected_files),
        test_files=tuple(test_files),
        missing_context=tuple(missing),
    )


def build_jira_work_metadata(
    issue: Mapping[str, Any],
    *,
    issue_key: str = "",
    links: Sequence[str] | None = None,
    release_context: Any | None = None,
    repo_path: str | Path | None = None,
) -> dict[str, Any]:
    """Build a JSON-serializable Jira work block for plan files."""

    context_links = list(links or ())
    if release_context is not None:
        context_links.extend(str(getattr(link, "url", "") or "") for link in getattr(release_context, "links", ()))
    classification = classify_jira_work(issue)
    metadata: dict[str, Any] = {
        "issue_key": issue_key or _extract_issue_fields(issue).key,
        "classification": classification.to_dict(),
        "context_hashes": jira_context_hashes(issue, context_links),
        "repo_hints": release_context_repo_targets(release_context) if release_context is not None else [],
        "comment_commands": {
            "supported": [
                "/whilly classify <feature|bug|task|devops>",
                "/whilly urgency <normal|hotfix>",
                "/whilly prd",
                "/whilly plan",
                "/whilly run",
                "/whilly continue",
                "/whilly replan",
                "/whilly cancel",
            ]
        },
    }
    if repo_path is not None:
        metadata["readiness"] = probe_code_readiness(repo_path).to_dict()
    return metadata


def _extract_issue_fields(issue: Mapping[str, Any]) -> _IssueFields:
    raw_fields = issue.get("fields")
    fields = raw_fields if isinstance(raw_fields, Mapping) else {}
    tasks = _plan_tasks(issue)
    key = _first_string(issue.get("key"), issue.get("issue_key"), issue.get("jira_key"))
    summary = _first_string(
        issue.get("summary"),
        fields.get("summary"),
        issue.get("name"),
        issue.get("project"),
        *(task.get("title") for task in tasks),
    )
    description = _join_text(
        _first_string(issue.get("description"), fields.get("description")),
        *(str(task.get("description") or task.get("prd_requirement") or "") for task in tasks),
    )
    issue_type = _nested_name(fields.get("issuetype")) or _first_string(issue.get("issue_type"), issue.get("type"))
    priority = _nested_name(fields.get("priority")) or _first_string(issue.get("priority"))
    labels = tuple(_string_list(fields.get("labels") or issue.get("labels")))
    acceptance = tuple(
        item
        for item in (
            *_string_list(issue.get("acceptance_criteria")),
            *(value for task in tasks for value in _string_list(task.get("acceptance_criteria"))),
        )
        if item
    )
    test_steps = tuple(
        item
        for item in (
            *_string_list(issue.get("test_steps")),
            *(value for task in tasks for value in _string_list(task.get("test_steps"))),
        )
        if item
    )
    return _IssueFields(
        key=key,
        summary=summary,
        description=description,
        issue_type=issue_type,
        priority=priority,
        labels=labels,
        acceptance_criteria=acceptance,
        test_steps=test_steps,
    )


def _plan_tasks(issue: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    raw = issue.get("tasks")
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, Mapping)]


def _classification_text(fields: _IssueFields) -> str:
    return " ".join(
        part
        for part in (
            fields.key,
            fields.summary,
            fields.description,
            fields.issue_type,
            fields.priority,
            " ".join(fields.labels),
            " ".join(fields.acceptance_criteria),
            " ".join(fields.test_steps),
        )
        if part
    ).lower()


def _score_keywords(
    text: str,
    keywords: Sequence[str],
    kind: str,
    scores: dict[str, int],
    signals: list[str],
    *,
    prefix: str = "keyword",
) -> None:
    hits = [keyword.strip() for keyword in keywords if keyword.strip() and keyword.strip() in text]
    if not hits:
        return
    scores[kind] += min(len(hits), 3)
    signals.append(f"{prefix}:{kind}")


def _select_kind(scores: Mapping[str, int]) -> str:
    ordered = ("bug", "devops", "feature", "task")
    best = max(ordered, key=lambda kind: scores.get(kind, 0))
    if scores.get(best, 0) <= 0:
        return "task"
    return best


def _missing_context(*, kind: str, urgency: str, fields: _IssueFields, text: str) -> list[str]:
    missing: list[str] = []
    if kind == "feature" and not fields.acceptance_criteria and "acceptance criteria" not in text:
        missing.append("acceptance_criteria")
    if kind == "bug":
        if not _contains_any(text, ("reproduce", "steps to reproduce", "repro")):
            missing.append("reproduction_steps")
        if not ("expected" in text and "actual" in text):
            missing.append("expected_actual")
    if kind == "task":
        if not fields.test_steps and not _contains_any(text, ("verify", "validation", "test")):
            missing.append("verification_step")
    if kind == "devops":
        if not _contains_any(text, ("env", "environment", "production", "staging", "ci", "pipeline")):
            missing.append("target_environment")
        if not _contains_any(text, ("rollback", "restore", "revert")):
            missing.append("rollback_plan")
        if not _contains_any(text, ("dry run", "dry-run", "plan only")):
            missing.append("dry_run")
    if urgency == "hotfix":
        if not _contains_any(text, ("rollback", "restore", "revert")):
            missing.append("rollback_plan")
        if not _contains_any(text, ("smoke", "healthcheck", "health check")):
            missing.append("smoke_test")
        if not _contains_any(text, ("risk", "impact", "blast radius")):
            missing.append("risk_assessment")
    return list(dict.fromkeys(missing))


def _recommended_flow(kind: str, urgency: str) -> str:
    if urgency == "hotfix":
        return f"hotfix_{kind}"
    return {
        "feature": "feature_prd",
        "bug": "bug_repro",
        "task": "task_checklist",
        "devops": "devops_change",
    }[kind]


def _contains_any(text: str, needles: Sequence[str]) -> bool:
    return any(needle in text for needle in needles)


def _first_string(*values: Any) -> str:
    for value in values:
        text = _stringify(value).strip()
        if text:
            return text
    return ""


def _join_text(*values: str) -> str:
    return "\n\n".join(value.strip() for value in values if value and value.strip())


def _nested_name(value: Any) -> str:
    if isinstance(value, Mapping):
        return _first_string(value.get("name"), value.get("value"))
    return _first_string(value)


def _string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        return [text for item in value if (text := _stringify(item).strip())]
    return []


def _stringify(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping):
        text = value.get("text")
        if isinstance(text, str):
            return text
        content = value.get("content")
        if isinstance(content, Sequence):
            return " ".join(_stringify(item) for item in content)
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        return " ".join(_stringify(item) for item in value)
    return str(value)


def _digest(value: Any) -> str:
    if isinstance(value, str):
        raw = value
    else:
        raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _detected_repo_files(root: Path) -> list[str]:
    names = (
        "pyproject.toml",
        "pytest.ini",
        "tox.ini",
        "setup.cfg",
        "package.json",
        "pnpm-lock.yaml",
        "yarn.lock",
        "go.mod",
        "pom.xml",
        "build.gradle",
        "build.gradle.kts",
        "gradlew",
        "Cargo.toml",
    )
    return [name for name in names if (root / name).exists()]


def _test_commands(root: Path) -> list[str]:
    commands: list[str] = []
    if any((root / name).exists() for name in ("pyproject.toml", "pytest.ini", "tox.ini", "setup.cfg")):
        if (root / "tests" / "unit").is_dir():
            commands.append("python3 -m pytest -q tests/unit")
        elif (root / "tests").is_dir():
            commands.append("python3 -m pytest -q tests")
        else:
            commands.append("python3 -m pytest -q")
    if (root / "package.json").is_file() and _package_has_test_script(root / "package.json"):
        if (root / "pnpm-lock.yaml").exists():
            commands.append("pnpm test")
        elif (root / "yarn.lock").exists():
            commands.append("yarn test")
        else:
            commands.append("npm test")
    if (root / "go.mod").is_file():
        commands.append("go test ./...")
    if (root / "pom.xml").is_file():
        commands.append("mvn test")
    if (root / "gradlew").is_file():
        commands.append("./gradlew test")
    elif (root / "build.gradle").is_file() or (root / "build.gradle.kts").is_file():
        commands.append("gradle test")
    if (root / "Cargo.toml").is_file():
        commands.append("cargo test")
    return commands


def _package_has_test_script(path: Path) -> bool:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    scripts = data.get("scripts") if isinstance(data, Mapping) else None
    return isinstance(scripts, Mapping) and bool(str(scripts.get("test") or "").strip())


def _test_files(root: Path) -> list[str]:
    patterns = (
        "tests/**/test_*.py",
        "tests/**/*_test.py",
        "test_*.py",
        "**/*.test.js",
        "**/*.spec.js",
        "**/*.test.ts",
        "**/*.spec.ts",
        "**/*_test.go",
        "src/test/**",
        "tests/**/*.rs",
    )
    found: list[str] = []
    for pattern in patterns:
        for path in root.glob(pattern):
            if not path.is_file() or _is_ignored_test_path(path):
                continue
            rel = path.relative_to(root).as_posix()
            if rel not in found:
                found.append(rel)
            if len(found) >= 50:
                return found
    return found


def _is_ignored_test_path(path: Path) -> bool:
    ignored = {".git", ".venv", "venv", "node_modules", "dist", "build", "__pycache__"}
    return any(part in ignored for part in path.parts)


__all__ = [
    "CodeReadinessResult",
    "JiraCommentCommand",
    "JiraWorkClassification",
    "READINESS_VERDICTS",
    "URGENCIES",
    "WORK_KINDS",
    "build_jira_work_metadata",
    "classify_jira_work",
    "jira_context_hashes",
    "parse_whilly_comment_command",
    "probe_code_readiness",
    "release_context_repo_targets",
]
