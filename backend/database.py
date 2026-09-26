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


def _normalize_search_job_statuses() -> None:
    """Fix legacy status strings (e.g. COMPLETED vs completed) after enum rollout."""
    from sqlalchemy import text

    fixes = {
        "COMPLETED": "completed",
        "PENDING": "pending",
        "RUNNING": "running",
        "FAILED": "failed",
    }
    with engine.begin() as conn:
        for old, new in fixes.items():
            conn.execute(
                text("UPDATE search_jobs SET status = :new WHERE status = :old"),
                {"old": old, "new": new},
            )


def _ensure_search_job_columns() -> None:
    from sqlalchemy import inspect, text

    insp = inspect(engine)
    if "search_jobs" not in insp.get_table_names():
        return
    cols = {c["name"] for c in insp.get_columns("search_jobs")}
    if "progress" not in cols:
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE search_jobs ADD COLUMN progress INTEGER DEFAULT 0"))


_LEAD_EMAIL_COLUMNS = (
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
)


def _ensure_lead_email_columns() -> None:
    from sqlalchemy import inspect, text

    insp = inspect(engine)
    if "lead_records" not in insp.get_table_names():
        return
    cols = {c["name"] for c in insp.get_columns("lead_records")}
    with engine.begin() as conn:
        for name, ddl in _LEAD_EMAIL_COLUMNS:
            if name not in cols:
                conn.execute(text(f"ALTER TABLE lead_records ADD COLUMN {name} {ddl}"))


def init_db():
    from backend import models  # noqa: F401

    Base.metadata.create_all(bind=engine)
    _normalize_search_job_statuses()
    _ensure_search_job_columns()
    _ensure_lead_email_columns()
    try:
        from backend.services.dbwiki import refresh_dbwiki

        refresh_dbwiki()
    except Exception:
        pass
