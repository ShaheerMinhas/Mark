"""Schema upgrade of a database that predates the pipeline refactor.

This is the path production Postgres will take, so it is exercised against a
database built with the *old* table shapes rather than a freshly created one.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect, text

from backend.database import (
    Base,
    _column_ddl,
    _ensure_columns,
    _ensure_indexes,
    _normalize_search_job_statuses,
)

# The search_jobs / lead_records shape as it existed before this refactor.
OLD_SCHEMA = (
    """
    CREATE TABLE search_jobs (
        id INTEGER PRIMARY KEY,
        name VARCHAR(255),
        prompt TEXT,
        filters JSON,
        linkedin_url TEXT,
        query_used TEXT,
        mode VARCHAR(50),
        status VARCHAR(50),
        message TEXT,
        owner_id INTEGER,
        organization_id INTEGER,
        created_at DATETIME
    )
    """,
    """
    CREATE TABLE lead_records (
        id INTEGER PRIMARY KEY,
        search_job_id INTEGER,
        name VARCHAR(255),
        email VARCHAR(255),
        phone VARCHAR(64),
        personal_location VARCHAR(255),
        ai_description TEXT,
        work_field VARCHAR(255),
        work_designation VARCHAR(255),
        company_name VARCHAR(255),
        company_location VARCHAR(255),
        linkedin_url TEXT,
        relevance_score FLOAT,
        activity_status VARCHAR(64),
        raw_snippet TEXT,
        tags JSON,
        created_at DATETIME
    )
    """,
)


@pytest.fixture
def legacy_engine(tmp_path: Path):
    engine = create_engine(f"sqlite:///{tmp_path / 'legacy.db'}")
    with engine.begin() as conn:
        for statement in OLD_SCHEMA:
            conn.execute(text(statement))
        conn.execute(
            text(
                "INSERT INTO search_jobs (id, name, prompt, status, owner_id, mode) "
                "VALUES (1, 'Old search', 'marketing managers', 'COMPLETED', 1, 'public')"
            )
        )
        conn.execute(
            text(
                "INSERT INTO lead_records (id, search_job_id, name, email, relevance_score) "
                "VALUES (1, 1, 'Anna Lind', 'anna@example.com', 0.7)"
            )
        )
    yield engine
    engine.dispose()


def upgrade(engine) -> None:
    Base.metadata.create_all(bind=engine)
    _normalize_search_job_statuses(engine)
    _ensure_columns(engine)
    _ensure_indexes(engine)


def test_new_columns_are_added_to_existing_tables(legacy_engine):
    upgrade(legacy_engine)

    columns = {c["name"] for c in inspect(legacy_engine).get_columns("search_jobs")}
    assert {
        "progress",
        "current_stage",
        "trace_id",
        "started_at",
        "completed_at",
        "error_message",
        "cancel_requested",
        "total_queries",
        "candidates_found",
        "qualified_leads",
        "emails_found",
        "phones_found",
        "errors_count",
    } <= columns


def test_lead_identity_and_enrichment_columns_are_added(legacy_engine):
    upgrade(legacy_engine)

    columns = {c["name"] for c in inspect(legacy_engine).get_columns("lead_records")}
    assert {
        "identity_key",
        "linkedin_url_normalized",
        "candidate_id",
        "enrichment_status",
        "enrichment_attempts",
        "email_status",
        "email_verified_at",
    } <= columns


def test_new_tables_are_created(legacy_engine):
    upgrade(legacy_engine)

    tables = set(inspect(legacy_engine).get_table_names())
    assert {"search_candidates", "search_query_runs", "job_queue"} <= tables


def test_new_indexes_are_added_to_existing_tables(legacy_engine):
    upgrade(legacy_engine)

    insp = inspect(legacy_engine)
    lead_indexes = {ix["name"] for ix in insp.get_indexes("lead_records")}
    job_indexes = {ix["name"] for ix in insp.get_indexes("search_jobs")}
    assert "uq_lead_search_identity" in lead_indexes
    assert "ix_lead_records_job_score" in lead_indexes
    assert "ix_lead_records_email" in lead_indexes
    assert "ix_search_jobs_owner_created" in job_indexes


def test_existing_rows_survive_the_upgrade(legacy_engine):
    upgrade(legacy_engine)

    with legacy_engine.begin() as conn:
        job = conn.execute(
            text("SELECT name, status, progress, candidates_found FROM search_jobs WHERE id = 1")
        ).one()
        lead = conn.execute(
            text("SELECT name, email, enrichment_status FROM lead_records WHERE id = 1")
        ).one()

    assert job.name == "Old search"
    # The uppercase legacy status is normalized on the way through.
    assert job.status == "completed"
    assert job.progress == 0
    assert job.candidates_found == 0
    assert lead.name == "Anna Lind"
    assert lead.email == "anna@example.com"
    assert lead.enrichment_status == "pending"


def test_upgrade_is_idempotent(legacy_engine):
    upgrade(legacy_engine)
    upgrade(legacy_engine)

    with legacy_engine.begin() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM lead_records")).scalar() == 1


def test_legacy_null_identity_keys_do_not_break_the_unique_index(legacy_engine):
    """Pre-existing rows have no identity_key; several NULLs must coexist."""
    upgrade(legacy_engine)

    with legacy_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO lead_records (id, search_job_id, name) VALUES (2, 1, 'Erik Ohlsson')"
            )
        )
        conn.execute(
            text("INSERT INTO lead_records (id, search_job_id, name) VALUES (3, 1, 'Lena Berg')")
        )
        assert conn.execute(text("SELECT COUNT(*) FROM lead_records")).scalar() == 3


def test_datetime_ddl_is_translated_for_postgres():
    """Postgres rejects DATETIME, so the portable spelling must be mapped."""
    from sqlalchemy.dialects import postgresql

    class _Bind:
        dialect = postgresql.dialect()

    assert _column_ddl("DATETIME", _Bind()) == "TIMESTAMP"
    assert _column_ddl("INTEGER DEFAULT 0", _Bind()) == "INTEGER DEFAULT 0"
    assert _column_ddl("BOOLEAN DEFAULT FALSE", _Bind()) == "BOOLEAN DEFAULT FALSE"


def test_datetime_ddl_is_left_alone_for_sqlite(legacy_engine):
    assert _column_ddl("DATETIME", legacy_engine) == "DATETIME"
