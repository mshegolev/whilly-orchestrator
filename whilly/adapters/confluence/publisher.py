"""Confluence REST API client for documentation auto-publishing.

Phase 4 of the Jira Scheduler integration (TASK-SCH-031).

This module provides a thin stdlib-only Confluence REST client that:
- Checks if a page exists (idempotent get_page_by_title)
- Creates new pages with Markdown body (storage format using wiki macro)
- Updates existing pages with version bump
- Returns structured publish results for audit/event logging

Why stdlib only? Consistency with whilly.sources.jira — both adapters use
urllib.request to avoid pulling in httpx as a runtime dependency for what
is essentially three REST calls.
"""

from __future__ import annotations

import json
import ssl
import urllib.parse
import urllib.request
from base64 import b64encode
from dataclasses import dataclass
from typing import Any


class ConfluencePublishError(RuntimeError):
    """Raised when a Confluence REST call fails or returns unexpected data."""


@dataclass(frozen=True)
class ConfluencePage:
    """Result of a create/update operation."""

    id: str
    title: str
    space_key: str
    version: int
    url: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "space_key": self.space_key,
            "version": self.version,
            "url": self.url,
        }


class ConfluencePublisher:
    """Thin REST client for the Atlassian Confluence Content API.

    Endpoints used (Confluence 7.x / Atlassian Cloud REST v1 compatible):
    - GET  /rest/api/content?title=X&spaceKey=Y  → idempotency check
    - POST /rest/api/content                     → create new page
    - PUT  /rest/api/content/{id}                → update existing page

    Auth: HTTP Basic with username + token (Atlassian Cloud API token)
    or Bearer for self-hosted (Personal Access Token).
    """

    def __init__(
        self,
        server_url: str,
        username: str,
        token: str,
        default_space: str = "",
        verify_ssl: bool = True,
        auth_scheme: str = "basic",
        timeout: int = 15,
    ):
        if not server_url:
            raise ValueError("Confluence server_url is required")
        if not token:
            raise ValueError("Confluence token is required")

        self.server_url = server_url.rstrip("/")
        self.username = username
        self.token = token
        self.default_space = default_space
        self.verify_ssl = verify_ssl
        self.auth_scheme = auth_scheme.lower()
        self.timeout = timeout

    def _build_auth_header(self) -> str:
        """Build the Authorization header value based on auth_scheme."""
        if self.auth_scheme == "bearer":
            return f"Bearer {self.token}"
        # Basic auth (Atlassian Cloud)
        credentials = f"{self.username}:{self.token}".encode("utf-8")
        encoded = b64encode(credentials).decode("ascii")
        return f"Basic {encoded}"

    def _request(
        self,
        method: str,
        path: str,
        params: dict[str, str] | None = None,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Execute an HTTP request and return parsed JSON response."""
        url = f"{self.server_url}{path}"
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"

        headers = {
            "Authorization": self._build_auth_header(),
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")

        req = urllib.request.Request(url=url, data=data, headers=headers, method=method)

        ctx = None
        if not self.verify_ssl:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE

        try:
            with urllib.request.urlopen(req, timeout=self.timeout, context=ctx) as resp:
                raw = resp.read()
                if not raw:
                    return {}
                return json.loads(raw.decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise ConfluencePublishError(
                f"Confluence {method} {path} failed with HTTP {exc.code}: {exc.read().decode('utf-8', errors='replace')}"
            ) from exc
        except (urllib.error.URLError, OSError) as exc:
            raise ConfluencePublishError(f"Confluence {method} {path} failed: {exc}") from exc

    def get_page_by_title(self, space_key: str, title: str) -> dict[str, Any] | None:
        """Find an existing page by title in the given space (idempotency check).

        Returns the first matching page dict or None.
        """
        result = self._request(
            "GET",
            "/rest/api/content",
            params={"title": title, "spaceKey": space_key, "expand": "version"},
        )
        results = result.get("results", [])
        if not results:
            return None
        return results[0]

    def create_page(
        self,
        space_key: str,
        title: str,
        body_markdown: str,
        parent_id: str | None = None,
    ) -> ConfluencePage:
        """Create a new Confluence page.

        Body is wrapped in the ``markdown`` storage macro. The page is created
        only if no page with the same title exists in the space (idempotency).

        Args:
            space_key: Target Confluence space key
            title: Page title (must be unique in space)
            body_markdown: Page body as Markdown
            parent_id: Optional parent page ID

        Returns:
            ConfluencePage with id, version, url

        Raises:
            ConfluencePublishError: on REST failure or unexpected response
        """
        existing = self.get_page_by_title(space_key, title)
        if existing:
            return self.update_page(existing["id"], title, body_markdown, current_version=existing["version"]["number"])

        # Wrap markdown in Confluence storage format using the markdown macro
        body_storage = _wrap_markdown_storage(body_markdown)

        payload: dict[str, Any] = {
            "type": "page",
            "title": title,
            "space": {"key": space_key},
            "body": {
                "storage": {
                    "value": body_storage,
                    "representation": "storage",
                }
            },
        }
        if parent_id:
            payload["ancestors"] = [{"id": parent_id}]

        result = self._request("POST", "/rest/api/content", body=payload)
        return _parse_page(result, self.server_url)

    def update_page(
        self,
        page_id: str,
        title: str,
        body_markdown: str,
        *,
        current_version: int,
    ) -> ConfluencePage:
        """Update an existing Confluence page with new content and version bump."""
        body_storage = _wrap_markdown_storage(body_markdown)
        payload = {
            "id": page_id,
            "type": "page",
            "title": title,
            "version": {"number": current_version + 1},
            "body": {
                "storage": {
                    "value": body_storage,
                    "representation": "storage",
                }
            },
        }
        result = self._request("PUT", f"/rest/api/content/{page_id}", body=payload)
        return _parse_page(result, self.server_url)


def _wrap_markdown_storage(markdown: str) -> str:
    """Wrap raw Markdown in a Confluence storage-format Markdown macro.

    The ``markdown`` macro is supported by both Confluence Server (via the
    Markdown for Confluence app) and Confluence Cloud (native). Falling back
    to plain text inside ``<ac:rich-text-body>`` is safer when the macro is
    unavailable, but produces unformatted output — Phase 4 default uses the
    macro and surfaces an error to the operator otherwise.
    """
    safe_md = (markdown or "").replace("]]>", "]]]]><![CDATA[>")
    return (
        '<ac:structured-macro ac:name="markdown" ac:schema-version="1">'
        f"<ac:plain-text-body><![CDATA[{safe_md}]]></ac:plain-text-body>"
        "</ac:structured-macro>"
    )


def _parse_page(response: dict[str, Any], server_url: str) -> ConfluencePage:
    """Convert REST response into a structured ConfluencePage."""
    page_id = str(response.get("id", ""))
    if not page_id:
        raise ConfluencePublishError(f"Confluence response missing id: {response}")

    title = str(response.get("title", ""))
    space = response.get("space", {})
    space_key = str(space.get("key", "") if isinstance(space, dict) else "")
    version = response.get("version", {})
    version_num = int(version.get("number", 1) if isinstance(version, dict) else 1)

    # Build URL from _links if available
    links = response.get("_links", {})
    if isinstance(links, dict):
        webui = links.get("webui", "")
        base = links.get("base", server_url)
        url = f"{base}{webui}" if webui else f"{server_url}/pages/viewpage.action?pageId={page_id}"
    else:
        url = f"{server_url}/pages/viewpage.action?pageId={page_id}"

    return ConfluencePage(
        id=page_id,
        title=title,
        space_key=space_key,
        version=version_num,
        url=url,
    )
