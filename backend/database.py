from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from backend.config import DATABASE_URL

connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
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


def init_db():
    from backend import models  # noqa: F401

    Base.metadata.create_all(bind=engine)
    _normalize_search_job_statuses()
