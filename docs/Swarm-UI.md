# Swarm browser UI

The admin-only `/swarm` page provides one conversation screen for a local swarm:
create/select a session, discuss or request a plan, inspect proposed revisions,
explicitly run a selected revision, and stop/resume work.

The router is mounted by the main runtime with `build_swarm_router(pool=pool,
secret=secret, registry_path=trusted_registry_path)`. The registry path is server
configuration; it is never accepted from browser requests. Existing session auth
and CSRF middleware remain authoritative.

Chat and coordinator work is scheduled as bounded in-process async jobs, one
active job per session and a global cap. Job handles are in-memory; conversation
and task state is PostgreSQL-backed. Status/history polling is safe to repeat. Failures are
stored as named system messages (`chat_failed`, `run_failed`, or `resume_failed`)
without exposing exception details to the browser. Router shutdown requests
coordinator cancellation and waits for its jobs.

Integration verification belongs to the main runtime worktree: mount the router,
run the service migrations, then exercise an admin browser session against a real
trusted registry. No revision is executed merely by discussing or generating a
plan; the user must click **Run Selected**.

## Execution Gantt

Select a session and click **Гант**. The view groups tasks by project or agent
role and displays actual attempt intervals, retries, statuses and named
dependencies ("После"). Select a revision in the sidebar to inspect that plan.
Proposed or unstarted work has no fabricated dates. Missing timestamps are
explicitly labeled. Running intervals extend to the current browser time;
timestamps use the browser timezone. The view refreshes every five seconds
while visible (existing active-job polling can refresh more frequently).
This is an execution timeline, not a predictive schedule or deadline editor.

Run the calculation regression tests with `node --test tests/swarm_gantt.test.cjs`
and the report contract test with `pytest tests/unit/test_swarm_gantt.py`.

## Keyboard and appearance

Dashboard, session and product pages share terminal-styled light/dark/system
themes. `F` labels visible enabled controls; typing a label only focuses a
control, never activates it. `?` opens keyboard help, `Esc` closes help/hints.
Native Tab/Shift+Tab and button Enter/Space behavior remain available outside
hint mode. Global hints do not intercept editable inputs or browser modifiers.
Both theme selection and navigation survive dashboard HTMX refresh.

## Organizing sessions

`Показать тестовые` and `Показать архив` reveal explicitly flagged sessions.
`Тестовая сессия` marks the selected session; `В архив` / `Из архива` reversibly
organizes the list. Neither action deletes history or starts/stops execution.
Titles never automatically classify sessions. Direct session links remain usable
even when the session is filtered from the list. Metadata writes require an
administrator session and the existing CSRF checks (migration 037).
