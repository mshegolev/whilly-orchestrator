# PRD: Multi-Plan WUI with Login and CRUD

**Version:** 2.0
**Date:** 2026-05-14
**Author:** Mikhail Shchegolev
**Status:** Draft (post-review)

---

## Change Log (v1 → v2)

This version replaces PRD v1.0 after three parallel reviews (Software Architect — 10 findings, Frontend UX — 10 findings, Devil's Advocate — 11 challenges). Concrete changes:

**Removed (scope cuts that all three reviews flagged):**
- **Phase 4 entirely** — "Worker management from WUI" with `POST /api/v1/plans/{id}/workers` spawning subprocesses. Both Software Architect (F3: subprocess reaper race, PID reuse, process-group leaks) and Devil's Advocate (#3: documented RCE in shared deploy) called this out as the highest-risk surface for the smallest convenience gain. Replaced with read-only worker visibility (existing data) and a "How to launch a worker" CLI snippet in the UI.
- **`sessions.default_plan_id` column** — Devil's Advocate #5 (Phase 1 unshippable without it). Default plan is now stored in the browser's `localStorage` (not in DB) and falls back to "show plans list" when unset.
- **`POST /api/v1/plans/{id}/restore` endpoint** — Devil's Advocate #8. Replaced by `PATCH /api/v1/plans/{id}` with `{archived: null}`.
- **Plan-picker dropdown in header** — Devil's Advocate #9 (duplicates the plan list page). Replaced by a "← All plans" breadcrumb.
- **Soft-delete for tasks** — `tasks.archived_at` + `ARCHIVED` status. Replaced by hard `DELETE` + `task.deleted` audit event in `whilly_events.jsonl`. Plans keep soft-delete (audit value of "what work was scoped here" is higher).
- **Card-based plan list** — Devil's Advocate #6. Replaced by a sortable HTML `<table>`. No `plans.css`, no `plans_list.html.j2` — extends the existing index template.
- **Dev-mode magic-link visible on confirmation page** — Frontend F7 + Devil's Advocate #2. Link is written to `whilly_events.jsonl` only (Devil's Advocate #7: drop double-write to `magic-links.log`).
- **Worker-bearer fallback on new CRUD endpoints** — Devil's Advocate #10. New plan/task CRUD accepts session cookie only; worker contract endpoints (`/tasks/claim`, `/tasks/complete`, etc.) keep bearer.

**Added (architectural fixes from the reviews):**
- **CSRF middleware** as Phase 1 first-class component (Architect F1). Cookie-authenticated state-mutating requests must satisfy either `SameSite=Strict` cookie + Origin allowlist or a double-submit CSRF token. Bearer/JWT bypass.
- **Auth chain order inverted: bearer → JWT → cookie** (Architect F2). Cookie is last and is *scoped* — its synthetic scope `operator.*` cannot satisfy worker endpoints regardless of credential presence.
- **Migration 019 split into 019a (additive) + 019b (validate constraint)** (Architect F4). No `ACCESS EXCLUSIVE` lock on `tasks` during deploy.
- **`If-Match` header for optimistic concurrency** instead of `expected_version` in body (Architect F6). `ETag` returned on every GET/PATCH.
- **Magic-link reuse pattern** instead of per-IP rate-limit bucket (Architect F7). Partial unique index `WHERE consumed_at IS NULL AND expires_at > now()` enforces "one live link per email."
- **Repository-layer soft-delete enforcement** with explicit `include_archived` parameter — no defaults, no VIEW (Architect F5). Importlinter rule blocks raw `FROM plans` outside repository.
- **Module split** into `auth_tokens.py` (pure crypto, stdlib), `sessions.py` (DB repo), `auth_routes.py` (FastAPI router) (Architect F9). No FastAPI imports in tokens.
- **Login funnel UX** — "Wrong email? Send again" link, dedicated "link already used" page, focus management in modals, force-release confirm step, in-flight loading states (Frontend F1, F2, F4, F6, F9 + Missing Screens checklist).
- **Phase 1+2 merged** into single "Phase 1: Auth + Plan List" (Devil's Advocate #5). No fake phasing.

**Reorganised:**
- **Timeline:** 4 weeks → 2.5 weeks (4 phases → 2 mandatory phases + 0.5 optional).
- **Goals G3 (worker launch button)** dropped from goals → moved to Future Extensions §10.
- **Goal G4 (soft-delete) narrowed** to plans only.

---

## 1. Problem Statement

Whilly's v4 architecture ([whilly/adapters/transport/server.py](../whilly/adapters/transport/server.py)) shipped a functional control plane and a single-plan operator dashboard at `/`. The dashboard renders [index.html.j2](../whilly/api/templates/index.html.j2) with HTMX fragments for tasks, workers and events, and exposes two forms (Create Task, Import from Jira). It works as a demo surface but breaks down the moment the operator manages more than one plan:

- **No plan list.** The dashboard reads `plan_id` from the URL (`?plan_id=parallel`); there is no `GET /api/v1/plans` and no "all my plans" page. To switch context the operator hand-edits the URL.
- **No login, but auth artefacts already leak.** Today's auth chain is "send a bearer in `?token=...`" — either a worker bearer (long-lived, vendored from `whilly worker register`) or a 1-hour dashboard JWT minted on every `GET /`. Both end up in browser history, in `whilly_events.jsonl`, and on monitor screen-shares. There is no concept of a user session and no way to revoke a leaked token short of restarting the server.
- **Plans are not CRUD-able from the UI.** Plan creation is a side effect of `whilly plan import` (CLI) or of the Jira-import endpoint (auto-creates `f"jira-{key.lower()}"` if no `plan_id` override — see [server.py:2733](../whilly/adapters/transport/server.py)). Renaming, archiving, attaching a budget requires SQL.
- **Tasks have only partial CRUD.** `POST /api/v1/tasks` exists ([server.py:2302](../whilly/adapters/transport/server.py)), `GET /api/v1/tasks` exists ([server.py:2244](../whilly/adapters/transport/server.py)); no `PATCH`, no `DELETE`. The only mutation paths are worker-driven (`claim` / `complete` / `fail` / `release` / `repair`).

**Baseline measurement (added in v2):** before building, the operator should grep `whilly_logs/whilly_events.jsonl` for distinct `plan_id` values seen in the last 30 days. If <5, this PRD is over-scoped and a one-day list-of-plans surface (Future Extensions §10) is the right alternative. If ≥5 and the operator switches between them ≥2×/week, this PRD's Phase 1 is justified. **The operator MUST run this check before Phase 1 starts.**

**Root cause:** the dashboard was designed as "demo surface for one running plan" — same shape as `whilly --tasks tasks.json` in v3. The control-plane API was built around worker contracts (`claim` / `complete` / `fail`), not around an operator-facing CRUD surface, so the UI inherits those gaps. Adding multi-plan management is additive; it does not change the worker contract.

---

## 2. Goals

| # | Goal |
|---|------|
| G1 | An operator opens `/` once, signs in via an email magic-link, and lands on a sortable table of *all* their plans without typing a URL. |
| G2 | The operator can create, rename, edit budget, and (soft-)archive plans from the UI; tasks can be edited (PATCH) and hard-deleted (with audit event). |
| G3 | The existing single-plan WUI continues to work for direct URL access (`?plan_id=...&token=...`) so worker-bearer-only ops paths and share-links are not broken. |
| G4 | Magic-link auth runs without an SMTP server in dev: the link is written to `whilly_events.jsonl` so a local operator can copy-paste it. |
| G5 | New cookie-authenticated endpoints are CSRF-safe by construction: SameSite=Strict + Origin allowlist enforced via middleware, not by ad-hoc checks. |
| G6 | All new endpoints support optimistic concurrency via `If-Match` / `ETag` — concurrent operator edits resolve cleanly without lost updates. |

---

## 3. Non-Goals

- **No multi-tenancy / no team accounts.** Scope is a single local operator (decision recorded 2026-05-14). All plans are visible to whoever logged in.
- **No OAuth (GitHub / Google / Microsoft).** Magic-link only.
- **No SMTP delivery in v1.** Dev-mode link surfaces in `whilly_events.jsonl`. SMTP belongs to a follow-up PRD.
- **No PATCH on IN_PROGRESS / CLAIMED tasks** — API rejects with 409 to avoid contradicting the worker's snapshot. The Force-release affordance (see Epic C) is a two-step confirm.
- **No worker launch from WUI** — moved to Future Extensions §10. The UI reads `workers` (existing table) but does not write to it.
- **No multi-plan worker process.** Operators run one `whilly-worker --plan X` terminal command per plan they want serviced. The UI surfaces a copy-paste snippet on every plan page.
- **No per-task agent log streaming.** SSE for plan-level events stays as today.
- **No mobile / responsive design in v1.** Operator tool is desktop-only. Stated explicitly so implementers do not add unnecessary breakpoints.
- **No GraphQL / REST versioning beyond `/api/v1`.** New endpoints land under `/api/v1`.

---

## 4. User Stories

### Epic A — Magic-Link Login & Sessions

**A1** — As an operator, I navigate to `/` without a `?token=` parameter. Whilly redirects me to `/login`. I see an email input.

**A2** — I submit my email. Whilly:
1. Looks for an unconsumed magic-link for this email with `expires_at > now()`. If one exists with `issued_at > now() - (auth_magic_link_ttl / 3)`, *reuses* it (no new row, no new log line). Otherwise mints a new single-use 15-minute token.
2. Writes a single `auth.magic_link.issued` event to `whilly_events.jsonl` (the link is in the payload; not duplicated to a second log).
3. Renders a confirmation page: "Check your inbox. Wrong address? [Send again]" (the "Send again" link reloads `/login?email=<value>` so the operator does not retype). The dev-mode link is **not** rendered on this page; operators get it from the log file via terminal.

**A3** — I click the link `/auth/magic?token=<opaque>`. Whilly verifies the token, marks it consumed, sets an HTTP-only `whilly_session` cookie with `SameSite=Strict; Path=/; Secure (when behind TLS); HttpOnly`, and 302-redirects me to `/`.

**A4** — A `/me` endpoint returns my session payload (`email`, `created_at`, `expires_at`) so the page can render "Signed in as …" in the header. Logout sends `POST /auth/logout`, which clears the cookie and inserts an `auth.session.revoked` event.

**A5** — Clicking a **consumed or expired** magic link renders a human page: "This link has already been used or has expired. [Request a new one]" with a link to `/login`. Never a raw JSON error.

**A6** — All `/api/v1/*` endpoints accept credentials via the **bearer → JWT → cookie** chain. Cookie is *last* and carries a synthetic scope `operator.*` that cannot satisfy worker-only endpoints (`/tasks/claim`, `/tasks/complete`, `/tasks/release`, `/tasks/fail`, `/tasks/repair`, `/workers/register`, `/workers/{id}/heartbeat`).

**A7** — Session-mid-use expiry: when an HTMX call returns 401 because the cookie is missing or expired, the JS redirect handler navigates to `/login?next=<current-path>`. After re-authentication the operator lands back on the page they were on.

### Epic B — Plans List, Picker & Soft Archive

**B1** — As an authenticated operator, `/` renders a sortable HTML `<table>` of every non-archived plan. Columns: name, plan_id, pending/in_progress/done/failed counts, budget cap + spent, online-worker indicator, last_event_at. Sort by last_event_at DESC by default; clicking any column header re-sorts. A `?q=` filter input above the table filters by name/plan_id substring (client-side; server still streams all plans for simplicity at this scale).

**B2** — Empty state: when the plans table is empty, the table is replaced by a centred "Create your first plan" CTA. Error state: if `GET /api/v1/plans` fails, a banner "Could not load plans — retry" appears in place of the table body.

**B3** — A "New Plan" button opens a modal that POSTs to `POST /api/v1/plans` with `{plan_id, name, prd_file?, budget_usd?}`. Validates `plan_id` uniqueness and inserts an empty plan; on 409 conflict, the `plan_id` field shows inline "this plan_id already exists" error. On success, modal closes and the operator routes to `/plans/<new_plan_id>`.

**B4** — Each plan row has an overflow menu (`⋯`): Rename, Edit budget, Archive, View JSON. Rename + Edit budget open a modal that PATCHes `/api/v1/plans/<plan_id>`. Archive POSTs PATCH with `{archived: true}` (no separate `/restore` endpoint).

**B5** — A "Show archived" checkbox at the top of the table reveals archived plans inline with a "Restore" button per row (PATCH `{archived: false}`). A subtle "(N archived)" badge next to the checkbox aids discoverability.

**B6** — Archived plans cannot accept new tasks (`POST /api/v1/tasks` returns 410 Gone) and are skipped by `POST /tasks/claim` so existing workers transparently idle.

**B7** — Plans list is read at every `GET /` via a single `repository.list_plans(include_archived: bool)` call. `archived_at IS NULL` filter is enforced at the repository layer (importlinter rule blocks raw `FROM plans` outside repository).

### Epic C — Task Edit & Hard Delete

**C1** — On `/plans/<plan_id>`, each task row has an "Edit" button (visible only when status ∈ {PENDING, DONE, FAILED, SKIPPED}). It opens a modal pre-filled with description, priority, key_files, acceptance_criteria, test_steps, dependencies. Modal has `role='dialog'`, `aria-modal='true'`, focus moves to the first input on open, Escape closes and returns focus to the trigger button.

**C2** — Save PATCHes `/api/v1/tasks/{task_id}?plan_id=<plan_id>` with `If-Match: "<version>"` header (read from the `ETag` of the original GET). The endpoint bumps `tasks.version`, writes a `task.edited` event with a JSON diff of changed fields, and returns 412 Precondition Failed if the version moved (worker claimed it, or another operator tab edited concurrently). The modal stays open on 412 and shows: "This task changed since you opened the modal. [Reload current values] or [Cancel]."

**C3** — Each task row has a Delete (`✕`) button. Click shows inline confirm "Delete task <id>? This cannot be undone. [Confirm] [Cancel]". Confirm DELETEs `/api/v1/tasks/{task_id}?plan_id=<plan_id>`, which hard-deletes the row and writes a `task.deleted` event (carrying the deleted row's full JSON for audit).

**C4** — `GET /api/v1/tasks` is unchanged from v1; there is no `include_archived` parameter for tasks because tasks are hard-deleted. The audit trail lives in events only.

**C5** — A task cannot be edited or deleted while `claimed_by IS NOT NULL` — the API returns 409 with body `{"error":"task_claimed","detail":"task is currently in worker <id>; release it first","worker_id":"..."}`. The UI surfaces this inline in the edit modal with a **two-step Force-release confirm**: an inline warning banner ("This will interrupt the running worker. The task will return to PENDING and may produce a duplicate result.") + a red "Confirm release" button next to a grey "Cancel" button. Confirm POSTs to the existing [`/tasks/{task_id}/release`](../whilly/adapters/transport/server.py).

### Epic D — Single-Plan Page & Cross-Cutting UI

**D1** — The current single-plan dashboard body (Tasks panel, Workers panel, Events SSE, Create Task form, Import from Jira form) moves verbatim to `/plans/<plan_id>`. The Workers panel becomes read-only: it lists workers currently bound to this plan (heartbeat ≤ 30s, status='online') with worker_id, hostname, last heartbeat. **No Launch / Stop buttons.** Below the panel, a copy-paste snippet:

```
To launch a worker for this plan locally, run:
  export WHILLY_CONTROL_URL=http://127.0.0.1:8000
  export WHILLY_PLAN_ID=<plan_id>
  export WHILLY_WORKER_TOKEN=<bearer from `whilly worker register`>
  whilly-worker
```

**D2** — `/` dispatcher logic:
- No session cookie + no `?token=` → redirect to `/login`.
- No session cookie + valid `?token=&plan_id=X` → render `/plans/X` in share-link mode (no header nav, banner "You're viewing a shared plan. [Sign in to manage all plans]").
- Valid session cookie + no specific path → render plans list (`/`).
- Valid session cookie + path `/plans/X` → render single-plan dashboard with header nav.

**D3** — Header nav (only when authenticated, only on full pages, not on share-link mode): "← All plans" breadcrumb when on `/plans/<plan_id>`; "Signed in as <email>" + logout link top-right.

**D4** — All forms and destructive actions show `aria-live="polite"` status messages on success/failure with red/green colour. Existing inline error rendering from [index.html.j2](../whilly/api/templates/index.html.j2) is reused.

**D5** — Modal pattern:
- `role="dialog"`, `aria-modal="true"`, `aria-labelledby="<heading-id>"`.
- On open: focus moves to the first focusable input.
- On Escape or backdrop click: closes and returns focus to the triggering button.
- On 412 (concurrency): modal stays open with diff (see C2).
- On 5xx: modal stays open with retry button.

---

## 5. Success Criteria

| ID | Criterion | Verification |
|----|-----------|--------------|
| SC-1.1 | Operator can sign in via magic-link and reach a non-empty plan list in ≤ 4 clicks (open `/` → enter email → click link → see plans). | Manual E2E; pytest E2E via Playwright optional. |
| SC-1.2 | `whilly_session` cookie is HTTP-only, `SameSite=Strict`, `Path=/`, signed with the same HMAC secret as dashboard JWTs ([generate_dashboard_secret](../whilly/api/dashboard_token.py)). | Integration test inspects `Set-Cookie` headers. |
| SC-1.3 | Cookie-authenticated state-mutating request without matching `Origin` is rejected with 403. CSRF middleware short-circuits before any handler runs. | Unit test with mocked Origin headers. |
| SC-1.4 | Bearer credential outranks cookie: a request carrying both `Authorization: Bearer ...` AND `Cookie: whilly_session=...` is authenticated as the bearer principal; the cookie scope `operator.*` cannot grant access to worker-only endpoints regardless. | Auth-matrix integration test. |
| SC-2.1 | `GET /api/v1/plans` returns all plans for the authenticated session in < 200ms (P95) on a DB with 1000 plans. | pytest + `EXPLAIN ANALYZE` snapshot. |
| SC-2.2 | `POST /api/v1/plans` is idempotent on `plan_id` uniqueness (returns 409 on conflict, no partial state). | Unit test against testcontainers Postgres. |
| SC-2.3 | Dev-mode magic-link is observable in `whilly_events.jsonl` exactly once per `POST /auth/login`, even when the same email submits multiple times in a row within `auth_magic_link_ttl/3` (reuse pattern). | Unit + integration test. |
| SC-3.1 | `PATCH /api/v1/tasks/{id}` with stale `If-Match` returns 412 Precondition Failed with the current version in the response `ETag`. | Unit test simulating concurrent update. |
| SC-3.2 | `DELETE /api/v1/tasks/{id}` removes the row and writes a `task.deleted` event whose payload contains the full pre-deletion task JSON. | Integration test. |
| SC-3.3 | A claimed task cannot be edited or deleted (409). Force-release flow requires two clicks before the worker is interrupted. | UI E2E (Playwright) + API contract test. |
| SC-4.1 | Existing `?token=<bearer>&plan_id=X` direct-URL flow continues to work without a session cookie (single-page mode for share-links). | Regression test of current dashboard handlers. |
| SC-4.2 | Share-link mode renders the banner "You're viewing a shared plan" and no header nav. | Snapshot test of rendered HTML. |
| SC-5.1 | All new CRUD endpoints accept *only* session cookie (no bearer fallback). Worker contract endpoints accept *only* bearer (no cookie). | Auth-matrix integration tests covering both paths. |
| SC-5.2 | Migration 019a is applied with `NOT VALID` (instant); 019b validates in a separate revision under `SHARE UPDATE EXCLUSIVE`. No deploy-window service interruption. | Migration smoke test against a loaded Postgres. |
| SC-5.3 | Importlinter rule fails CI if any module outside `whilly.adapters.db.repository` references `FROM plans` or `FROM tasks` in a raw SQL string. | `importlinter` config + CI step. |

---

## 6. Technical Scope

### 6.1 New Modules

| Path | Purpose |
|------|---------|
| `whilly/api/auth_tokens.py` | Magic-link mint/verify, session-cookie value mint/verify. Pure stdlib (`hmac`, `secrets`, `time`). **No FastAPI imports.** Mirrors [dashboard_token.py](../whilly/api/dashboard_token.py) shape. Unit-testable without DB. |
| `whilly/api/sessions.py` | DB repository for `magic_links` and `sessions` tables. `create_magic_link`, `consume_magic_link`, `create_session`, `verify_session`, `revoke_session`. No FastAPI imports. |
| `whilly/api/auth_routes.py` | FastAPI router with `/login`, `/auth/login`, `/auth/magic`, `/auth/logout`, `/me`. Cookie helpers (`set_session_cookie`, `clear_session_cookie`). Templates for login and "link used" pages. |
| `whilly/api/csrf.py` | `WhillySessionCSRFMiddleware` — inspects every state-mutating request whose auth resolved via cookie; demands `Origin` allowlist match. Bearer/JWT bypass. |
| `whilly/api/plans_api.py` | `POST/PATCH /api/v1/plans` + `GET /api/v1/plans` (list). Archive is a PATCH; no separate route. |
| `whilly/api/tasks_api_crud.py` | `PATCH /api/v1/tasks/{id}` + `DELETE /api/v1/tasks/{id}`. Uses `If-Match` / `ETag`. |
| `whilly/api/templates/login.html.j2` | Email entry form. Includes "Wrong address? Send again" affordance. |
| `whilly/api/templates/login_check_inbox.html.j2` | Confirmation page (dev-mode link NOT rendered here). |
| `whilly/api/templates/login_consumed.html.j2` | "This link has already been used or has expired" page. |

### 6.2 Modified Modules

| Path | Change |
|------|--------|
| [whilly/adapters/transport/server.py](../whilly/adapters/transport/server.py) | Mount new routers (`auth_routes`, `plans_api`, `tasks_api_crud`). Install `WhillySessionCSRFMiddleware` before the routers. Extend `_authenticate_*` helpers with cookie path **after** bearer/JWT; cookie principal carries `operator.*` scope only. Move `@app.get("/")` body to a dispatcher that branches per D2. |
| [whilly/api/dashboard.py](../whilly/api/dashboard.py) | `render_dashboard` accepts `auth_context: AuthContext` so header element can render the email. |
| [whilly/api/templates/index.html.j2](../whilly/api/templates/index.html.j2) | Add `<nav>` slot (rendered only when `auth_context.email` is set). Add plans-table block (server-side rendered). Existing single-plan body becomes an `{% include %}` so the same partial works on `/` (with cards-table replaced) and `/plans/<plan_id>`. Force-release confirm pattern (C5) added inline. |
| [whilly/adapters/db/repository.py](../whilly/adapters/db/repository.py) | Add `list_plans(include_archived: bool)`, `archive_plan`, `unarchive_plan`, `create_plan`, `patch_plan`, `patch_task`, `delete_task`. All public methods take explicit `include_archived` (no defaults). |
| [whilly/adapters/db/migrations/versions/](../whilly/adapters/db/migrations/versions/) | Three new migrations (see [§6.3](#63-new-database-tables-and-columns)): 018, 019a, 019b. |

### 6.3 New Database Tables and Columns

**Migration `018_sessions_and_magic_links.py`:**

```sql
CREATE TABLE magic_links (
    token_hash      text PRIMARY KEY,                 -- hash of the opaque token; never store the raw token
    email           text NOT NULL,
    issued_at       timestamptz NOT NULL DEFAULT now(),
    expires_at      timestamptz NOT NULL,
    consumed_at     timestamptz
);
-- Reuse pattern: only one unconsumed, unexpired link per email at any time
CREATE UNIQUE INDEX uq_magic_links_active_email
    ON magic_links(email)
    WHERE consumed_at IS NULL AND expires_at > now();
CREATE INDEX ix_magic_links_email_issued ON magic_links(email, issued_at DESC);
CREATE INDEX ix_magic_links_expires_at ON magic_links(expires_at) WHERE consumed_at IS NULL;

CREATE TABLE sessions (
    session_id      text PRIMARY KEY,                 -- random 32-byte url-safe id
    email           text NOT NULL,
    created_at      timestamptz NOT NULL DEFAULT now(),
    last_seen_at    timestamptz NOT NULL DEFAULT now(),
    expires_at      timestamptz NOT NULL,
    revoked_at      timestamptz
);
CREATE INDEX ix_sessions_email_active ON sessions(email) WHERE revoked_at IS NULL;
CREATE INDEX ix_sessions_expires_at ON sessions(expires_at) WHERE revoked_at IS NULL;
```

**Migration `019a_plans_archived_at.py` (additive, online):**

```sql
ALTER TABLE plans
    ADD COLUMN IF NOT EXISTS archived_at timestamptz,
    ADD COLUMN IF NOT EXISTS last_event_at timestamptz;
CREATE INDEX CONCURRENTLY ix_plans_active_last_event ON plans(last_event_at DESC NULLS LAST) WHERE archived_at IS NULL;
```

No changes to `tasks` table (hard delete, no ARCHIVED status). `last_event_at` is computed on read in v2 (sub-query against `events`); the column is reserved for a future Phase 1.5 if performance demands denormalisation, but is **not populated** in v2 — see Risk row §9.

**Migration `019b`** is intentionally empty in v2 (no constraint changes). It exists only to reserve the revision id; if a future PR introduces the `ARCHIVED` task status, the constraint swap goes here with `NOT VALID` + `VALIDATE CONSTRAINT` in two passes (Architect F4).

### 6.4 Config Schema Extensions

`whilly.toml` gains an optional `[wui]` section:

```toml
[wui]
auth_dev_mode         = true       # write magic link to whilly_events.jsonl instead of SMTP
auth_session_ttl      = "30d"
auth_magic_link_ttl   = "15m"
session_cookie_name   = "whilly_session"
session_cookie_secure = false      # set true behind TLS
csrf_origin_allowlist = ["http://127.0.0.1:8000", "http://localhost:8000"]
```

Env-var equivalents follow the existing `WHILLY_*` pattern.

### 6.5 API Surface (revised)

| Method | Path | Auth | Body | Returns |
|--------|------|------|------|---------|
| GET | `/login` | none | — | HTML login form |
| POST | `/auth/login` | none | form: `email` | 200 HTML (check inbox) |
| GET | `/auth/magic` | magic-link token in query | query: `token` | 302 → `/` with `Set-Cookie`; OR 200 "link used" page on consumed token |
| GET | `/me` | session | — | `{email, created_at, expires_at}` |
| POST | `/auth/logout` | session | — | 204 |
| GET | `/api/v1/plans` | **session only** | query: `include_archived`, `limit`, `cursor` | `{plans: [...], next_cursor}` |
| POST | `/api/v1/plans` | **session only** | `{plan_id, name, prd_file?, budget_usd?}` | 201, PlanPayload + ETag |
| PATCH | `/api/v1/plans/{plan_id}` | **session only** | `{name?, budget_usd?, archived?}` + `If-Match` header | 200 PlanPayload + ETag; 412 on stale |
| PATCH | `/api/v1/tasks/{task_id}` | **session only** | TaskCreateRequest subset + `If-Match` header | 200 TaskPayload + ETag; 412 on stale; 409 on claimed |
| DELETE | `/api/v1/tasks/{task_id}` | **session only** | `If-Match` header | 204; 409 on claimed |

Worker contract endpoints (`/tasks/claim`, `/tasks/{id}/complete`, `/tasks/{id}/fail`, `/tasks/{id}/release`, `/tasks/{id}/repair`, `/workers/register`, `/workers/{id}/heartbeat`) accept **bearer only** — no cookie path. This narrows the auth matrix to two clean cases.

All endpoints inherit the existing CORS / origin chain from [DASHBOARD_DEFAULT_ORIGIN](../whilly/adapters/transport/server.py), now also gated by the CSRF middleware on cookie-auth requests.

---

## 7. Dependencies

- **Postgres ≥ 13** (partial indexes with `WHERE`). Local dev `docker-compose.yml` ships postgres:15.
- **Alembic migrations infrastructure** — already present at [alembic.ini](../alembic.ini) and [whilly/adapters/db/migrations/](../whilly/adapters/db/migrations/).
- **No new Python dependencies.** Magic-link signing reuses `hmac` + `secrets` from stdlib, mirroring [dashboard_token.py](../whilly/api/dashboard_token.py). HTML form is plain HTMX (already vendored in static).
- **No SMTP** (gated by `auth_dev_mode=true`).
- **`importlinter`** (already a dev dep — verify in pyproject.toml; if not, added as a CI-only dep for SC-5.3).

---

## 8. Milestones and Phases

### Phase 1 — Auth, CSRF, Plan List, Single-Plan Page (1.5 weeks)

**Scope:** Epic A complete (A1–A7); Epic B1, B2, B5, B6, B7; Epic D complete (D1–D5). Migrations 018 + 019a applied. `auth_tokens.py`, `sessions.py`, `auth_routes.py`, `csrf.py`, `plans_api.py` (GET only). Cookie path added to `_authenticate_*` helpers **after** bearer/JWT. `/` dispatcher implements D2 branching. Login funnel + share-link mode + plans table.

**Exit criteria:** SC-1.1, SC-1.2, SC-1.3, SC-1.4, SC-2.1, SC-2.3, SC-4.1, SC-4.2, SC-5.1, SC-5.2 (019a only), SC-5.3.

**Demoable:** Operator signs in, sees all plans, navigates to a plan, sees tasks/events; share-link still works for unauthenticated visitors.

### Phase 2 — Plans + Tasks CRUD (1 week)

**Scope:** Epic B3, B4; Epic C complete (C1–C5). `POST/PATCH /api/v1/plans` (the write side). `PATCH/DELETE /api/v1/tasks/{id}` with `If-Match`. New-plan modal, plan-row overflow menu, task-edit modal with modal pattern (D5), Force-release two-step confirm.

**Exit criteria:** SC-2.2, SC-3.1, SC-3.2, SC-3.3.

**Demoable:** Full Acceptance Demo Script (Appendix B) passes.

### Phase 3 — Optional Polish (0.5 weeks; only if Phase 1+2 operator-usage data shows it is needed)

**Scope:** Plan-picker dropdown in nav (only if operator data shows ≥10 plan switches per week — measured via SSE event tap). `last_event_at` denormalisation with trigger + integration test if `MAX(emitted_at)` sub-query becomes a hot path. `q=` filter moves server-side if client-side sees >500 plans.

**Gate:** Skip Phase 3 entirely if Phase 1+2 ships and the operator reports "fine as-is" after one week of use.

---

## 9. Risks & Mitigations (revised)

| Risk | Likelihood | Mitigation |
|------|------------|------------|
| **CSRF on cookie-authenticated mutations** | Was Critical in v1 | Phase 1 ships `WhillySessionCSRFMiddleware` (Architect F1). SameSite=Strict + Origin allowlist + bearer/JWT bypass. SC-1.3 verifies. |
| **Cookie shadows bearer** (operator pastes bearer into a logged-in tab) | Was High in v1 | Auth chain inverted: bearer first, cookie last. Cookie scope `operator.*` cannot satisfy worker endpoints (Architect F2). SC-1.4 verifies. |
| **Session cookie shipped over plain HTTP in shared deploy** | Medium | `session_cookie_secure` config option; warn loudly on startup when `--host 0.0.0.0` and `session_cookie_secure=false`. |
| **Magic-link enumeration / replay** | Medium | Reuse pattern (Architect F7): partial unique index forces "one live link per email". Constant-time response shape. SC-2.3 verifies. |
| **`archived_at IS NULL` filter forgotten in future code path** | Medium | Repository-layer enforcement: explicit `include_archived` parameter, no defaults (Architect F5). Importlinter rule blocks raw `FROM plans` outside repository module. SC-5.3 verifies. |
| **`last_event_at` denormalisation drift** | Low (deferred) | v2 computes on read; column is reserved but unpopulated. Denormalisation deferred to Phase 3 with trigger + integration test (Architect F10). |
| **Migration 019a lock contention on `plans`** | Low | Additive-only migration (`ADD COLUMN IF NOT EXISTS` + `CREATE INDEX CONCURRENTLY`). `019b` is reserved-empty so the next constraint change is forced into two passes (Architect F4). SC-5.2 verifies. |
| **PATCH 412 storm** when worker rapidly bumps `tasks.version` during operator edit | Low | UI distinguishes 409 (claimed; offer Force-release) from 412 (stale; offer Reload). Both are recoverable without data loss. SC-3.1 + SC-3.3. |
| **Plan rename breaks share-links** | Very low | `plans.id` is immutable, only `name` is editable. Documented in user-facing copy. |

---

## 10. Future Extensions (Out of Scope for v2)

- **Worker launch from WUI** — deferred from v1 due to RCE-vector concerns. Future PRD must threat-model: `127.0.0.1`-only binding, CSRF-protected, audit-logged, default-disabled flag. Until then, operators run `whilly-worker` from a terminal (snippet on every plan page).
- **OAuth providers** (GitHub, Google, Microsoft) layered onto `sessions`.
- **Multi-tenant teams**: `teams` table, `plans.team_id`, role-based ACL.
- **SMTP / Resend / Postmark** integration for magic-link delivery in production.
- **PATCH for IN_PROGRESS tasks** with a "queued edit" flow that the agent picks up at the next iteration.
- **Per-task agent log streaming** in WUI via SSE bound to `task_id`.
- **Mobile / responsive layout** if operator demand emerges.
- **Multi-plan worker** (`WHILLY_PLAN_IDS=a,b,c`).
- **Plan-picker dropdown in header** (moved out of v2 main scope, see Phase 3 gate).
- **Plan list page becomes a card layout** if operator preference shifts from table-dense to visual.

---

## 11. Appendix A — Existing Components Referenced

| Component | Path |
|-----------|------|
| FastAPI app factory | [whilly/adapters/transport/server.py:create_app](../whilly/adapters/transport/server.py) |
| Dashboard token mint/verify | [whilly/api/dashboard_token.py](../whilly/api/dashboard_token.py) |
| Dashboard render entry | [whilly/api/dashboard.py:render_dashboard](../whilly/api/dashboard.py) |
| Tasks list payload | [whilly/api/tasks_api.py:list_tasks_payload](../whilly/api/tasks_api.py) |
| Worker registration RPC | [server.py @app.post("/workers/register")](../whilly/adapters/transport/server.py) |
| Worker run loop | [whilly/cli/worker.py:run_worker_command](../whilly/cli/worker.py) |
| Plan-import CLI | [whilly/cli/plan.py:_insert_plan_and_tasks](../whilly/cli/plan.py) |
| Migration root | [whilly/adapters/db/migrations/versions/](../whilly/adapters/db/migrations/versions/) |
| Existing dashboard template | [whilly/api/templates/index.html.j2](../whilly/api/templates/index.html.j2) |

---

## 12. Appendix B — Acceptance Demo Script (v2, no worker-launch UI)

After Phases 1+2 ship, this scripted walkthrough must pass:

1. Fresh DB (`docker-compose down -v && ./scripts/db-up.sh && alembic upgrade head`).
2. Start control plane (`whilly server --host 127.0.0.1 --port 8000`).
3. Open `http://127.0.0.1:8000/` in browser → land on `/login` (no cookie, no token).
4. Submit a typo `mshegolev@gnail.com` → see "Check your inbox" with **[Wrong address? Send again]** link. Click it → return to `/login?email=mshegolev@gnail.com` with field pre-filled.
5. Correct to `mshegolev@gmail.com` → submit → see "Check your inbox".
6. Tail `whilly_logs/whilly_events.jsonl` → copy the `/auth/magic?token=...` URL → paste in browser.
7. Land on `/` with empty plans table + "Create your first plan" CTA + header "Signed in as mshegolev@gmail.com".
8. Click the magic-link URL **again** in a new tab → land on "This link has already been used or has expired" page with [Request a new one] link.
9. Click "New Plan" → modal opens, focus is in the `plan_id` input → fill `plan_id=demo`, `name=Demo`, budget=$5 → Save → modal closes → land on `/plans/demo` with header showing "← All plans".
10. Click "Import from Jira" → `EORD-9843` → autonomous → see new task `JIRA-EORD-9843` appear in tasks table.
11. Open another terminal, run the copy-paste worker snippet from the plan page. Worker registers within 5s, appears in Workers panel.
12. Watch SSE events tick the task through CLAIMED → IN_PROGRESS.
13. Click task row → Edit modal opens with current values. Change priority to medium → Save → modal closes; `task.edited` event appears in event stream with a diff payload.
14. While task is IN_PROGRESS, click Edit on it → 409 inline banner appears with **Force release** two-step confirm. Click Force release → confirm → task returns to PENDING.
15. Click `⋯` on `Demo` row in the plans table → Archive → plan vanishes.
16. Check "Show archived" checkbox → plan reappears with Restore button → click Restore → plan back in active list.
17. Click "Signed in as …" → Logout → cookie cleared → `/` redirects to `/login`.
18. Open `http://127.0.0.1:8000/?token=<worker-bearer>&plan_id=demo` (no session) → land on `/plans/demo` in share-link mode with banner "You're viewing a shared plan. [Sign in to manage all plans]". No header nav.
