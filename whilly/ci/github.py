"""One-shot GitHub CI polling adapter backed by the ``gh`` CLI."""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections.abc import Mapping
from typing import Any

from whilly.ci.models import CI_PROVIDER_GITHUB, CICheckSummary, CIPollResult, CIPollSpec
from whilly.gh_utils import gh_subprocess_env

_HASH_PR_TARGET_RE = re.compile(r"^ci://github/(?P<owner>[^/]+)/(?P<repo>[^#]+)#pr-(?P<number>\d+)$")
_PATH_PR_TARGET_RE = re.compile(r"^ci://github/(?P<owner>[^/]+)/(?P<repo>.+)/pull/(?P<number>\d+)$")


class GitHubCIPollAdapter:
    """Run one bounded GitHub PR status probe and return explicit CI evidence."""

    def __init__(self, *, gh_bin: str = "gh", timeout_s: float = 60.0) -> None:
        self.gh_bin = gh_bin
        self.timeout_s = timeout_s

    async def __call__(self, spec: CIPollSpec) -> CIPollResult:
        started = time.monotonic()
        parsed = _parse_target(spec.target)
        if parsed is None:
            return _unavailable_result(
                spec,
                started=started,
                reason="github_target_unparseable",
                duration_s=time.monotonic() - started,
            )

        owner, repo, pr_number = parsed
        try:
            returncode, stdout, stderr = await self._probe(spec, owner, repo, pr_number)
        except asyncio.TimeoutError:
            return _timed_out_result(spec, started=started, duration_s=time.monotonic() - started)
        except OSError as exc:
            return _unavailable_result(
                spec,
                started=started,
                reason=f"github_probe_unavailable: {exc}",
                duration_s=time.monotonic() - started,
            )

        duration_s = time.monotonic() - started
        if returncode != 0:
            combined = f"{stdout}\n{stderr}".lower()
            if "not authenticated" in combined or "auth" in combined or "login" in combined:
                return _unauthenticated_result(spec, started=started, duration_s=duration_s)
            return _unavailable_result(
                spec,
                started=started,
                reason="github_probe_failed",
                duration_s=duration_s,
            )

        try:
            payload = json.loads(stdout or "{}")
        except json.JSONDecodeError:
            return _unavailable_result(
                spec,
                started=started,
                reason="github_response_invalid_json",
                duration_s=duration_s,
            )
        if not isinstance(payload, Mapping):
            return _unavailable_result(
                spec,
                started=started,
                reason="github_response_not_object",
                duration_s=duration_s,
            )

        checks = tuple(_check_from_rollup_entry(entry) for entry in _status_rollup(payload))
        checks = tuple(check for check in checks if check is not None)
        state, conclusion, reason = _overall_status(checks)
        return CIPollResult(
            name=spec.name,
            provider=CI_PROVIDER_GITHUB,
            target=spec.target,
            state=state,
            conclusion=conclusion,
            required=spec.required,
            attempts=1,
            max_attempts=spec.max_attempts,
            timeout_s=spec.timeout_s,
            duration_s=duration_s,
            details_url=_string_or_none(payload.get("url")),
            checks=checks,
            reason=reason,
        )

    async def _probe(self, spec: CIPollSpec, owner: str, repo: str, pr_number: int) -> tuple[int, str, str]:
        timeout_s = spec.timeout_s if spec.timeout_s > 0 else self.timeout_s
        proc = await asyncio.create_subprocess_exec(
            self.gh_bin,
            "pr",
            "view",
            str(pr_number),
            "--repo",
            f"{owner}/{repo}",
            "--json",
            "state,statusCheckRollup,url",
            env=gh_subprocess_env(),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout_bytes, stderr_bytes = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
        return (
            int(proc.returncode or 0),
            stdout_bytes.decode("utf-8", errors="replace"),
            stderr_bytes.decode("utf-8", errors="replace"),
        )


def _parse_target(target: str) -> tuple[str, str, int] | None:
    match = _HASH_PR_TARGET_RE.match(target) or _PATH_PR_TARGET_RE.match(target)
    if match is None:
        return None
    return match.group("owner"), match.group("repo"), int(match.group("number"))


def _status_rollup(payload: Mapping[str, Any]) -> tuple[Any, ...]:
    rollup = payload.get("statusCheckRollup")
    if isinstance(rollup, list):
        return tuple(rollup)
    return ()


def _check_from_rollup_entry(entry: Any) -> CICheckSummary | None:
    if not isinstance(entry, Mapping):
        return None
    name = _string_or_none(entry.get("name")) or _string_or_none(entry.get("context")) or "unknown"
    state = _normal_check_state(entry.get("state") or entry.get("status"))
    conclusion = _normal_conclusion(entry.get("conclusion"))
    details_url = (
        _string_or_none(entry.get("detailsUrl"))
        or _string_or_none(entry.get("details_url"))
        or _string_or_none(entry.get("url"))
    )
    return CICheckSummary(name=name, state=state, conclusion=conclusion, details_url=details_url)


def _overall_status(checks: tuple[CICheckSummary, ...]) -> tuple[str, str, str]:
    if not checks:
        return "unknown", "unknown", "github_status_rollup_missing"
    if any(check.state != "completed" for check in checks):
        return "in_progress", "unknown", "github_status_rollup_incomplete"
    conclusions = {check.conclusion for check in checks}
    if conclusions == {"success"}:
        return "completed", "success", ""
    if "failure" in conclusions:
        return "completed", "failure", "github_status_rollup_failed"
    if "cancelled" in conclusions:
        return "completed", "cancelled", "github_status_rollup_cancelled"
    if "timed_out" in conclusions:
        return "completed", "timed_out", "github_status_rollup_timed_out"
    return "completed", "unknown", "github_status_rollup_unknown"


def _normal_check_state(value: Any) -> str:
    raw = str(value or "").strip().lower()
    if raw in {"completed", "success", "failure", "cancelled", "skipped"}:
        return "completed"
    if raw in {"queued", "pending", "waiting", "requested"}:
        return "queued"
    if raw in {"in_progress", "in progress", "running"}:
        return "in_progress"
    return raw or "unknown"


def _normal_conclusion(value: Any) -> str:
    raw = str(value or "").strip().lower()
    if raw in {"success", "failure", "cancelled", "timed_out", "skipped"}:
        return raw
    if raw in {"neutral", "startup_failure", "action_required"}:
        return "failure"
    return raw or "unknown"


def _string_or_none(value: Any) -> str | None:
    if isinstance(value, str) and value:
        return value
    return None


def _unavailable_result(spec: CIPollSpec, *, started: float, reason: str, duration_s: float) -> CIPollResult:
    return CIPollResult(
        name=spec.name,
        provider=CI_PROVIDER_GITHUB,
        target=spec.target,
        state="unavailable",
        conclusion="unavailable",
        required=spec.required,
        attempts=1,
        max_attempts=spec.max_attempts,
        timeout_s=spec.timeout_s,
        duration_s=duration_s or time.monotonic() - started,
        unavailable=True,
        reason=reason,
    )


def _unauthenticated_result(spec: CIPollSpec, *, started: float, duration_s: float) -> CIPollResult:
    return CIPollResult(
        name=spec.name,
        provider=CI_PROVIDER_GITHUB,
        target=spec.target,
        state="unavailable",
        conclusion="unavailable",
        required=spec.required,
        attempts=1,
        max_attempts=spec.max_attempts,
        timeout_s=spec.timeout_s,
        duration_s=duration_s or time.monotonic() - started,
        unauthenticated=True,
        reason="github_authentication_required",
    )


def _timed_out_result(spec: CIPollSpec, *, started: float, duration_s: float) -> CIPollResult:
    return CIPollResult(
        name=spec.name,
        provider=CI_PROVIDER_GITHUB,
        target=spec.target,
        state="unknown",
        conclusion="timed_out",
        required=spec.required,
        attempts=1,
        max_attempts=spec.max_attempts,
        timeout_s=spec.timeout_s,
        duration_s=duration_s or time.monotonic() - started,
        timed_out=True,
        reason="github_probe_timed_out",
    )


__all__ = ["GitHubCIPollAdapter"]
