---
phase: 05-shared-operator-table-contract
verified: 2026-05-08T11:36:51Z
status: passed
score: 5/5 must-haves verified
---

# Phase 5: Shared Operator Table Contract Verification Report

**Phase Goal:** Define and apply a shared TUI/WUI table-column contract for operator surfaces.  
**Verified:** 2026-05-08T11:36:51Z  
**Status:** passed  
**Re-verification:** No - initial verification

## Goal Achievement

### Observable Truths

| # | Truth | Status | Evidence |
| --- | --- | --- | --- |
| 1 | Operators see one explicit table-column contract for TUI and WUI task, worker, review, and event surfaces. | VERIFIED | `whilly/operator_views.py` defines `OperatorTable`, `OperatorMedium`, `OperatorTableColumn`, `OPERATOR_TABLE_COLUMNS`, `operator_table_columns`, and `operator_table_labels`; TUI and WUI both consume that metadata. |
| 2 | WUI task columns use Worker instead of Claimed by while keeping Updated as a documented WUI-only field. | VERIFIED | `OPERATOR_TABLE_COLUMNS` uses `claimed_by` label `Worker`; `updated_at` is `show_tui=False`, `show_wui=True`, with a medium note. `_tasks_table.html` renders headers from `table_columns.tasks`, and the integration test asserts `Claimed by` is absent. |
| 3 | WUI and TUI worker rows use the same field-key order: worker_id, hostname, owner_email, status, last_heartbeat. | VERIFIED | `OPERATOR_TABLE_COLUMNS[WORKERS]` pins the order; TUI `_workers_table` renders that order; WUI `_workers_table.html` renders hostname before owner; tests assert both field order and rendered worker row order. |
| 4 | Review queue and event table labels and field keys are pinned by shared metadata and tests. | VERIFIED | `OPERATOR_TABLE_COLUMNS[REVIEW_GAPS]` and `[EVENTS]` define shared keys and medium visibility; unit and integration tests assert review and event labels. |
| 5 | Future mobile layout work can rely on tested labels and field mapping instead of reverse-engineering templates. | VERIFIED | `tests/unit/test_operator_views.py`, `tests/unit/test_tui.py`, and `tests/integration/test_htmx_dashboard.py` pin pure metadata, TUI header consumption, WUI headers, and worker cell order. |

**Score:** 5/5 truths verified

### Required Artifacts

| Artifact | Expected | Status | Details |
| --- | --- | --- | --- |
| `whilly/operator_views.py` | Pure shared operator surface and table-column metadata | VERIFIED | Exports the required metadata types/helpers in `__all__`; no Rich, FastAPI, Jinja, asyncpg, or filesystem imports were added to the pure contract section. |
| `whilly/cli/tui.py` | TUI table headers and surface tabs rendered from shared metadata | VERIFIED | Imports `operator_surface_items` and `operator_table_columns`; `_add_contract_columns()` applies TUI labels for task, worker, review, and event tables. |
| `whilly/api/dashboard.py` | WUI template context populated from shared metadata | VERIFIED | Builds `surfaces` and `table_columns` from `operator_surface_items()` and `operator_table_columns(..., "wui")`. |
| `whilly/api/templates/_tasks_table.html` | WUI task headers using shared contract labels | VERIFIED | Iterates `table_columns.tasks` for headers and colspan; task cells match the metadata order including `Updated`. |
| `whilly/api/templates/_workers_table.html` | WUI worker headers and cells in shared contract order | VERIFIED | Iterates `table_columns.workers`; row cells render worker, hostname, owner, status, last heartbeat. |
| `tests/unit/test_operator_views.py` | Pure contract tests for labels, field keys, and medium-specific differences | VERIFIED | Tests exact surface order, task/worker/review/event fields and labels, and medium notes. |
| `tests/unit/test_tui.py` | TUI rendering tests for shared table labels | VERIFIED | Monkeypatch guard proves TUI reads `operator_table_columns`; surface tests assert expected labels and task `Updated` omission. |
| `tests/integration/test_htmx_dashboard.py` | WUI dashboard tests for shared table labels and worker order | VERIFIED | `test_dashboard_table_contract_headers_and_worker_order` asserts task, worker, review, and event headers plus worker hostname-before-owner order. |

### Key Link Verification

| From | To | Via | Status | Details |
| --- | --- | --- | --- | --- |
| `whilly/cli/tui.py` | `whilly/operator_views.py` | Imports `operator_surface_items` and `operator_table_columns` | WIRED | TUI header tabs and all table builders call shared metadata helpers. |
| `whilly/api/dashboard.py` | `whilly/operator_views.py` | Builds WUI `surfaces` and `table_columns` context | WIRED | Context includes `tasks`, `workers`, `review_gaps`, and `events` columns from `operator_table_columns(..., "wui")`. |
| `whilly/api/templates/_tasks_table.html` | `whilly/api/dashboard.py` | Uses `table_columns.tasks` | WIRED | Headers and empty-row colspan are driven from the context. |
| `whilly/api/templates/_workers_table.html` | `whilly/api/dashboard.py` | Uses `table_columns.workers` | WIRED | Headers and empty-row colspan are driven from the context; row cell order follows the contract. |
| `whilly/api/templates/index.html.j2` | `whilly/api/dashboard.py` | Uses `table_columns.review_gaps` and `table_columns.events` | WIRED | Review and event table headers and colspans use the shared context. |

### Requirements Coverage

| Requirement | Source Plan | Description | Status | Evidence |
| --- | --- | --- | --- | --- |
| OPUI-07 | `05-01-PLAN.md` | TUI and WUI table columns follow a shared operator contract or document intentional differences. | SATISFIED | Shared metadata exists, intentional differences are encoded in `OperatorTableColumn` flags/notes, renderers consume it, and focused tests pass. |

No orphaned Phase 5 requirements were found in `.planning/REQUIREMENTS.md`; OPUI-07 is the only requirement mapped to Phase 5 and it is claimed by the plan.

### Anti-Patterns Found

| File | Line | Pattern | Severity | Impact |
| --- | --- | --- | --- | --- |
| None | - | - | - | No blocker or incomplete-implementation patterns found. Grep hits were legitimate empty JSON fallbacks, HTML input placeholder attributes, and no-selection JS flow. |

### Human Verification Required

None required for this phase goal. The goal is a table-column contract, and the observable contract is covered by pure metadata assertions, renderer wiring checks, integration header/order tests, and lint.

### Verification Commands

- `.venv/bin/python -m pytest -q tests/unit/test_operator_views.py tests/unit/test_tui.py tests/integration/test_htmx_dashboard.py --maxfail=1` - `50 passed in 4.90s`
- `.venv/bin/python -m ruff check whilly/operator_views.py whilly/cli/tui.py whilly/api/dashboard.py tests/unit/test_operator_views.py tests/unit/test_tui.py tests/integration/test_htmx_dashboard.py` - `All checks passed!`

### Gaps Summary

No gaps found. The shared operator table-column contract exists, is substantive, is wired into both TUI and WUI renderers, and is pinned by focused tests for OPUI-07.

---

_Verified: 2026-05-08T11:36:51Z_  
_Verifier: Claude (gsd-verifier)_
