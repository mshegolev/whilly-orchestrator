"""UI tests for the username+password login form (PRD Epic A, post-020_users).

Browser-driven via Playwright. Locators are role/label/text based; no CSS
selectors, no ``data-testid`` — tests validate the user-visible contract
and stay decoupled from markup refactors (e.g. the TUI redesign in
``style(wui): unified TUI aesthetic across all templates``).

Bootstrap user from migration 020 (``admin``/``admin``, role=admin) is the
canonical fixture identity. The autouse truncate in
``tests/ui/conftest.py`` does NOT include the ``users`` table, so this
row survives between tests.

User stories covered:

* **A1** Operator lands on ``/login`` and sees a username + password form.
* **A2** Submitting ``admin``/``admin`` sets a session cookie and lands
  on the plans list with "Signed in as ..." nav.
* **A3** Wrong password keeps the operator on ``/login`` and renders an
  inline error banner.
* **A4** ``Log out`` revokes the session and bounces back to ``/login``.
* **A5** The "sign in by email" footer link routes to the magic-link
  fallback form at ``/login/magic`` (preserved for ops convenience).
"""

from __future__ import annotations

import pytest

pytestmark = [pytest.mark.ui]


# ── A1: form renders ───────────────────────────────────────────────────────


def test_login_form_renders_with_username_and_password(page, live_server: str) -> None:
    page.goto(f"{live_server}/login")

    # Page heading communicates the page purpose.
    assert page.get_by_role("heading", name="Sign in to Whilly").is_visible()

    # Username + password inputs reachable by accessible label.
    username = page.get_by_label("username")
    assert username.is_visible()
    assert username.get_attribute("required") is not None
    password = page.get_by_label("password")
    assert password.is_visible()
    assert password.get_attribute("type") == "password"
    assert password.get_attribute("required") is not None

    # Submit button name preserved across redesigns — the locator is the
    # contract; the TUI redesign chose "[ sign in ]" as the visible label.
    assert page.get_by_role("button", name="[ sign in ]").is_visible()


# ── A2: successful login ───────────────────────────────────────────────────


def test_admin_admin_login_sets_session_and_lands_on_plans(page, live_server: str) -> None:
    """A2 — bootstrap admin credentials succeed and the operator sees the plans table."""
    page.goto(f"{live_server}/login")
    page.get_by_label("username").fill("admin")
    page.get_by_label("password").fill("admin")
    page.get_by_role("button", name="[ sign in ]").click()

    # Header nav announces the authenticated principal.
    page.get_by_text("Signed in as").wait_for()
    assert page.get_by_text("admin@whilly.local").is_visible()
    assert page.get_by_role("button", name="Log out").is_visible()

    # Plans section heading is present (empty-state CTA fine — truncate fixture).
    assert page.get_by_role("heading", name="Plans").is_visible()


# ── A3: wrong password → inline error ─────────────────────────────────────


def test_wrong_password_shows_inline_error_and_stays_on_login(page, live_server: str) -> None:
    """A3 — invalid creds keep the operator on /login with a role=alert banner."""
    page.goto(f"{live_server}/login")
    page.get_by_label("username").fill("admin")
    page.get_by_label("password").fill("definitely-not-the-password")
    page.get_by_role("button", name="[ sign in ]").click()

    # We stay on the login form (the heading is still there).
    assert page.get_by_role("heading", name="Sign in to Whilly").is_visible()

    # The error banner is reachable via role=alert.
    alert = page.get_by_role("alert")
    alert.wait_for(state="visible")
    assert "invalid" in alert.inner_text().lower()


# ── A3b: unknown username → same generic error ────────────────────────────


def test_unknown_username_shows_same_generic_error(page, live_server: str) -> None:
    """A3b — enumeration-safe: nobody-known returns the same banner as wrong-password."""
    page.goto(f"{live_server}/login")
    page.get_by_label("username").fill("nobody-known")
    page.get_by_label("password").fill("admin")
    page.get_by_role("button", name="[ sign in ]").click()

    alert = page.get_by_role("alert")
    alert.wait_for(state="visible")
    assert "invalid" in alert.inner_text().lower()


# ── A4: logout ─────────────────────────────────────────────────────────────


def test_logout_clears_session_and_redirects_to_login(page, live_server: str) -> None:
    """A4 — Log out revokes the session; the next GET / bounces back to /login."""
    page.goto(f"{live_server}/login")
    page.get_by_label("username").fill("admin")
    page.get_by_label("password").fill("admin")
    page.get_by_role("button", name="[ sign in ]").click()
    page.get_by_text("Signed in as").wait_for()

    page.get_by_role("button", name="Log out").click()

    page.goto(f"{live_server}/")
    page.get_by_role("heading", name="Sign in to Whilly").wait_for()


# ── A5: magic-link fallback discoverable ──────────────────────────────────


def test_login_footer_links_to_magic_link_fallback(page, live_server: str) -> None:
    """A5 — the /login footer surfaces "sign in by email" as a recovery affordance."""
    page.goto(f"{live_server}/login")

    fallback = page.get_by_role("link", name="sign in by email")
    assert fallback.is_visible()
    assert fallback.get_attribute("href").startswith("/login/magic")

    fallback.click()
    page.get_by_role("heading", name="Sign in to Whilly").wait_for()
    # The magic page asks for Email instead of username + password.
    assert page.get_by_label("Email").is_visible()
    assert page.get_by_role("button", name="Send sign-in link").is_visible()


# ── A5b: magic flow itself still works end-to-end ─────────────────────────


def test_magic_link_flow_still_works_for_ops_recovery(page, live_server: str, magic_link_reader) -> None:
    """A5b — POST /auth/magic-login mints a link; GET /auth/magic establishes session."""
    page.goto(f"{live_server}/login/magic")
    page.get_by_label("Email").fill("ops-recovery@example.com")
    page.get_by_role("button", name="Send sign-in link").click()
    page.get_by_role("heading", name="Check your inbox").wait_for()

    link = magic_link_reader("ops-recovery@example.com")
    page.goto(link)

    page.get_by_text("Signed in as").wait_for()
    assert page.get_by_text("ops-recovery@example.com").is_visible()
