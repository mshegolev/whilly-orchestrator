from __future__ import annotations

from whilly.operator_views import (
    OperatorAction,
    OperatorSurface,
    operator_action_specs,
    operator_surface_hotkey_help,
    operator_surface_hotkeys,
    operator_wui_route_prefixes,
    operator_wui_selectors,
)


def test_operator_ui_contract_pins_surface_hotkeys_actions_selectors_and_routes() -> None:
    assert operator_surface_hotkeys() == (
        ("1", OperatorSurface.OVERVIEW),
        ("2", OperatorSurface.COMPLIANCE),
        ("3", OperatorSurface.PLANS_TASKS),
        ("4", OperatorSurface.WORKERS),
        ("5", OperatorSurface.EVENTS),
    )
    assert operator_surface_hotkey_help() == "1-5=switch"

    assert dict(operator_wui_selectors()) == {
        "surface_tab": "[data-surface-tab]",
        "surface_panel": "[data-surface]",
        "filter": "#dashboard-filter",
        "worker_control": "[data-control-action]",
        "review_decision": "[data-review-decision]",
        "review_actionable_row": '#review-gaps tbody tr[data-review-actionable="true"]',
    }
    assert dict(operator_wui_route_prefixes()) == {
        "worker_control": "/api/v1/admin/workers/",
        "task_human_review": "/api/v1/tasks/",
    }

    specs = operator_action_specs()
    assert tuple(spec.action for spec in specs) == (
        OperatorAction.QUIT,
        OperatorAction.REFRESH,
        OperatorAction.FILTER_FOCUS,
        OperatorAction.WORKERS_PAUSE,
        OperatorAction.WORKERS_RESUME,
        OperatorAction.REVIEW_SELECT_NEXT,
        OperatorAction.REVIEW_SELECT_PREVIOUS,
        OperatorAction.REVIEW_APPROVE,
        OperatorAction.REVIEW_REJECT,
        OperatorAction.REVIEW_CHANGES_REQUESTED,
    )
    assert tuple(
        (
            spec.action,
            spec.label,
            spec.hotkeys,
            spec.surfaces,
            spec.wui_selector,
            spec.wui_route_prefix,
            spec.medium_note,
        )
        for spec in specs
    ) == (
        (OperatorAction.QUIT, "Quit", ("q", "Q"), (), "", "", ""),
        (OperatorAction.REFRESH, "Refresh", ("r",), (), "", "", ""),
        (OperatorAction.FILTER_FOCUS, "Focus filter", ("/",), (), "#dashboard-filter", "", ""),
        (
            OperatorAction.WORKERS_PAUSE,
            "Pause workers",
            ("p", "P"),
            (),
            "[data-control-action='pause']",
            "/api/v1/admin/workers/",
            "",
        ),
        (
            OperatorAction.WORKERS_RESUME,
            "Resume workers",
            ("R",),
            (),
            "[data-control-action='resume']",
            "/api/v1/admin/workers/",
            "",
        ),
        (
            OperatorAction.REVIEW_SELECT_NEXT,
            "Select next review gap",
            ("j", "J"),
            (OperatorSurface.COMPLIANCE,),
            '#review-gaps tbody tr[data-review-actionable="true"]',
            "",
            "",
        ),
        (
            OperatorAction.REVIEW_SELECT_PREVIOUS,
            "Select previous review gap",
            ("k", "K"),
            (OperatorSurface.COMPLIANCE,),
            '#review-gaps tbody tr[data-review-actionable="true"]',
            "",
            "",
        ),
        (
            OperatorAction.REVIEW_APPROVE,
            "Approve review",
            ("a", "A"),
            (OperatorSurface.COMPLIANCE,),
            "[data-review-decision='approved']",
            "/api/v1/tasks/",
            "",
        ),
        (
            OperatorAction.REVIEW_REJECT,
            "Reject review",
            ("x", "X"),
            (OperatorSurface.COMPLIANCE,),
            "[data-review-decision='rejected']",
            "/api/v1/tasks/",
            "",
        ),
        (
            OperatorAction.REVIEW_CHANGES_REQUESTED,
            "Request changes",
            ("c", "C"),
            (OperatorSurface.COMPLIANCE,),
            "[data-review-decision='changes_requested']",
            "/api/v1/tasks/",
            "",
        ),
    )
