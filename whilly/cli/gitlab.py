"""``whilly gitlab`` command surface.

Provides a ``smoke`` subcommand that performs read-only, token-authenticated
checks against a live GitLab instance:

  1. ``auth``          — GET /api/v4/user (verifies Bearer token is valid)
  2. ``project_access`` — GET /api/v4/projects/{encoded path}
  3. ``repo_hint``      — verify returned project matches the requested repo URL

Results are accumulated in a :class:`~whilly.cli.smoke.SmokeReport` and written
as a redacted JSON file under ``whilly_logs/smoke/``.

Usage::

    whilly gitlab smoke --repo-url https://gitlab.example.com/group/repo

Exit codes: 0 = all checks pass, 1 = a check failed, 2 = configuration missing.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from typing import Any
from urllib.error import HTTPError, URLError

from whilly.cli.smoke import (
    EXIT_CHECK_FAILED,
    EXIT_CONFIG_MISSING,
    EXIT_OK,
    SmokeReport,
    _redact_url,
    _smoke_report_dir,
    write_smoke_report,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_REMOTE_HOST_RE = re.compile(r"(?:https?://|git@)([^/:]+)", re.IGNORECASE)

# Type alias for the injectable HTTP getter.
GitLabGetter = Callable[..., dict[str, Any]]


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


def build_gitlab_parser() -> argparse.ArgumentParser:
    """Return the argument parser for ``whilly gitlab``."""
    parser = argparse.ArgumentParser(
        prog="whilly gitlab",
        description="Smoke-test live GitLab integration (auth, project access, repo-hint).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_smoke = sub.add_parser(
        "smoke",
        help=("Run read-only GitLab smoke checks (auth, project access, repo-hint) and write a redacted JSON report."),
    )
    p_smoke.add_argument(
        "--repo-url",
        required=True,
        help="Full GitLab repository URL, e.g. https://gitlab.example.com/group/repo.",
    )
    p_smoke.add_argument(
        "--timeout",
        type=int,
        default=15,
        help="Per-request HTTP timeout in seconds. Default: 15.",
    )
    p_smoke.add_argument(
        "--json",
        action="store_true",
        help="Print the full report payload as JSON.",
    )
    return parser


# ---------------------------------------------------------------------------
# Config state
# ---------------------------------------------------------------------------


def _resolve_gitlab_config_state(
    env: Mapping[str, str],
    host: str,
) -> tuple[str, str]:
    """Return ``(url, token)`` from *env* for the given *host*.

    URL: ``GITLAB_URL`` → ``WHILLY_GITLAB_URL`` (trailing slash stripped).
    Any ``user:pass@`` userinfo embedded in the URL is stripped at the source
    so credentials can never reach error messages, reports, or stdout (CR-01).
    Token precedence: ``GITLAB_TOKEN`` → ``GITLAB_API_TOKEN`` →
    ``WHILLY_GITLAB_API_TOKEN`` → ``glab config get token`` CLI fallback.

    Both values may be empty strings when not configured.
    """
    url = _redact_url((env.get("GITLAB_URL") or env.get("WHILLY_GITLAB_URL") or "").strip().rstrip("/"))

    token = (env.get("GITLAB_TOKEN") or env.get("GITLAB_API_TOKEN") or env.get("WHILLY_GITLAB_API_TOKEN") or "").strip()

    if not token:
        # glab CLI fallback — mirrors whilly/sinks/gitlab_mr.py:_resolve_gitlab_token
        try:
            import subprocess  # noqa: PLC0415 — lazy import; only called when env is absent

            result = subprocess.run(
                ["glab", "config", "get", "token", "-h", host],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            if result.returncode == 0 and result.stdout.strip():
                token = result.stdout.strip()
        except Exception:  # noqa: BLE001
            pass

    return url, token


# ---------------------------------------------------------------------------
# HTTP client
# ---------------------------------------------------------------------------


def _gitlab_get(url: str, *, token: str, timeout: int = 15) -> dict[str, Any]:
    """GET *url* with a ``Bearer`` token and return parsed JSON.

    Converts :class:`~urllib.error.HTTPError` and
    :class:`~urllib.error.URLError` into :class:`RuntimeError` so callers
    never see a raw urllib traceback. Error messages embed the *redacted*
    URL only — any ``user:pass@`` userinfo is stripped so credentials can
    never flow into check hints, reports, or stdout (CR-01).
    """
    safe_url = _redact_url(url)
    req = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 — operator-supplied URL
            raw = resp.read().decode("utf-8", errors="replace")
            try:
                return json.loads(raw or "{}")
            except json.JSONDecodeError as exc:
                content_type = resp.headers.get("content-type", "")
                hint = "received HTML/SSO page" if "<html" in raw.lower() else "response body is not JSON"
                raise RuntimeError(
                    f"GitLab GET {safe_url!r} returned non-JSON response: content-type={content_type!r}, {hint}"
                ) from exc
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")[:500] if exc.fp else ""
        raise RuntimeError(f"GitLab GET {safe_url!r} failed: HTTP {exc.code} — {body}") from exc
    except URLError as exc:
        raise RuntimeError(f"GitLab GET {safe_url!r} network error: {exc.reason}") from exc


# ---------------------------------------------------------------------------
# Project path helpers
# ---------------------------------------------------------------------------


def _extract_host_from_url(repo_url: str) -> str:
    """Return the hostname extracted from *repo_url*, or an empty string."""
    m = _REMOTE_HOST_RE.search(repo_url)
    return m.group(1) if m else ""


def _resolve_project_path(repo_url: str) -> str:
    """Extract and URL-encode the ``namespace/repo`` path from *repo_url*.

    Strips the scheme, host, leading slash, and trailing ``.git``.
    Path traversal components (``..``) are removed before encoding so that
    a malicious input cannot reach arbitrary API paths (T-19-06).
    """
    try:
        parsed = urllib.parse.urlsplit(repo_url)
    except Exception:  # noqa: BLE001
        return urllib.parse.quote(repo_url.lstrip("/"), safe="")

    path = parsed.path.strip("/")
    # Strip trailing .git
    if path.endswith(".git"):
        path = path[:-4].rstrip("/")

    # Normalise: remove any .. components (traversal guard)
    safe_parts = [p for p in path.split("/") if p and p != ".."]
    safe_path = "/".join(safe_parts)

    return urllib.parse.quote(safe_path, safe="")


def _matches_repo(project: dict[str, Any], repo_path_encoded: str) -> bool:
    """Return True when *project* matches the requested repository.

    Compares both ``path_with_namespace`` and the basename of
    ``http_url_to_repo`` against the decoded requested path (case-insensitive).
    """
    requested = urllib.parse.unquote(repo_path_encoded).lower()

    pwn = (project.get("path_with_namespace") or "").lower()
    http_url = (project.get("http_url_to_repo") or "").rstrip("/")
    if http_url.endswith(".git"):
        http_url = http_url[:-4]
    # Extract path component from http_url_to_repo
    try:
        http_path = urllib.parse.urlsplit(http_url).path.strip("/").lower()
    except Exception:  # noqa: BLE001
        http_path = ""

    return requested == pwn or requested == http_path


# ---------------------------------------------------------------------------
# Core smoke action
# ---------------------------------------------------------------------------


def _run_gitlab_smoke(
    args: argparse.Namespace,
    *,
    gitlab_getter: GitLabGetter,
    env: Mapping[str, str],
) -> int:
    """Run read-only GitLab smoke checks and write a redacted report.

    Returns an exit code: 0 all pass, 1 a check failed, 2 config missing.
    """
    host = _extract_host_from_url(args.repo_url) or "gitlab.example.com"
    url, token = _resolve_gitlab_config_state(env, host)

    if not url or not token:
        missing: list[str] = []
        if not url:
            missing.append("GITLAB_URL")
        if not token:
            missing.append("GITLAB_TOKEN")
        missing_str = ", ".join(missing)
        print(
            f"whilly gitlab smoke: missing configuration: {missing_str}.\n"
            f"  Set {missing_str} env vars and pass --repo-url with the full repository URL.",
            file=sys.stderr,
        )
        return EXIT_CONFIG_MISSING

    api_base = f"{url}/api/v4"
    report = SmokeReport(kind="gitlab")
    project_data: dict[str, Any] = {}

    # ------------------------------------------------------------------
    # Check 1: auth (GET /api/v4/user)
    # ------------------------------------------------------------------
    try:
        gitlab_getter(f"{api_base}/user", token=token, timeout=args.timeout)
        report.add_check("auth", passed=True)
    except Exception as exc:  # noqa: BLE001
        report.add_check(
            "auth",
            passed=False,
            hint=f"Verify GITLAB_TOKEN is valid for {_redact_url(url)}. Detail: {exc}",
        )

    # ------------------------------------------------------------------
    # Check 2: project_access (GET /api/v4/projects/{encoded path})
    # ------------------------------------------------------------------
    repo_path_encoded = _resolve_project_path(args.repo_url)
    try:
        data = gitlab_getter(f"{api_base}/projects/{repo_path_encoded}", token=token, timeout=args.timeout)
        if isinstance(data, dict) and data.get("id"):
            project_data = data
            report.add_check("project_access", passed=True)
        else:
            report.add_check(
                "project_access",
                passed=False,
                hint=(
                    f"Project API response missing 'id' — verify the repo path "
                    f"in --repo-url ({urllib.parse.unquote(repo_path_encoded)})."
                ),
            )
    except Exception as exc:  # noqa: BLE001
        report.add_check(
            "project_access",
            passed=False,
            hint=(
                f"Cannot access project {urllib.parse.unquote(repo_path_encoded)!r} "
                f"on {_redact_url(url)}. Detail: {exc}"
            ),
        )

    # ------------------------------------------------------------------
    # Check 3: repo_hint (path_with_namespace / http_url_to_repo match)
    # ------------------------------------------------------------------
    try:
        if project_data:
            if _matches_repo(project_data, repo_path_encoded):
                report.add_check("repo_hint", passed=True)
            else:
                requested = urllib.parse.unquote(repo_path_encoded)
                actual_pwn = project_data.get("path_with_namespace", "")
                report.add_check(
                    "repo_hint",
                    passed=False,
                    hint=(
                        f"Requested repo path {requested!r} does not match project path_with_namespace={actual_pwn!r}."
                    ),
                )
        else:
            report.add_check(
                "repo_hint",
                passed=False,
                hint="Skipped: project_access check did not return a valid project.",
            )
    except Exception as exc:  # noqa: BLE001
        report.add_check(
            "repo_hint",
            passed=False,
            hint=f"repo_hint check error: {exc}",
        )

    # ------------------------------------------------------------------
    # Build and write the redacted report payload
    # ------------------------------------------------------------------
    payload = report.to_payload()
    payload["target"] = {
        "host": _redact_url(url),
        "repo_path": urllib.parse.unquote(repo_path_encoded),
    }

    report_path = write_smoke_report(_smoke_report_dir(), "gitlab", payload)

    # ------------------------------------------------------------------
    # Print human-readable summary (or full JSON)
    # ------------------------------------------------------------------
    summary = payload["summary"]
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        status = "PASS" if summary["all_passed"] else "FAIL"
        print(
            f"whilly gitlab smoke: {status} "
            f"host={_redact_url(url)} "
            f"repo={urllib.parse.unquote(repo_path_encoded)} "
            f"passed={summary['passed']}/{summary['total']}"
        )
        for check in payload["checks"]:
            icon = "OK" if check["passed"] else "FAIL"
            line = f"  [{icon}] {check['name']}"
            if not check["passed"] and check.get("hint"):
                line += f": {check['hint']}"
            print(line)

    print(f"  report: {report_path}", file=sys.stderr)

    return EXIT_OK if report.all_passed else EXIT_CHECK_FAILED


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def run_gitlab_command(
    argv: Sequence[str],
    *,
    gitlab_getter: GitLabGetter | None = None,
    environ: Mapping[str, str] | None = None,
) -> int:
    """Run ``whilly gitlab`` with *argv* excluding the top-level command name.

    :param gitlab_getter: Injectable HTTP getter for testing (defaults to
        :func:`_gitlab_get`).
    :param environ: Injectable environment mapping (defaults to
        :data:`os.environ`).
    """
    parser = build_gitlab_parser()
    try:
        args = parser.parse_args(list(argv))
    except SystemExit as exc:
        return int(exc.code) if isinstance(exc.code, int) else EXIT_CONFIG_MISSING

    import os  # noqa: PLC0415

    effective_env: Mapping[str, str] = environ if environ is not None else os.environ
    effective_getter: GitLabGetter = gitlab_getter if gitlab_getter is not None else _gitlab_get

    if args.command == "smoke":
        return _run_gitlab_smoke(args, gitlab_getter=effective_getter, env=effective_env)

    parser.print_usage(sys.stderr)
    return EXIT_CHECK_FAILED


__all__ = [
    "build_gitlab_parser",
    "run_gitlab_command",
    "_run_gitlab_smoke",
    "_gitlab_get",
    "_resolve_gitlab_config_state",
    "_resolve_project_path",
]
