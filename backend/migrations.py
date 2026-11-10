"""Versioned schema steps.

``create_all`` builds missing tables. It does not add columns to tables that
already exist, and it does not remember what ran. These steps do. Existing
workspace rows are never copied into the shared cache.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import inspect, text
from sqlalchemy.orm import Session

from backend.database import Base, engine


def _versions(bind) -> set[int]:
    insp = inspect(bind)
    if "schema_migrations" not in set(insp.get_table_names()):
        return set()
    with bind.connect() as conn:
        rows = conn.execute(text("SELECT version FROM schema_migrations")).all()
    return {int(row[0]) for row in rows}


def _record(bind, version: int, name: str) -> None:
    with bind.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO schema_migrations (version, name, applied_at) VALUES (:version, :name, :applied_at)"
            ),
            {"version": version, "name": name, "applied_at": datetime.utcnow()},
        )


def _step_cache_tables(bind) -> None:
    """Ensure the shared cache tables exist. Does not read or copy lead_records."""
    Base.metadata.create_all(
        bind=bind,
        tables=[
            Base.metadata.tables[name]
            for name in (
                "schema_migrations",
                "global_companies",
                "global_people",
                "global_person_technologies",
                "global_evidence",
                "global_identity_keys",
                "cache_policies",
            )
            if name in Base.metadata.tables
        ],
    )
    from backend.services.global_cache.policy import ensure_default_policy

    session = Session(bind=bind)
    try:
        ensure_default_policy(session)
        session.commit()
    finally:
        session.close()


def _step_scheduler(bind) -> None:
    """Scheduler tables. Does not enable discovery and does not copy private leads."""
    names = (
        "suppression_entries",
        "discovery_settings",
        "discovery_targets",
        "discovery_runs",
        "discovery_stage_attempts",
        "scheduler_locks",
    )
    Base.metadata.create_all(
        bind=bind,
        tables=[Base.metadata.tables[name] for name in names if name in Base.metadata.tables],
    )
    from backend.services.discovery_engine.settings import ensure_settings

    session = Session(bind=bind)
    try:
        ensure_settings(session)
        session.commit()
    finally:
        session.close()


STEPS: tuple[tuple[int, str, object], ...] = (
    (1, "global_discovery_cache", _step_cache_tables),
    (2, "discovery_scheduler", _step_scheduler),
)


def apply_migrations(bind=None) -> list[int]:
    bind = bind or engine
    Base.metadata.create_all(bind=bind, tables=[Base.metadata.tables["schema_migrations"]])
    applied = _versions(bind)
    ran: list[int] = []
    for version, name, step in STEPS:
        if version in applied:
            continue
        step(bind)
        if version not in _versions(bind):
            _record(bind, version, name)
        ran.append(version)
    return ran
