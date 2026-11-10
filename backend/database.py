from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from backend.config import DATABASE_URL

connect_args = {"check_same_thread": False, "timeout": 30} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, connect_args=connect_args)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _normalize_search_job_statuses(bind=None) -> None:
    """Fix legacy status strings (e.g. COMPLETED vs completed) after enum rollout."""
    from sqlalchemy import text

    fixes = {
        "COMPLETED": "completed",
        "PENDING": "pending",
        "RUNNING": "running",
        "FAILED": "failed",
    }
    with (bind or engine).begin() as conn:
        for old, new in fixes.items():
            conn.execute(
                text("UPDATE search_jobs SET status = :new WHERE status = :old"),
                {"old": old, "new": new},
            )


#: Portable DDL spellings, translated per dialect by :func:`_column_ddl`.
_DIALECT_TYPES = {
    "postgresql": {"DATETIME": "TIMESTAMP"},
}


def _column_ddl(ddl: str, bind=None) -> str:
    overrides = _DIALECT_TYPES.get((bind or engine).dialect.name, {})
    for portable, native in overrides.items():
        if ddl.upper().startswith(portable):
            return native + ddl[len(portable) :]
    return ddl


_ADDED_COLUMNS: dict[str, tuple[tuple[str, str], ...]] = {
    "users": (
        ("is_platform_admin", "BOOLEAN DEFAULT FALSE"),
        ("admin_token_version", "INTEGER DEFAULT 0"),
    ),
    "discovery_settings": (
        ("max_runs_per_hour", "INTEGER DEFAULT 4"),
        ("max_runs_per_day", "INTEGER DEFAULT 24"),
        ("pause_enrichment", "BOOLEAN DEFAULT 1"),
        ("pause_email", "BOOLEAN DEFAULT 1"),
        ("pause_verify", "BOOLEAN DEFAULT 1"),
        ("allow_public_search", "BOOLEAN DEFAULT 0"),
        ("retention_days", "INTEGER"),
    ),
    "provider_usage": (
        ("scope", "VARCHAR(16)"),
        ("reservation_id", "INTEGER"),
    ),
    "global_people": (
        ("archived_at", "DATETIME"),
        ("correction_note", "TEXT"),
    ),
    "global_companies": (
        ("archived_at", "DATETIME"),
        ("correction_note", "TEXT"),
    ),
    "discovery_targets": (("criteria", "JSON"),),
    "search_jobs": (
        ("progress", "INTEGER DEFAULT 0"),
        ("current_stage", "VARCHAR(64)"),
        ("trace_id", "VARCHAR(64)"),
        ("started_at", "DATETIME"),
        ("completed_at", "DATETIME"),
        ("error_message", "TEXT"),
        ("cancel_requested", "BOOLEAN DEFAULT FALSE"),
        ("total_queries", "INTEGER DEFAULT 0"),
        ("completed_queries", "INTEGER DEFAULT 0"),
        ("failed_queries", "INTEGER DEFAULT 0"),
        ("candidates_found", "INTEGER DEFAULT 0"),
        ("candidates_deduplicated", "INTEGER DEFAULT 0"),
        ("qualified_leads", "INTEGER DEFAULT 0"),
        ("enrichment_attempted", "INTEGER DEFAULT 0"),
        ("enrichment_completed", "INTEGER DEFAULT 0"),
        ("emails_found", "INTEGER DEFAULT 0"),
        ("phones_found", "INTEGER DEFAULT 0"),
        ("errors_count", "INTEGER DEFAULT 0"),
        ("cache_served", "INTEGER DEFAULT 0"),
    ),
    "lead_records": (
        ("company_domain", "VARCHAR(255)"),
        ("email_status", "VARCHAR(32)"),
        ("email_confidence", "VARCHAR(16)"),
        ("email_source", "VARCHAR(64)"),
        ("email_pattern", "VARCHAR(64)"),
        ("email_source_url", "TEXT"),
        ("email_evidence", "JSON"),
        ("email_verification", "JSON"),
        ("email_verified_at", "DATETIME"),
        ("email_enriched_at", "DATETIME"),
        ("identity_key", "VARCHAR(64)"),
        ("linkedin_url_normalized", "VARCHAR(255)"),
        ("candidate_id", "INTEGER"),
        ("enrichment_status", "VARCHAR(32) DEFAULT 'pending'"),
        ("enrichment_attempts", "INTEGER DEFAULT 0"),
        ("enrichment_error", "VARCHAR(255)"),
        ("enrichment_locked_until", "DATETIME"),
        ("phone_source", "VARCHAR(64)"),
        ("crm_stage", "VARCHAR(32) DEFAULT 'new'"),
        ("crm_notes", "TEXT"),
        ("lead_status", "VARCHAR(32) DEFAULT 'new'"),
        ("priority", "VARCHAR(16) DEFAULT 'medium'"),
        ("assigned_user_id", "INTEGER"),
        ("follow_up_date", "DATETIME"),
        ("archived_at", "DATETIME"),
        ("custom_fields", "JSON"),
        ("score_breakdown", "JSON"),
        ("updated_at", "DATETIME"),
    ),
}

#: Tables introduced by the pipeline refactor. ``create_all`` builds these whole,
#: so they need no per-column migration entries.


def _ensure_columns(bind=None) -> None:
    from sqlalchemy import inspect, text

    bind = bind or engine
    insp = inspect(bind)
    tables = set(insp.get_table_names())
    for table, specs in _ADDED_COLUMNS.items():
        if table not in tables:
            continue  # create_all already built it with every column
        existing = {c["name"] for c in insp.get_columns(table)}
        with bind.begin() as conn:
            for name, ddl in specs:
                if name in existing:
                    continue
                conn.execute(
                    text(f"ALTER TABLE {table} ADD COLUMN {name} {_column_ddl(ddl, bind)}")
                )


def _ensure_indexes(bind=None) -> None:
    """Create any model-declared index missing from an existing table.

    ``create_all`` only builds indexes alongside a brand-new table, so tables that
    predate a model change never pick up new indexes without this.
    """
    import logging

    from sqlalchemy import inspect, text

    bind = bind or engine
    insp = inspect(bind)
    tables = set(insp.get_table_names())
    for table in Base.metadata.sorted_tables:
        if table.name not in tables or not table.indexes:
            continue
        existing = {ix.get("name") for ix in insp.get_indexes(table.name)}
        for index in table.indexes:
            if not index.name or index.name in existing:
                continue
            columns = [c.name for c in index.columns]
            if not columns:
                continue
            unique = "UNIQUE " if index.unique else ""
            statement = (
                f"CREATE {unique}INDEX IF NOT EXISTS {index.name} "
                f"ON {table.name} ({', '.join(columns)})"
            )
            try:
                with bind.begin() as conn:
                    conn.execute(text(statement))
            except Exception:
                # A pre-existing duplicate can block a unique index. Log and continue
                # rather than taking the whole app down on boot.
                logging.getLogger(__name__).warning(
                    "could not create index %s on %s", index.name, table.name, exc_info=True
                )


def upgrade_database(bind=None) -> None:
    """Bring a fresh or already-populated database up to the current schema.

    Safe to run more than once. Does not copy private leads into the shared cache.
    """
    from backend import models  # noqa: F401

    bind = bind or engine
    Base.metadata.create_all(bind=bind)
    _normalize_search_job_statuses(bind)
    _ensure_columns(bind)
    _ensure_indexes(bind)
    from backend.migrations import apply_migrations

    apply_migrations(bind)


def init_db():
    from backend import models  # noqa: F401

    upgrade_database(engine)
    try:
        from backend.services.dbwiki import refresh_dbwiki

        refresh_dbwiki()
    except Exception:
        pass
