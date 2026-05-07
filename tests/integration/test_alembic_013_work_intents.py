"""Smoke tests for migration ``013_work_intents_repo_targets``."""

from __future__ import annotations

from alembic.script import ScriptDirectory

from tests.conftest import _build_alembic_config
from whilly.adapters.db import MIGRATIONS_DIR


def test_013_is_head_revision() -> None:
    cfg = _build_alembic_config("postgresql+asyncpg://placeholder/whilly")
    script = ScriptDirectory.from_config(cfg)

    assert script.get_current_head() == "013_work_intents_repo_targets"
    revision = script.get_revision("013_work_intents_repo_targets")
    assert revision is not None
    assert revision.down_revision == "012_pull_requests_and_pr_events"


def test_013_migration_file_declares_core_tables() -> None:
    text = (MIGRATIONS_DIR / "versions" / "013_work_intents_repo_targets.py").read_text(encoding="utf-8")

    for table_name in (
        "work_intents",
        "plan_origins",
        "repo_targets",
        "plan_repo_targets",
        "task_repo_targets",
    ):
        assert table_name in text


def test_schema_sql_documents_work_intent_and_repo_target_tables() -> None:
    text = (MIGRATIONS_DIR.parent / "schema.sql").read_text(encoding="utf-8")

    assert "CREATE TABLE work_intents" in text
    assert "CREATE TABLE repo_targets" in text
    assert "CREATE TABLE task_repo_targets" in text
    assert "ix_pull_requests_plan_repo_pr_unique" in text
