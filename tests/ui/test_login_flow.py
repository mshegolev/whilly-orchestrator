"""UI tests for the magic-link login funnel (PRD Epic A1–A5).

Browser-driven via Playwright. All locators are role/label/text based — no
``data-testid``, no CSS selectors — so the tests stay decoupled from
markup refactors and validate the *user-visible* contract.

User stories covered:

* **A1** Operator lands on ``/login`` → fills email → submits.
* **A2** "Check your inbox" page shows email + dev-mode hint + Send again.
* **A3** Operator clicks magic link → cookie set → lands on plans page.
* **A4** Header nav shows "Signed in as <email>" + Log out.
* **A5** Replay of consumed link renders "already been used" page.
"""

from __future__ import annotations

import pytest

pytestmark = [pytest.mark.ui]


def test_login_form_renders_with_label_and_submit_button(page, live_server: str) -> None:
    page.goto(f"{live_server}/login")

    # Heading communicates the page purpose.
    assert page.get_by_role("heading", name="Sign in to Whilly").is_visible()

    # Email input is reachable by accessible label (no testid needed).
    email_input = page.get_by_label("Email")
    assert email_input.is_visible()
    assert email_input.get_attribute("type") == "email"
    assert email_input.get_attribute("required") is not None

    # Submit button has visible name — getByRole resolves it.
    submit = page.get_by_role("button", name="Send sign-in link")
    assert submit.is_visible()


def test_submit_renders_check_inbox_with_email_and_send_again(page, live_server: str, magic_link_reader) -> None:
    page.goto(f"{live_server}/login")
    page.get_by_label("Email").fill("a1@example.com")
    page.get_by_role("button", name="Send sign-in link").click()

    page.get_by_role("heading", name="Check your inbox").wait_for()
    assert page.get_by_text("a1@example.com").is_visible()
    # Dev-mode hint is a role="note" landmark.
    assert page.get_by_text("written to").first.is_visible()
    # "Send again" link round-trips back to /login with prefilled email.
    send_again = page.get_by_role("link", name="Send again")
    assert send_again.is_visible()
    assert "email=a1%40example.com" in send_again.get_attribute("href")

    # Event log carries exactly one magic-link.issued for this email.
    link = magic_link_reader("a1@example.com")
    assert "/auth/magic?token=" in link


def test_send_again_returns_to_login_with_email_prefilled(page, live_server: str) -> None:
    """A2 — Wrong address? Send again should round-trip without retyping."""
    page.goto(f"{live_server}/login")
    page.get_by_label("Email").fill("typo@gnail.com")
    page.get_by_role("button", name="Send sign-in link").click()
    page.get_by_role("heading", name="Check your inbox").wait_for()

    page.get_by_role("link", name="Send again").click()
    page.get_by_role("heading", name="Sign in to Whilly").wait_for()

    # The email field is pre-filled with the typo so the operator can edit
    # only the broken character instead of retyping the whole address.
    assert page.get_by_label("Email").input_value() == "typo@gnail.com"


def test_magic_link_sets_session_and_lands_on_plans_page(page, live_server: str, magic_link_reader) -> None:
    """A3+A4 — consuming a valid link establishes the session and reveals plans nav."""
    page.goto(f"{live_server}/login")
    page.get_by_label("Email").fill("a3@example.com")
    page.get_by_role("button", name="Send sign-in link").click()
    page.get_by_role("heading", name="Check your inbox").wait_for()

    link = magic_link_reader("a3@example.com")
    page.goto(link)

    # Header nav announces the authenticated principal.
    page.get_by_text("Signed in as").wait_for()
    assert page.get_by_text("a3@example.com").is_visible()
    assert page.get_by_role("button", name="Log out").is_visible()

    # Plans table heading is present (empty-state fallback is fine — fixture
    # truncated the DB).
    assert page.get_by_role("heading", name="Plans").is_visible()


def test_replay_of_consumed_magic_link_renders_already_used_page(page, live_server: str, magic_link_reader) -> None:
    """A5 — a consumed link must render a human page with a recovery CTA."""
    page.goto(f"{live_server}/login")
    page.get_by_label("Email").fill("a5@example.com")
    page.get_by_role("button", name="Send sign-in link").click()
    page.get_by_role("heading", name="Check your inbox").wait_for()
    link = magic_link_reader("a5@example.com")

    # First consume — session established.
    page.goto(link)
    page.get_by_text("Signed in as").wait_for()
    page.get_by_role("button", name="Log out").click()

    # Second consume — same token, no longer valid.
    page.goto(link)
    assert page.get_by_role("heading", name="This link has already been used or has expired").is_visible()
    cta = page.get_by_role("link", name="Request a new link")
    assert cta.is_visible()
    assert cta.get_attribute("href") == "/login"


def test_logout_clears_session_and_redirects_to_login(page, live_server: str, magic_link_reader) -> None:
    """A4 — Logout button revokes the session and bounces the operator back to /login."""
    page.goto(f"{live_server}/login")
    page.get_by_label("Email").fill("logout@example.com")
    page.get_by_role("button", name="Send sign-in link").click()
    page.get_by_role("heading", name="Check your inbox").wait_for()
    page.goto(magic_link_reader("logout@example.com"))
    page.get_by_text("Signed in as").wait_for()

    page.get_by_role("button", name="Log out").click()

    # After logout, requesting / should bounce to /login.
    page.goto(f"{live_server}/")
    page.get_by_role("heading", name="Sign in to Whilly").wait_for()
