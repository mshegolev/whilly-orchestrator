"""Add ``whilly_notify_event()`` plpgsql function + ``tr_events_notify`` AFTER INSERT trigger.

Adds the data-layer half of the M3 SSE-fanout pipeline: every row
inserted into ``events`` now fires ``pg_notify('whilly_events', …)``
with a small JSON payload that the M3 control-plane's
``_event_notify_listener_loop`` (a dedicated asyncpg connection
listening on the ``whilly_events`` channel) drains into the
in-memory per-subscriber queue feeding ``GET /events/stream``.

Function signature
------------------
* ``whilly_notify_event() RETURNS trigger`` — plpgsql; reads the
  inserted row through the special ``NEW`` record. Builds a minimal
  JSON object with the four contract-required keys (``event_id``,
  ``event_type``, ``task_id``, ``plan_id``) plus a copy of
  ``payload`` so SSE clients receive the full event without a
  follow-up SELECT. ``detail`` is *not* included by design — it is
  large free-form diagnostic data and stays in the table for
  forensics.

Trigger binding
---------------
* ``tr_events_notify`` — ``AFTER INSERT FOR EACH ROW`` on ``events``.
  AFTER (not BEFORE) so ``NEW.id`` is the actual inserted PK rather
  than ``NULL``; FOR EACH ROW so a multi-row ``INSERT … VALUES (…),
  (…)`` (the existing ``EventFlusher`` batched path, ~500 rows per
  flush) emits one NOTIFY per row, preserving the SSE one-event-per-
  fanout-message contract. Postgres delivers NOTIFYs only on COMMIT,
  so a rolled-back transaction emits zero NOTIFYs (VAL-M3-MIGRATE-
  010-014).

Payload size discipline (VAL-M3-MIGRATE-010-008)
-----------------------------------------------
Postgres' ``pg_notify`` payload limit is 8000 bytes (compiled-in
``NAMEDATALEN``-derived ceiling). When the assembled JSON exceeds a
conservative 7900-byte budget — realistic only when ``payload`` is
itself several KB — the trigger drops the heavy ``payload`` field
and adds ``"truncated": true`` so the SSE client knows the channel
message is a pointer rather than the full event. Listeners that need
the full row do a single SELECT keyed by ``event_id``.

Idempotency / re-applicability (VAL-M3-MIGRATE-010-901)
------------------------------------------------------
The upgrade uses ``CREATE OR REPLACE FUNCTION`` (no DROP needed —
replaces the body in place if the name already exists) and
``DROP TRIGGER IF EXISTS tr_events_notify ON events`` BEFORE the
``CREATE TRIGGER`` so re-applying the migration against a database
that already has both objects succeeds without error. The downgrade
uses ``DROP TRIGGER IF EXISTS`` and ``DROP FUNCTION IF EXISTS``
(NOT ``CASCADE``) so it is also re-runnable and never severs
unrelated dependents (VAL-M3-MIGRATE-010-902).

Channel name (VAL-M3-MIGRATE-010-015)
-------------------------------------
The literal string ``'whilly_events'`` is the channel; the M3 SSE
listener (``LISTEN whilly_events``) and the validation contract
both pin this exact value. Changing it would require a coordinated
update across the listener, the M3 SSE tests, and the dashboard.

Privileges (VAL-M3-MIGRATE-010-903)
-----------------------------------
``CREATE FUNCTION … LANGUAGE plpgsql`` and ``CREATE TRIGGER`` only
require schema-owner privileges on ``events`` — no superuser
required. The role used by ``WHILLY_DATABASE_URL`` in
``docker-compose.control-plane.yml`` is the table owner, so
``alembic upgrade head`` runs without elevation.

Reversibility
-------------
``downgrade()`` drops the trigger first (so ``DROP FUNCTION`` does
not need ``CASCADE``), then drops the function. After
``downgrade -1`` ``pg_proc`` no longer lists ``whilly_notify_event``
and ``pg_trigger`` no longer lists ``tr_events_notify``; the schema
is byte-equal to revision 010_funnel_url (apart from
``alembic_version`` rolling back).

Revision ID: 011_events_notify_trigger
Revises: 010_funnel_url
Create Date: 2026-05-04
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "011_events_notify_trigger"
down_revision: str | None = "010_funnel_url"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


NOTIFY_FUNCTION_NAME: str = "whilly_notify_event"
NOTIFY_TRIGGER_NAME: str = "tr_events_notify"
NOTIFY_CHANNEL_NAME: str = "whilly_events"
NOTIFY_PAYLOAD_BUDGET_BYTES: int = 7900


CREATE_NOTIFY_FUNCTION_SQL: str = f"""
CREATE OR REPLACE FUNCTION {NOTIFY_FUNCTION_NAME}() RETURNS trigger
LANGUAGE plpgsql AS $body$
DECLARE
    v_full    jsonb;
    v_text    text;
    v_minimal jsonb;
BEGIN
    v_full := jsonb_build_object(
        'event_id',   NEW.id,
        'event_type', NEW.event_type,
        'task_id',    NEW.task_id,
        'plan_id',    NEW.plan_id,
        'payload',    COALESCE(NEW.payload, '{{}}'::jsonb)
    );
    v_text := v_full::text;
    IF octet_length(v_text) > {NOTIFY_PAYLOAD_BUDGET_BYTES} THEN
        v_minimal := jsonb_build_object(
            'event_id',   NEW.id,
            'event_type', NEW.event_type,
            'task_id',    NEW.task_id,
            'plan_id',    NEW.plan_id,
            'truncated',  true
        );
        v_text := v_minimal::text;
    END IF;
    PERFORM pg_notify('{NOTIFY_CHANNEL_NAME}', v_text);
    RETURN NEW;
END;
$body$;
"""


DROP_NOTIFY_TRIGGER_SQL: str = f"DROP TRIGGER IF EXISTS {NOTIFY_TRIGGER_NAME} ON events"
CREATE_NOTIFY_TRIGGER_SQL: str = f"""
CREATE TRIGGER {NOTIFY_TRIGGER_NAME}
    AFTER INSERT ON events
    FOR EACH ROW
    EXECUTE FUNCTION {NOTIFY_FUNCTION_NAME}()
"""

DROP_NOTIFY_FUNCTION_SQL: str = f"DROP FUNCTION IF EXISTS {NOTIFY_FUNCTION_NAME}()"


def upgrade() -> None:
    """Install ``whilly_notify_event()`` + ``tr_events_notify`` (idempotent).

    Statements are issued one at a time because asyncpg refuses
    prepared statements that carry multiple top-level commands. The
    ``DROP TRIGGER IF EXISTS`` precedes ``CREATE TRIGGER`` so a
    re-applied migration succeeds against a database that already
    has the trigger present.
    """
    op.execute(CREATE_NOTIFY_FUNCTION_SQL)
    op.execute(DROP_NOTIFY_TRIGGER_SQL)
    op.execute(CREATE_NOTIFY_TRIGGER_SQL)


def downgrade() -> None:
    """Drop the trigger then the function — no CASCADE, no orphans."""
    op.execute(DROP_NOTIFY_TRIGGER_SQL)
    op.execute(DROP_NOTIFY_FUNCTION_SQL)
