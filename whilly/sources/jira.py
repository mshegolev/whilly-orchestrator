"""Jira source adapter — pull issues into whilly's JSON plan.

Reads one Jira issue (by key) via the Atlassian REST API and writes it as a
single whilly ``Task`` through the same idempotent :func:`merge_into_plan`
pipeline the GitHub source uses. Auth comes from the ``[jira]`` config section
(or the ``JIRA_SERVER_URL`` / ``JIRA_USERNAME`` / ``JIRA_API_TOKEN`` env vars
kept for back-compat).

Public surface mirrors :mod:`whilly.sources.github_issues`::

    from whilly.sources.jira import fetch_single_jira_issue
    plan_path, stats = fetch_single_jira_issue("ABC-123", out_path="tasks.json")

CLI entry point: ``whilly jira import ABC-123 [--import-db|--run]``.
The legacy ``whilly --from-jira ABC-123 [--go]`` form is still routed there.

Uses only stdlib (``urllib.request``) so Jira access works without ``requests``
being installed.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import ssl
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from json import JSONDecodeError
from urllib.error import HTTPError, URLError
from urllib.request import HTTPSHandler, ProxyHandler, Request, build_opener, urlopen

from whilly.security.prompt_sanitizer import sanitize_external_text
from whilly.sources.github_issues import FetchStats, GitHubIssuesSource, merge_into_plan

log = logging.getLogger("whilly")


def _bypass_system_proxy() -> bool:
    """Return True if the operator opted into urllib bypass via env."""
    return (os.environ.get("WHILLY_JIRA_NO_PROXY") or "").strip() == "1"


def _jira_opener_no_proxy(context=None):
    """Build a urllib opener with empty ProxyHandler — ignores env + macOS sysconfig.

    The optional SSL ``context`` argument flows through to ``HTTPSHandler`` so
    callers that need ``JIRA_VERIFY_SSL=false`` or a custom CA file still get
    their SSL settings honoured even on the no-proxy path.
    """
    https_handler = HTTPSHandler(context=context) if context is not None else HTTPSHandler()
    return build_opener(ProxyHandler({}), https_handler)


# ── Public config resolution ──────────────────────────────────────────────────


@dataclass
class JiraAuth:
    """Resolved Jira server URL + credentials, from whilly.toml or env."""

    server_url: str
    username: str
    token: str
    verify_ssl: bool = True
    ca_file: str = ""
    auth_scheme: str = "basic"
    api_version: str = "3"

    @classmethod
    def from_config(cls) -> JiraAuth:
        """Pull server + creds from config layers; ``token`` honours secret schemes."""
        server = ""
        username = ""
        token = ""
        verify_ssl = True
        ca_file = ""
        auth_scheme = "basic"
        api_version = "3"
        try:
            from whilly.config import get_toml_section
            from whilly.secrets import resolve as resolve_secret

            section = get_toml_section("jira")
            server = (section.get("server_url") or "").strip()
            username = (section.get("username") or "").strip()
            raw_token = section.get("token") or ""
            verify_ssl = _parse_bool_setting(section.get("verify_ssl"), default=True)
            ca_file = (section.get("ca_file") or "").strip()
            auth_scheme = _normalize_jira_auth_scheme(section.get("auth_scheme") or auth_scheme)
            api_version = _normalize_jira_api_version(section.get("api_version") or api_version)
            if raw_token:
                resolved = resolve_secret(raw_token)
                token = resolved if isinstance(resolved, str) else ""
        except ImportError:
            pass
        # Env vars fill anything missing (existing JiraIntegration contract).
        server = server or os.environ.get("JIRA_SERVER_URL", "").strip()
        username = username or os.environ.get("JIRA_USERNAME", "").strip()
        token = token or os.environ.get("JIRA_API_TOKEN", "").strip()
        company_settings = _company_settings_jira_auth()
        server = server or company_settings.get("server_url", "")
        username = username or company_settings.get("username", "")
        token = token or company_settings.get("token", "")
        verify_ssl = _parse_bool_setting(
            os.environ.get("JIRA_VERIFY_SSL")
            or os.environ.get("JIRA_SSL_VERIFY")
            or company_settings.get("verify_ssl"),
            default=verify_ssl,
        )
        ca_file = (
            os.environ.get("JIRA_CA_FILE")
            or os.environ.get("JIRA_SSL_CA_FILE")
            or company_settings.get("ca_file")
            or ca_file
        ).strip()
        auth_scheme = _normalize_jira_auth_scheme(
            os.environ.get("JIRA_AUTH_SCHEME")
            or os.environ.get("JIRA_TOKEN_TYPE")
            or company_settings.get("auth_scheme")
            or auth_scheme
        )
        api_version = _normalize_jira_api_version(
            os.environ.get("JIRA_API_VERSION") or company_settings.get("api_version") or api_version
        )
        required = [("server_url", server), ("token", token)]
        if auth_scheme == "basic":
            required.append(("username", username))
        if not all(value for _, value in required):
            missing = [name for name, val in required if not val]
            raise RuntimeError(
                "Jira source is unconfigured — missing: "
                + ", ".join(missing)
                + ". Set [jira] in whilly.toml or JIRA_SERVER_URL/JIRA_USERNAME/JIRA_API_TOKEN. "
                + "For Personal Access Tokens set JIRA_AUTH_SCHEME=bearer."
            )
        return cls(
            server_url=server.rstrip("/"),
            username=username,
            token=token,
            verify_ssl=verify_ssl,
            ca_file=ca_file,
            auth_scheme=auth_scheme,
            api_version=api_version,
        )


def _company_settings_jira_auth() -> dict[str, str]:
    """Resolve Jira auth from a flat company settings YAML file when configured.

    Some QA environments keep shared settings in a top-level YAML file with
    keys such as ``JIRA_URL``, ``JIRA_TOKEN`` and ``EMAIL_USER``. Whilly keeps
    this optional and stdlib-only: set ``WHILLY_COMPANY_SETTINGS_FILE`` (or
    ``COMPANY_SETTINGS_FILE``) to opt in. Values are never logged here.
    """

    path = os.environ.get("WHILLY_COMPANY_SETTINGS_FILE") or os.environ.get("COMPANY_SETTINGS_FILE") or ""
    if not path:
        return {}
    settings_path = Path(path).expanduser()
    try:
        raw = settings_path.read_text(encoding="utf-8")
    except OSError:
        return {}
    data = _parse_flat_yaml_settings(raw)
    return {
        "server_url": data.get("JIRA_URL", ""),
        "username": data.get("JIRA_USERNAME") or data.get("EMAIL_USER") or data.get("ACCOUNT_USER") or "",
        "token": data.get("JIRA_TOKEN", ""),
        "verify_ssl": data.get("JIRA_VERIFY_SSL") or data.get("JIRA_SSL_VERIFY") or "",
        "ca_file": data.get("JIRA_CA_FILE") or data.get("JIRA_SSL_CA_FILE") or "",
        "auth_scheme": data.get("JIRA_AUTH_SCHEME") or data.get("JIRA_TOKEN_TYPE") or "",
        "api_version": data.get("JIRA_API_VERSION") or "",
    }


def _parse_flat_yaml_settings(raw: str) -> dict[str, str]:
    """Parse simple top-level ``KEY: value`` YAML settings without PyYAML."""

    out: dict[str, str] = {}
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or ":" not in stripped:
            continue
        key, value = stripped.split(":", 1)
        key = key.strip()
        if not key or key.startswith("-"):
            continue
        value = value.strip()
        if " #" in value:
            value = value.split(" #", 1)[0].rstrip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        out[key] = value
    return out


def _parse_bool_setting(value: Any, *, default: bool) -> bool:
    """Parse common config/env boolean spellings while preserving the default."""

    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y", "on"}:
        return True
    if text in {"0", "false", "no", "n", "off"}:
        return False
    return default


def _normalize_jira_auth_scheme(value: Any) -> str:
    """Normalize Jira authorization mode."""

    text = str(value or "").strip().lower()
    if text in {"bearer", "pat", "token", "personal_access_token"}:
        return "bearer"
    return "basic"


def _normalize_jira_api_version(value: Any) -> str:
    """Normalize supported Jira REST API version names."""

    text = str(value or "").strip().lower()
    if text in {"2", "3", "latest"}:
        return text
    return "3"


# ── Low-level REST call ───────────────────────────────────────────────────────


def _jira_get(auth: JiraAuth, path: str, *, timeout: int = 15) -> dict:
    """GET ``{server}{path}`` and return parsed JSON. Raises RuntimeError on failure."""
    url = f"{auth.server_url}{path}"
    if auth.auth_scheme == "bearer":
        authorization = f"Bearer {auth.token}"
    else:
        header = base64.b64encode(f"{auth.username}:{auth.token}".encode("utf-8")).decode("ascii")
        authorization = f"Basic {header}"
    req = Request(
        url,
        headers={"Authorization": authorization, "Accept": "application/json"},
        method="GET",
    )
    context = _jira_ssl_context(auth)
    # Optional: bypass macOS system-level proxy (PAC / scutil) when the
    # operator's network has a corporate proxy that doesn't route to the
    # internal Jira host. Env-gated so production behaviour is unchanged.
    opener = _jira_opener_no_proxy(context=context) if _bypass_system_proxy() else None
    try:
        if opener is not None:
            response = opener.open(req, timeout=timeout)
        elif context is None:
            response = urlopen(req, timeout=timeout)
        else:
            response = urlopen(req, timeout=timeout, context=context)
        with response as resp:
            raw_body = resp.read().decode("utf-8", errors="replace")
            try:
                return json.loads(raw_body or "{}")
            except JSONDecodeError as exc:
                content_type = resp.headers.get("content-type", "")
                status = getattr(resp, "status", "")
                hint = "received HTML login/SSO page" if "<html" in raw_body.lower() else "response body is not JSON"
                prefix = raw_body.replace("\r", " ").replace("\n", " ")[:300]
                raise RuntimeError(
                    f"Jira GET {path} returned non-JSON response: HTTP {status}, "
                    f"content-type={content_type!r}, {hint}: {prefix}"
                ) from exc
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")[:500] if exc.fp else ""
        raise RuntimeError(f"Jira GET {path} failed: HTTP {exc.code} — {body}") from exc
    except URLError as exc:
        raise RuntimeError(f"Jira GET {path} network error: {exc.reason}") from exc


def _jira_ssl_context(auth: JiraAuth) -> ssl.SSLContext | None:
    """Return an explicit SSL context only when Jira TLS config differs from defaults."""

    if not auth.verify_ssl:
        log.warning("Jira TLS certificate verification is disabled by configuration")
        return ssl._create_unverified_context()  # noqa: SLF001 - operator-controlled corporate Jira escape hatch
    if auth.ca_file:
        return ssl.create_default_context(cafile=auth.ca_file)
    return None


def _jira_rest_path(auth: JiraAuth, resource: str) -> str:
    """Build a Jira REST path using the configured API version."""

    return f"/rest/api/{auth.api_version}/{resource.lstrip('/')}"


# ── Description flattening (Jira v3 Atlassian Document Format) ────────────────


def _flatten_adf(node: Any) -> str:
    """Best-effort ADF → plain text. Preserves paragraph breaks and bullet-list dashes."""
    if node is None:
        return ""
    if isinstance(node, str):
        return node

    if isinstance(node, list):
        return "".join(_flatten_adf(n) for n in node)

    if not isinstance(node, dict):
        return ""

    ntype = node.get("type") or ""
    content = node.get("content") or []
    inner = "".join(_flatten_adf(c) for c in content)

    if ntype == "text":
        return str(node.get("text") or "")
    if ntype == "heading":
        level = int((node.get("attrs") or {}).get("level") or 2)
        hashes = "#" * max(1, min(level, 6))
        return f"{hashes} {inner.strip()}\n\n"
    if ntype == "paragraph":
        return inner.rstrip() + "\n\n"
    if ntype == "hardBreak":
        return "\n"
    if ntype == "listItem":
        return f"- {inner.strip()}\n"
    if ntype in ("bulletList", "orderedList"):
        return inner + "\n"
    if ntype == "codeBlock":
        return f"\n```\n{inner.rstrip()}\n```\n\n"
    if ntype == "blockquote":
        return "\n".join(f"> {line}" for line in inner.splitlines()) + "\n\n"
    # Unknown nodes — just pass through their inner content.
    return inner


# ── Issue → whilly task ───────────────────────────────────────────────────────


# Map common Jira priority names → whilly priority values (case-insensitive).
_PRIORITY_MAP: dict[str, str] = {
    "highest": "critical",
    "critical": "critical",
    "blocker": "critical",
    "high": "high",
    "medium": "medium",
    "normal": "medium",
    "low": "low",
    "lowest": "low",
    "trivial": "low",
}


def _jira_priority(issue_fields: dict[str, Any]) -> str:
    priority = (issue_fields.get("priority") or {}).get("name") or ""
    return _PRIORITY_MAP.get(priority.lower(), "medium")


def _extract_section_bullets(text: str, section_name: str) -> list[str]:
    """Pick bullet items under a markdown-style ``## Acceptance`` heading in the flattened description."""
    if not text:
        return []
    lines = text.splitlines()
    in_section = False
    items: list[str] = []
    heading_re = re.compile(rf"^#{{1,6}}\s+{re.escape(section_name)}\b", re.IGNORECASE)
    next_heading_re = re.compile(r"^#{1,6}\s+\S")
    bullet_re = re.compile(r"^\s*[-*]\s+(.+?)\s*$")
    for line in lines:
        if heading_re.match(line):
            in_section = True
            continue
        if in_section and next_heading_re.match(line):
            break
        if in_section:
            m = bullet_re.match(line)
            if m:
                items.append(m.group(1).strip())
    return items


def issue_to_task_dict(key: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Convert a Jira REST /issue response into the dict shape expected by merge_into_plan.

    We reuse the GitHub merge pipeline: it matches tasks by ``id`` (``JIRA-<key>``
    here) and treats the input as a gh-shaped issue dict. The field names that
    matter to :func:`whilly.sources.github_issues.issue_to_task` are ``number``,
    ``title``, ``body``, ``labels``, ``url`` — we synthesise them from Jira.
    """
    fields = payload.get("fields") or {}
    summary = fields.get("summary") or key
    description_raw = fields.get("description")
    # Jira v3 → ADF; v2 → string. Support both for robustness.
    if isinstance(description_raw, dict):
        body = _flatten_adf(description_raw).strip()
    else:
        body = (description_raw or "").strip()

    labels_list = fields.get("labels") or []
    priority_name = (fields.get("priority") or {}).get("name") or ""
    labels_for_gh = [{"name": label} for label in labels_list]
    if priority_name:
        labels_for_gh.append({"name": f"priority:{_jira_priority(fields)}"})

    browse_url = ""
    base = payload.get("self") or ""
    if base:
        # Jira's `self` URL is ``{server}/rest/api/3/issue/{id}`` — derive the browse URL.
        match = re.match(r"^(https?://[^/]+)/", base)
        if match:
            browse_url = f"{match.group(1)}/browse/{key}"

    # The gh pipeline uses ``number`` as the task id suffix; pass the key as-is
    # so ``issue_to_task`` produces ``GH-<key>`` — but we want ``JIRA-<key>``.
    # Easiest path: build the Task dict directly here rather than going through
    # ``issue_to_task``. Still reuse ``merge_into_plan`` which treats the input
    # as a pre-built issue dict OR as something its own converter handles.
    description_short = summary
    if body:
        snippet = body.replace("\r\n", "\n")
        if len(snippet) > 480:
            snippet = snippet[:480].rsplit("\n", 1)[0] + "\n…"
        description_short = f"{summary}\n\n{snippet}"

    return {
        "_jira_key": key,
        "number": key,  # merge_into_plan uses this to build the task id
        "title": summary,
        "body": body,
        "description": sanitize_external_text(description_short, scope="jira_description"),
        "description_short": description_short,
        "labels": labels_for_gh,
        "url": browse_url,
        "priority": _jira_priority(fields),
        "acceptance_criteria": [
            sanitize_external_text(item, scope="jira_acceptance")
            for item in _extract_section_bullets(body, "Acceptance")
        ],
        "test_steps": [
            sanitize_external_text(item, scope="jira_test") for item in _extract_section_bullets(body, "Test")
        ],
        "jira_key": key,
    }


def _adapt_for_merge(jira_dict: dict[str, Any]) -> dict[str, Any]:
    """Shape a Jira-source dict so :func:`merge_into_plan` produces JIRA-prefixed Task ids.

    ``issue_to_task`` uses ``f"GH-{number}"`` unconditionally. We intercept by
    building the gh-shaped dict with ``number = "<key>"`` and then rewriting
    ``task.id`` after conversion.
    """
    return {
        "number": jira_dict["_jira_key"],
        "title": jira_dict["title"],
        "body": jira_dict["body"],
        "labels": jira_dict["labels"],
        "url": jira_dict["url"],
        "createdAt": "",
        "updatedAt": "",
    }


# ── Top-level API ─────────────────────────────────────────────────────────────


def fetch_single_jira_issue(
    key: str,
    out_path: str | Path = "tasks.json",
    *,
    timeout: int = 15,
) -> tuple[Path, FetchStats]:
    """Fetch one Jira issue by key and merge it into a one-task plan.

    Returns the resolved plan path and a :class:`FetchStats` matching the
    semantics of :func:`whilly.sources.github_issues.fetch_single_issue` so
    callers can share result-handling code.
    """
    clean_key = parse_jira_key(key)
    auth = JiraAuth.from_config()
    # Use v3 for richer ADF payload; v2 responds with plain-string description.
    payload = _jira_get(auth, _jira_rest_path(auth, f"issue/{clean_key}"), timeout=timeout)
    jira_dict = issue_to_task_dict(clean_key, payload)
    source = GitHubIssuesSource(
        owner="jira",
        repo=clean_key.split("-", 1)[0].lower() or "project",
        label=clean_key,
        limit=1,
    )
    plan_path = Path(out_path).resolve()
    # Adapt + merge. merge_into_plan will build a Task with id GH-<key>; we
    # rewrite it to JIRA-<key> in the plan afterwards so downstream integrations
    # recognise it as a Jira task.
    stats = merge_into_plan([_adapt_for_merge(jira_dict)], source, plan_path)
    _rewrite_task_id_to_jira(plan_path, clean_key)
    log.info("Jira source: %s fetched (new=%d, updated=%d)", clean_key, stats.new, stats.updated)
    return plan_path, stats


def _rewrite_task_id_to_jira(plan_path: Path, key: str) -> None:
    """Rewrite the task id inside the saved plan from ``GH-<key>`` to ``JIRA-<key>``.

    Keeps the file valid JSON (reads → mutates → writes atomically via tempfile
    is overkill here; the pipeline already wrote the file atomically).
    """
    if not plan_path.is_file():
        return
    data = json.loads(plan_path.read_text(encoding="utf-8"))
    changed = False
    browse_url = ""
    repo_target = _jira_target_repo_block()
    for task in data.get("tasks", []):
        if task.get("id") == f"GH-{key}":
            task["id"] = f"JIRA-{key}"
            task.setdefault("category", "jira-issue")
            task["jira_key"] = key
            browse_url = str(task.get("prd_requirement") or "")
            if repo_target is None:
                task.pop("repo_target_id", None)
            else:
                task["repo_target_id"] = repo_target["id"]
            changed = True
    if changed:
        data["origin"] = {
            "system": "jira_issue",
            "ref": key,
            "url": browse_url,
            "title": f"Jira issue {key}",
            "decomposition_mode": "source_adapter",
        }
        data["repo_targets"] = [] if repo_target is None else [repo_target]
        plan_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _jira_target_repo_block() -> dict[str, str] | None:
    """Return optional GitHub repo target for Jira-sourced tasks.

    Jira issue keys do not identify a code repository by themselves. Operators
    can set ``WHILLY_JIRA_TARGET_REPO=owner/repo`` to make generated v4 plans
    repo-routable while keeping the Jira origin separate from execution.
    """
    repo = os.environ.get("WHILLY_JIRA_TARGET_REPO", "").strip()
    if not repo or "/" not in repo:
        return None
    return {
        "id": f"github:{repo}",
        "provider": "github",
        "repo_full_name": repo,
        "clone_url": f"https://github.com/{repo}.git",
    }


# ── Key parsing ───────────────────────────────────────────────────────────────


_JIRA_KEY_RE = re.compile(r"^[A-Z][A-Z0-9]+-\d+$")


def parse_jira_key(ref: str) -> str:
    """Normalise a Jira reference into a ``ABC-123`` key.

    Accepts:
      - ``ABC-123`` canonical form
      - ``abc-123`` (case-insensitive — upper-cased to match Jira storage)
      - ``https://<server>/browse/ABC-123`` URL
    """
    if not ref or not isinstance(ref, str):
        raise ValueError(f"Jira reference must be a non-empty string, got {ref!r}")
    s = ref.strip()

    url_match = re.search(r"/browse/([A-Za-z][A-Za-z0-9]+-\d+)", s)
    if url_match:
        return url_match.group(1).upper()

    candidate = s.upper()
    if _JIRA_KEY_RE.match(candidate):
        return candidate
    raise ValueError(f"Cannot parse Jira reference {ref!r}. Expected 'ABC-123' or an issue browse URL.")


__all__ = [
    "JiraAuth",
    "fetch_single_jira_issue",
    "issue_to_task_dict",
    "parse_jira_key",
]
