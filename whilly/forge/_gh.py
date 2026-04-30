"""``gh`` CLI shellouts for :mod:`whilly.forge.intake` (TASK-108a).

Tiny wrapper module that owns every ``subprocess.run`` against the
``gh`` CLI: fetching an issue payload (``gh issue view``) and flipping
labels (``gh issue edit``). Pulled out of :mod:`whilly.forge.intake`
so unit tests can monkeypatch ``_run_gh`` in one place and so the
intake pipeline reads as plain orchestration without subprocess
plumbing.

Auth resolution
---------------
Always invokes ``gh`` with ``env=gh_subprocess_env()`` (per the
mission's hard-rule in ``AGENTS.md`` — no ``os.environ`` blind
inheritance). The helper in :mod:`whilly.gh_utils` decides which
``GITHUB_TOKEN`` / ``GH_TOKEN`` value to expose to the subprocess.

JSON field set
--------------
``gh issue view`` is invoked with a *pinned* ``--json`` field set
(``number,title,body,labels,comments,state,url``) so the test fixture
and the production caller stay in sync — bumping the field set must
update both the constant here and the stub.

Error shape
-----------
Two named exceptions surface to the intake pipeline:

* :class:`GHIssueNotFoundError` — ``gh`` returned exit 1 with a
  ``"GraphQL: Could not resolve"`` (or ``"Could not find"``) signature
  in stderr. The intake pipeline maps this to ``EXIT_USER_ERROR``
  (VAL-FORGE-009).
* :class:`GHCLIError` — any other non-zero exit, including network
  / transport errors (``"could not connect to api.github.com"``).
  The intake pipeline surfaces it on stderr verbatim and exits
  non-zero without writing a plan (VAL-FORGE-010).
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from typing import Any, Final

from whilly.gh_utils import gh_subprocess_env

logger = logging.getLogger(__name__)


#: Pinned ``--json`` field set for ``gh issue view`` (VAL-FORGE-003).
#: Tests assert this exact comma-separated string is passed; production
#: callers and the fake_gh.sh stub must keep this set in lockstep.
GH_ISSUE_VIEW_JSON_FIELDS: Final[str] = "number,title,body,labels,comments,state,url"

#: Default subprocess timeout for any ``gh`` invocation. 30 s is well
#: above the typical ``gh issue view`` round-trip (sub-second on a
#: warm token) and gives the CLI room for one full retry on a cold
#: connection without the orchestration loop noticing.
DEFAULT_GH_TIMEOUT_SECONDS: Final[float] = 30.0


class GHCLIError(RuntimeError):
    """``gh`` invocation failed (non-zero exit) for any reason.

    ``returncode`` and ``stderr`` are preserved so the caller can
    distinguish transient errors (network, rate-limit) from permanent
    ones (404, malformed args).
    """

    def __init__(self, message: str, *, returncode: int, stderr: str) -> None:
        super().__init__(message)
        self.returncode = returncode
        self.stderr = stderr


class GHIssueNotFoundError(GHCLIError):
    """``gh issue view`` reported the issue does not exist (404 / GraphQL miss).

    Distinct from :class:`GHCLIError` so the intake pipeline can map it
    to ``EXIT_USER_ERROR`` (1) and print a friendly "issue not found"
    message instead of dumping ``gh``'s GraphQL error verbatim.
    """


class GHCLIMissingError(RuntimeError):
    """``gh`` is not on the operator's ``PATH`` (VAL-FORGE-008)."""


def _run_gh(
    args: list[str],
    *,
    timeout: float = DEFAULT_GH_TIMEOUT_SECONDS,
) -> subprocess.CompletedProcess[str]:
    """Run ``gh <args>`` with the resolved subprocess env, capture stdout/stderr.

    Single-source-of-truth for every ``gh`` invocation in this module:
    keeps the ``env=gh_subprocess_env()`` discipline centralised so a
    future caller can't accidentally bypass auth resolution by reaching
    around the helper.

    Tests monkeypatch this function directly (``forge._gh._run_gh``) to
    return a canned :class:`subprocess.CompletedProcess`. The
    ``shutil.which("gh")`` precondition check sits here too so a
    monkeypatched ``shutil.which`` returning ``None`` exercises the
    "gh CLI absent" path without needing a separate seam.

    Raises:
        GHCLIMissingError: ``gh`` is not on PATH.
    """
    gh_path = shutil.which("gh")
    if gh_path is None:
        raise GHCLIMissingError(
            "gh CLI is not on PATH; install it via `brew install gh` or see "
            "https://cli.github.com for platform-specific install instructions."
        )
    cmd = [gh_path, *args]
    env = gh_subprocess_env()
    logger.debug("forge._gh: running %s", cmd)
    return subprocess.run(  # noqa: S603 — args are constants + validated owner/repo/N
        cmd,
        capture_output=True,
        text=True,
        env=env,
        timeout=timeout,
        check=False,
    )


def fetch_issue(owner: str, repo: str, number: int) -> dict[str, Any]:
    """Return the JSON payload for ``owner/repo/<number>`` via ``gh issue view``.

    Calls ``gh issue view <number> --repo <owner>/<repo> --json
    <fields>`` exactly once with the pinned :data:`GH_ISSUE_VIEW_JSON_FIELDS`.
    Returns the parsed JSON body.

    Raises:
        GHIssueNotFoundError: ``gh`` exited 1 with a "could not resolve"
            stderr signature.
        GHCLIError: any other non-zero exit (network errors, auth
            failures, malformed args).
        GHCLIMissingError: ``gh`` is not on PATH.
    """
    args = [
        "issue",
        "view",
        str(number),
        "--repo",
        f"{owner}/{repo}",
        "--json",
        GH_ISSUE_VIEW_JSON_FIELDS,
    ]
    result = _run_gh(args)
    if result.returncode != 0:
        stderr = result.stderr or ""
        # GitHub's GraphQL API returns "Could not resolve to an Issue"
        # (current text) or "Could not find" (older variant) on a
        # missing issue / wrong number. Match both — case-insensitive
        # so a future GH CLI rewrite of the message doesn't silently
        # demote a 404 to a generic CLI error.
        lowered = stderr.lower()
        if result.returncode == 1 and ("could not resolve" in lowered or "could not find" in lowered):
            raise GHIssueNotFoundError(
                f"GitHub issue {owner}/{repo}#{number} not found",
                returncode=result.returncode,
                stderr=stderr,
            )
        raise GHCLIError(
            f"gh issue view failed (exit={result.returncode}): {stderr.strip() or '<no stderr>'}",
            returncode=result.returncode,
            stderr=stderr,
        )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise GHCLIError(
            f"gh issue view returned invalid JSON: {exc}",
            returncode=0,
            stderr=result.stderr,
        ) from exc
    if not isinstance(payload, dict):
        raise GHCLIError(
            f"gh issue view returned non-object JSON: {type(payload).__name__}",
            returncode=0,
            stderr=result.stderr,
        )
    return payload


def flip_label(
    owner: str,
    repo: str,
    number: int,
    *,
    add: str,
    remove: str,
) -> None:
    """Run ``gh issue edit <N> --repo X/Y --add-label A --remove-label B``.

    Single combined invocation per VAL-FORGE-006: both flags on the same
    ``gh`` call so the label transition is atomic from GitHub's
    perspective. Order of the flags isn't pinned; the test asserts
    *both* labels are present and the labels are exact literals.

    Raises:
        GHCLIError: ``gh`` exited non-zero. Caller decides whether to
            surface as a hard failure.
    """
    args = [
        "issue",
        "edit",
        str(number),
        "--repo",
        f"{owner}/{repo}",
        "--remove-label",
        remove,
        "--add-label",
        add,
    ]
    result = _run_gh(args)
    if result.returncode != 0:
        raise GHCLIError(
            f"gh issue edit failed (exit={result.returncode}): {(result.stderr or '').strip() or '<no stderr>'}",
            returncode=result.returncode,
            stderr=result.stderr or "",
        )


__all__ = [
    "DEFAULT_GH_TIMEOUT_SECONDS",
    "GHCLIError",
    "GHCLIMissingError",
    "GHIssueNotFoundError",
    "GH_ISSUE_VIEW_JSON_FIELDS",
    "fetch_issue",
    "flip_label",
]
