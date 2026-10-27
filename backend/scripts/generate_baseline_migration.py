"""Utility script to generate Alembic baseline migration from approved DDL schema.

This script parses `docs/project-documents/EduSense_PostgreSQL_Schema_Revised.sql`,
strips explicit manual transaction control (`BEGIN;` / `COMMIT;`) since Alembic
manages its own migration transactions, and generates
`backend/migrations/versions/0001_initial_baseline.py` with full upgrade DDL
and reversible cascade drop statements for downgrade.

Usage:
    python backend/scripts/generate_baseline_migration.py
"""

from pathlib import Path


def generate_migration() -> None:
    repo_root = Path(__file__).resolve().parent.parent.parent
    sql_path = repo_root / "docs" / "project-documents" / "EduSense_PostgreSQL_Schema_Revised.sql"

    if not sql_path.exists():
        raise FileNotFoundError(f"Source DDL not found at {sql_path}")

    sql_content = sql_path.read_text(encoding="utf-8")

    # Strip BEGIN; and COMMIT; because Alembic manages its own transaction block!
    cleaned_sql = []
    for line in sql_content.splitlines():
        stripped = line.strip()
        if stripped in ("BEGIN;", "COMMIT;"):
            continue
        cleaned_sql.append(line)

    sql_text = "\n".join(cleaned_sql).strip()

    # Tables in reverse dependency order for safe cascade downgrade
    tables_in_drop_order = [
        "audit_security_events",
        "system_settings",
        "document_tags",
        "tags",
        "practice_answer_results",
        "practice_attempts",
        "login_attempts",
        "refresh_sessions",
        "verification_tokens",
        "task_idempotency",
        "background_jobs",
        "payment_transactions",
        "subscriptions",
        "quota_policies",
        "ai_usage_logs",
        "ai_quotas",
        "notifications",
        "course_progress",
        "study_plan_items",
        "study_plans",
        "questions",
        "question_banks",
        "document_summaries",
        "answer_citations",
        "ai_messages",
        "ai_conversations",
        "document_chunks",
        "documents",
        "class_sessions",
        "class_schedules",
        "exams",
        "assignments",
        "courses",
        "user_roles",
        "roles",
        "user_profiles",
        "users",
        "subscription_plans",
    ]

    downgrade_stmts = [
        f'    op.execute("DROP TABLE IF EXISTS {tbl} CASCADE;")'
        for tbl in tables_in_drop_order
    ]
    downgrade_stmts.append('    op.execute("DROP EXTENSION IF EXISTS vector CASCADE;")')
    downgrade_code = "\n".join(downgrade_stmts)

    template = f'''"""Initial repository baseline schema (38 tables + pgvector extension)

Revision ID: 0001_initial_baseline
Revises: 
Create Date: 2026-10-27 20:00:00.000000

Source of truth: docs/project-documents/EduSense_PostgreSQL_Schema_Revised.sql
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "0001_initial_baseline"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

UPGRADE_SQL = """
{sql_text}
"""


def _execute_sql_script(conn: sa.Connection, sql: str) -> None:
    raw = getattr(conn, "connection", None)
    if raw is not None and hasattr(raw, "driver_connection"):
        driver = raw.driver_connection
        if hasattr(driver, "execute"):
            from sqlalchemy.util._concurrency_py3k import await_only

            await_only(driver.execute(sql))
            return
    conn.exec_driver_sql(sql)


def upgrade() -> None:
    # Execute full verified DDL via driver connection to avoid colon bind parameter parsing
    bind = op.get_bind()
    _execute_sql_script(bind, UPGRADE_SQL)


def downgrade() -> None:
    # Drop all created tables in reverse dependency order
{downgrade_code}
'''

    out_path = repo_root / "backend" / "migrations" / "versions" / "0001_initial_baseline.py"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(template, encoding="utf-8")
    print(f"Generated {out_path} successfully.")


if __name__ == "__main__":
    generate_migration()
