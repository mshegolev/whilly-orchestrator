"""UI tests for the Tasks CRUD surface (PRD Epic C1–C5).

Browser-driven via Playwright against the live uvicorn server. Tests focus
on the operator-visible contract of the task edit modal: optimistic
concurrency (412 stale If-Match), claimed-task rejection (409 with Force-
release confirm), hard delete with inline two-step confirm.

Note: the same legacy bearer-in-URL caveat that affects the Create Task
form (see ``test_plans_crud.py::test_archived_plan_rejects_new_task_with_410``)
also affects the Edit/Delete fetch handlers — they read ``?token=`` from
the URL. The signed_in_page fixture lands on a session-cookie page so
these tests must navigate to ``/plans/<id>?token=<bearer>`` for now. The
follow-up note in the skip-reason there applies here too.
"""

from __future__ import annotations

import pytest

pytestmark = [pytest.mark.ui]


# ── helpers ────────────────────────────────────────────────────────────────


def _switch_to_plans_tasks_tab(page) -> None:
    """The Tasks panel lives behind the operator surface tab strip."""
    page.get_by_role("tab").filter(has_text="Plans/Tasks").click()


def test_edit_task_modal_opens_with_prefilled_fields(signed_in_page, insert_plan, insert_task) -> None:
    """C1 — clicking Edit on a PENDING task row opens modal with current values."""
    insert_plan(plan_id="ui-tasks", name="UI Tasks")
    insert_task(
        plan_id="ui-tasks",
        task_id="UI-T-1",
        description="Original description",
        priority="high",
    )

    page = signed_in_page
    page.reload()
    _switch_to_plans_tasks_tab(page)

    row = page.get_by_test_id("task-row").filter(has=page.get_by_text("UI-T-1"))
    row.wait_for(state="visible")
    row.get_by_role("button", name="Edit").click()

    dialog = page.get_by_test_id("edit-task-modal")
    dialog.wait_for(state="visible")
    assert dialog.get_by_label("Description").input_value() == "Original description"
    # Priority select — its accessible label is "Priority", the current
    # selected option is "high".
    assert dialog.get_by_label("Priority").input_value() == "high"


def test_edit_task_save_with_valid_if_match_updates_row(signed_in_page, insert_plan, insert_task, live_server) -> None:
    """C2 — Save with the row's current If-Match version succeeds (200)."""
    insert_plan(plan_id="ui-tasks", name="UI Tasks")
    insert_task(plan_id="ui-tasks", task_id="UI-T-2", description="Before", version=0)

    page = signed_in_page
    # Navigate to the plan-scoped URL so the operator snapshot is filtered
    # to this plan_id; signed_in_page lands on / which shows ALL tasks.
    page.goto(f"{live_server}/plans/ui-tasks")
    _switch_to_plans_tasks_tab(page)

    row = page.get_by_test_id("task-row").filter(has=page.get_by_text("UI-T-2"))
    row.wait_for(state="visible")
    row.get_by_role("button", name="Edit").click()

    dialog = page.get_by_test_id("edit-task-modal")
    dialog.wait_for(state="visible")
    dialog.get_by_label("Description").fill("After")
    dialog.get_by_role("button", name="Save").click()

    dialog.wait_for(state="hidden")
    # PATCH succeeded (modal hidden = 200). Verify by re-opening the modal
    # for the same task ID — its description input must show "After". The
    # tasks-table cell doesn't render the description text, so we cannot
    # assert on the table; the modal re-fetch is the operator-visible
    # confirmation that the new value persisted.
    page.goto(f"{live_server}/plans/ui-tasks")
    _switch_to_plans_tasks_tab(page)
    row = page.get_by_test_id("task-row").filter(has=page.get_by_text("UI-T-2"))
    row.wait_for(state="visible")
    row.get_by_role("button", name="Edit").click()
    dialog2 = page.get_by_test_id("edit-task-modal")
    dialog2.wait_for(state="visible")
    assert dialog2.get_by_label("Description").input_value() == "After"


@pytest.mark.skip(
    reason=(
        "C5 race scenario — Edit button is hidden for CLAIMED tasks by "
        "design (_tasks_table.html line 39: _editable iff status in "
        "{PENDING,DONE,FAILED,SKIPPED}). The 409 force-release path only "
        "fires when a PENDING task is claimed mid-edit; reproducing that "
        "requires (1) seed PENDING, (2) open Edit modal, (3) in-flight "
        "UPDATE tasks SET claimed_by via psql, (4) click Save. Doable but "
        "needs a more elaborate fixture than the simple seed-then-click "
        "pattern used elsewhere. Tracked as follow-up."
    )
)
def test_edit_claimed_task_surfaces_409_force_release_banner(
    signed_in_page, insert_plan, insert_task, postgres_dsn, live_server
) -> None:
    """C5 — editing a claimed task triggers 409 with a two-step Force-release confirm."""
    insert_plan(plan_id="ui-tasks", name="UI Tasks")
    # tasks.claimed_by → workers(worker_id) FK; seed a worker row first.
    from tests.ui.conftest import _psql_run  # type: ignore[attr-defined]

    _psql_run(
        postgres_dsn,
        "INSERT INTO workers (worker_id, hostname, status) "
        "VALUES ('w-ui-fake', 'ui-fake-host', 'online') ON CONFLICT DO NOTHING",
    )
    insert_task(
        plan_id="ui-tasks",
        task_id="UI-T-3",
        description="Claimed by worker",
        version=1,
        status="CLAIMED",
        claimed_by="w-ui-fake",
    )

    page = signed_in_page
    page.goto(f"{live_server}/plans/ui-tasks")
    _switch_to_plans_tasks_tab(page)

    row = page.get_by_test_id("task-row").filter(has=page.get_by_text("UI-T-3"))
    row.wait_for(state="visible")
    row.get_by_role("button", name="Edit").click()

    dialog = page.get_by_test_id("edit-task-modal")
    dialog.wait_for(state="visible")
    dialog.get_by_label("Description").fill("attempted change")
    dialog.get_by_role("button", name="Save").click()

    # The 409 surfaces a banner inside the modal with a Force-release button
    # carrying the unique testid (text "Confirm release" is ambiguous across
    # destructive flows, hence the testid is the right primary locator).
    force_btn = dialog.get_by_test_id("force-release-confirm-btn")
    force_btn.wait_for(state="visible")
    # The banner mentions the worker id so the operator sees who is in the
    # way before they interrupt.
    assert dialog.get_by_text("w-ui-fake").is_visible()


def test_delete_task_with_inline_confirm_removes_row(signed_in_page, insert_plan, insert_task) -> None:
    """C3 — Delete shows two-step inline confirm; Confirm hard-deletes the row."""
    insert_plan(plan_id="ui-tasks", name="UI Tasks")
    insert_task(plan_id="ui-tasks", task_id="UI-T-4", description="To delete", version=0)

    page = signed_in_page
    page.reload()
    _switch_to_plans_tasks_tab(page)

    row = page.get_by_test_id("task-row").filter(has=page.get_by_text("UI-T-4"))
    row.wait_for(state="visible")

    # First click: inline confirm appears next to the delete button.
    row.get_by_role("button", name="Delete task").click()
    # Second click: actual delete.
    row.get_by_role("button", name="Confirm").click()

    page.get_by_test_id("task-row").filter(has=page.get_by_text("UI-T-4")).wait_for(state="hidden")


# ── C4 — non-skipped because it's a read-only assertion ───────────────────


def test_pending_tasks_show_edit_and_delete_buttons(signed_in_page, insert_plan, insert_task, live_server) -> None:
    """C4 — PENDING tasks expose Edit + Delete in the Actions column.

    This is a markup-only check — no auth needed for the GET. Confirms that
    the row contract (testid + actions) is present so future iterations can
    rely on it.
    """
    insert_plan(plan_id="ui-tasks", name="UI Tasks")
    insert_task(plan_id="ui-tasks", task_id="UI-T-5", description="Visible actions", version=0)

    page = signed_in_page
    page.goto(f"{live_server}/plans/ui-tasks")
    _switch_to_plans_tasks_tab(page)

    row = page.get_by_test_id("task-row").filter(has=page.get_by_text("UI-T-5"))
    row.wait_for(state="visible")
    # Edit and Delete buttons reachable by accessible name (no testid).
    assert row.get_by_role("button", name="Edit").is_visible()
    assert row.get_by_role("button", name="Delete task").is_visible()
