"""GitHub issue feedback reporter for bugs and ideas."""

from __future__ import annotations

import os
import platform
import subprocess
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from whilly import __version__
from whilly.gh_utils import gh_subprocess_env
from whilly.security.prompt_sanitizer import sanitize_title_slot
from whilly.security.secret_lint import redact_secrets

DEFAULT_FEEDBACK_REPO = "mshegolev/whilly-orchestrator"
FEEDBACK_REPO_ENV = "WHILLY_FEEDBACK_REPO"


class FeedbackKind(str, Enum):
    BUG = "bug"
    IDEA = "idea"


@dataclass(frozen=True)
class GitHubIssueResult:
    ok: bool
    issue_url: str = ""
    command: tuple[str, ...] = ()
    dry_run: bool = False
    returncode: int = 0
    reason: str = ""


def default_labels(kind: FeedbackKind | str) -> tuple[str, ...]:
    feedback_kind = kind if isinstance(kind, FeedbackKind) else FeedbackKind(kind)
    return (feedback_kind.value, "whilly")


def default_repo(environ: dict[str, str] | None = None) -> str:
    env = os.environ if environ is None else environ
    return (env.get(FEEDBACK_REPO_ENV) or DEFAULT_FEEDBACK_REPO).strip()


def build_feedback_body(
    *,
    kind: FeedbackKind | str,
    title: str,
    message: str,
    command: str = "",
) -> str:
    feedback_kind = kind if isinstance(kind, FeedbackKind) else FeedbackKind(kind)
    safe_message = redact_secrets(message.strip())
    safe_command = sanitize_title_slot(command, max_chars=180)
    safe_title = sanitize_title_slot(title, max_chars=120)

    lines = [
        "## Report",
        f"- kind: `{feedback_kind.value}`",
        f"- title: `{safe_title}`",
    ]
    if safe_command:
        lines.append(f"- command: `{safe_command}`")
    lines.extend(
        [
            "",
            "## Details",
            safe_message or "(no details provided)",
            "",
            "## Environment",
            f"- whilly: `{__version__}`",
            f"- python: `{platform.python_version()}`",
            f"- platform: `{platform.platform()}`",
            f"- executable: `{sys.executable}`",
        ]
    )
    return "\n".join(lines)


def create_github_issue(
    *,
    repo: str,
    title: str,
    body: str,
    labels: tuple[str, ...],
    dry_run: bool = False,
    gh_bin: str = "gh",
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> GitHubIssueResult:
    safe_title = sanitize_title_slot(title, max_chars=120)
    label_arg = ",".join(label for label in labels if label)
    base_command = (
        gh_bin,
        "issue",
        "create",
        "--repo",
        repo,
        "--title",
        safe_title,
        "--label",
        label_arg,
    )

    if dry_run:
        return GitHubIssueResult(
            ok=True,
            command=(*base_command, "--body-file", "<generated-report.md>"),
            dry_run=True,
        )

    body_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".md", delete=False) as body_file:
            body_file.write(body)
            body_path = Path(body_file.name)
        command = (*base_command, "--body-file", str(body_path))
        completed = runner(
            command,
            capture_output=True,
            text=True,
            env=gh_subprocess_env(),
            timeout=30,
            check=False,
        )
    except FileNotFoundError as exc:
        return GitHubIssueResult(
            ok=False,
            command=base_command,
            returncode=127,
            reason=f"gh CLI not found: {exc}",
        )
    except OSError as exc:
        return GitHubIssueResult(ok=False, command=base_command, returncode=1, reason=str(exc))
    finally:
        if body_path is not None:
            body_path.unlink(missing_ok=True)

    if completed.returncode != 0:
        reason = (completed.stderr or completed.stdout or "gh issue create failed").strip()
        return GitHubIssueResult(
            ok=False,
            command=command,
            returncode=int(completed.returncode),
            reason=reason,
        )
    return GitHubIssueResult(
        ok=True,
        issue_url=(completed.stdout or "").strip(),
        command=command,
        returncode=0,
    )


__all__ = [
    "DEFAULT_FEEDBACK_REPO",
    "FEEDBACK_REPO_ENV",
    "FeedbackKind",
    "GitHubIssueResult",
    "build_feedback_body",
    "create_github_issue",
    "default_labels",
    "default_repo",
]
