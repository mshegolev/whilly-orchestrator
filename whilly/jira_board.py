"""Jira-side lifecycle sync — transition tickets as whilly tasks move statuses.

Mirrors :mod:`whilly.project_board` for GitHub Projects v2 but talks to Jira's
REST API. Used as a ``TaskManager.on_status_change`` callback (wired from
``whilly.cli._wire_project_board_sync``): when a Jira-sourced task's status
transitions, this client picks the matching Jira transition and performs it.

Soft-fails on any HTTP / auth error. Never raises into the orchestrator loop.

Requires ``[jira]`` config (server_url + username + token) — the same section
used for issue-close automation, so setup is one place for both directions.
"""

from __future__ import annotations

import base64
import json
import logging
import re
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from whilly.sources.jira import JiraAuth

log = logging.getLogger("whilly")


# Whilly internal status → target Jira status *name* (case-insensitive match on
# ``transitions[*].to.name``). Override per-project via the ``[jira].status_mapping``
# section if your workflow uses different names.
DEFAULT_JIRA_STATUS_MAPPING: dict[str, str] = {
    "pending": "To Do",
    "in_progress": "In Progress",
    "done": "In Review",
    "merged": "Done",
    "failed": "Failed",
    "skipped": "Cancelled",
    "blocked": "Blocked",
    "human_loop": "Waiting for Customer",
}


class JiraBoardClient:
    """Thin REST client that drives a Jira ticket through statuses."""

    def __init__(self, auth: JiraAuth, status_mapping: dict[str, str] | None = None) -> None:
        self.auth = auth
        self.status_mapping = dict(DEFAULT_JIRA_STATUS_MAPPING)
        if status_mapping:
            self.status_mapping.update(status_mapping)

    # ── Construction from layered config ─────────────────────────────────────

    @classmethod
    def from_config(cls, config: Any) -> JiraBoardClient | None:
        """Build a client if ``[jira]`` is configured and board-sync is enabled.

        Enables itself when ``[jira].enable_board_sync`` is truthy OR ``enabled``
        is truthy (re-uses the existing ``enabled`` toggle). Returns None when
        auth is missing or the user has explicitly disabled it.
        """
        try:
            from whilly.config import get_toml_section
        except ImportError:
            return None
        section = get_toml_section("jira")
        if section.get("enable_board_sync") is False:
            return None
        if section.get("enabled") is False:
            # Respect the existing [jira].enabled toggle that also guards auto-close.
            return None
        try:
            auth = JiraAuth.from_config()
        except RuntimeError:
            return None
        mapping = section.get("status_mapping") if isinstance(section.get("status_mapping"), dict) else None
        return cls(auth, status_mapping=mapping)

    # ── Public surface ───────────────────────────────────────────────────────

    def set_issue_status(self, key: str, status_name: str) -> bool:
        """Transition the Jira ticket *key* to *status_name*. Soft-fails on any error.

        Returns ``True`` on successful transition, ``False`` when the transition
        wasn't available, the ticket doesn't exist, or any HTTP error occurred.
        """
        try:
            available = self._fetch_transitions(key)
        except Exception as exc:
            log.warning("Jira transitions fetch failed for %s: %s", key, exc)
            return False

        target_id: str | None = None
        wanted = status_name.strip().lower()
        for transition in available:
            name = ((transition.get("to") or {}).get("name") or "").strip().lower()
            if name == wanted:
                target_id = str(transition.get("id") or "")
                break
        if not target_id:
            log.info(
                "Jira transition %r unavailable for %s — got %s",
                status_name,
                key,
                sorted({((t.get("to") or {}).get("name") or "") for t in available}),
            )
            return False

        try:
            self._post_transition(key, target_id)
        except Exception as exc:
            log.warning("Jira transition %s → %r failed: %s", key, status_name, exc)
            return False
        log.info("Jira: %s → %r", key, status_name)
        return True

    def set_task_status(self, task: Any, whilly_status: str) -> bool:
        """Translate whilly status → Jira transition and apply for the task's key.

        Task's Jira key is read from ``task.jira_key`` when present, else
        extracted from ``task.id`` via the ``JIRA-<KEY>`` prefix.
        """
        mapped = self.status_mapping.get(whilly_status)
        if not mapped:
            return False
        key = _extract_jira_key(task)
        if not key:
            return False
        return self.set_issue_status(key, mapped)

    # ── Internals ────────────────────────────────────────────────────────────

    def _fetch_transitions(self, key: str) -> list[dict]:
        data = self._api("GET", f"/rest/api/3/issue/{key}/transitions")
        return list(data.get("transitions") or [])

    def _post_transition(self, key: str, transition_id: str) -> None:
        self._api(
            "POST",
            f"/rest/api/3/issue/{key}/transitions",
            payload={"transition": {"id": transition_id}},
            expect_empty=True,
        )

    def _api(
        self,
        method: str,
        path: str,
        *,
        payload: dict | None = None,
        timeout: int = 15,
        expect_empty: bool = False,
    ) -> dict:
        url = f"{self.auth.server_url}{path}"
        header = base64.b64encode(f"{self.auth.username}:{self.auth.token}".encode("utf-8")).decode("ascii")
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        req = Request(
            url,
            data=data,
            headers={
                "Authorization": f"Basic {header}",
                "Accept": "application/json",
                **({"Content-Type": "application/json"} if data else {}),
            },
            method=method,
        )
        try:
            with urlopen(req, timeout=timeout) as resp:
                body = resp.read().decode("utf-8")
                if expect_empty or not body:
                    return {}
                return json.loads(body or "{}")
        except HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")[:500] if exc.fp else ""
            raise RuntimeError(f"Jira {method} {path} failed: HTTP {exc.code} — {body}") from exc
        except URLError as exc:
            raise RuntimeError(f"Jira {method} {path} network error: {exc.reason}") from exc


def _extract_jira_key(task: Any) -> str | None:
    """Return the Jira key for a task, if derivable.

    Priority: explicit ``task.jira_key`` attribute / dict entry, then the
    ``JIRA-<KEY>`` prefix in ``task.id``.
    """
    jira_key = getattr(task, "jira_key", None)
    if isinstance(jira_key, str) and jira_key.strip():
        return jira_key.strip()
    if isinstance(task, dict) and isinstance(task.get("jira_key"), str):
        return task["jira_key"].strip()
    task_id = getattr(task, "id", "") or ""
    match = re.match(r"^JIRA-([A-Z][A-Z0-9]+-\d+)$", task_id)
    return match.group(1) if match else None


__all__ = ["JiraBoardClient", "DEFAULT_JIRA_STATUS_MAPPING"]
