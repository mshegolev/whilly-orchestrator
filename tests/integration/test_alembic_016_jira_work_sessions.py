"""Smoke tests for migration ``016_jira_work_sessions``."""

from __future__ import annotations

from alembic.script import ScriptDirectory

from tests.conftest import _build_alembic_config
from whilly.adapters.db import MIGRATIONS_DIR


def test_016_is_head_revision() -> None:
    cfg = _build_alembic_config("postgresql+asyncpg://placeholder/whilly")
    script = ScriptDirectory.from_config(cfg)

    assert script.get_current_head() == "016_jira_work_sessions"
    revision = script.get_revision("016_jira_work_sessions")
    assert revision is not None
    assert revision.down_revision == "015_plan_verification_commands"


def test_016_migration_declares_jira_work_state_tables() -> None:
    text = (MIGRATIONS_DIR / "versions" / "016_jira_work_sessions.py").read_text(encoding="utf-8")

    assert "jira_work_sessions" in text
    assert "jira_work_events" in text
    assert "summary_hash" in text
    assert "description_hash" in text
    assert "link_set_hash" in text
    assert "last_seen_comment_id" in text
    assert "raw_snapshot" in text


def test_schema_sql_mentions_jira_work_state_tables() -> None:
    text = (MIGRATIONS_DIR.parent / "schema.sql").read_text(encoding="utf-8")

    assert "CREATE TABLE jira_work_sessions" in text
    assert "CREATE TABLE jira_work_events" in text
    assert "Work-kind/session state for Jira-driven plans" in text
