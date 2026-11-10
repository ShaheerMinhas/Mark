"""Postgres migration and multi-worker races. Skipped unless MARK_POSTGRES_TEST_URL is set.

The database is local and ephemeral. These tests do not touch a deployed service.
"""

from __future__ import annotations

import os
import threading
from datetime import datetime

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

pytestmark = pytest.mark.skipif(not os.getenv("MARK_POSTGRES_TEST_URL"), reason="local Postgres URL is not set")

URL = os.getenv("MARK_POSTGRES_TEST_URL", "")


def _engine():
    return create_engine(URL, pool_size=8, max_overflow=8)


def _reset(engine) -> None:
    from backend.database import Base

    Base.metadata.drop_all(bind=engine)
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS schema_migrations"))


def test_fresh_and_legacy_postgres_migrations_keep_private_rows():
    from backend.database import upgrade_database

    engine = _engine()
    _reset(engine)
    upgrade_database(engine)
    upgrade_database(engine)
    with engine.connect() as conn:
        versions = {row[0] for row in conn.execute(text("SELECT version FROM schema_migrations"))}
        assert versions >= {1, 2, 3, 4}
        assert conn.execute(text("SELECT COUNT(*) FROM global_people")).scalar() == 0
    _reset(engine)
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                CREATE TABLE search_jobs (
                    id INTEGER PRIMARY KEY,
                    name VARCHAR(255),
                    prompt TEXT,
                    status VARCHAR(50),
                    owner_id INTEGER,
                    mode VARCHAR(50)
                )
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE TABLE lead_records (
                    id INTEGER PRIMARY KEY,
                    search_job_id INTEGER,
                    name VARCHAR(255),
                    email VARCHAR(255)
                )
                """
            )
        )
        conn.execute(
            text("INSERT INTO search_jobs (id, name, prompt, status, owner_id, mode) VALUES (1, 'Old', 'q', 'COMPLETED', 1, 'public')")
        )
        conn.execute(text("INSERT INTO lead_records (id, search_job_id, name, email) VALUES (1, 1, 'Anna Lind', 'anna@example.com')"))
    upgrade_database(engine)
    with engine.connect() as conn:
        assert conn.execute(text("SELECT name FROM lead_records WHERE id = 1")).scalar() == "Anna Lind"
        assert conn.execute(text("SELECT COUNT(*) FROM global_people")).scalar() == 0
        assert conn.execute(text("SELECT status FROM search_jobs WHERE id = 1")).scalar() == "completed"
    engine.dispose()


def test_postgres_queue_budget_and_upsert_races():
    from backend.database import upgrade_database
    from backend.models import DiscoveryReservation, GlobalPerson, JobType
    from backend.services.discovery_engine.spend import BudgetDenied, reserve_max, seed_providers, set_budget, set_price
    from backend.services.global_cache.upsert import PublicProspect, upsert_public_prospect
    from backend.services.jobqueue import queue

    engine = _engine()
    _reset(engine)
    upgrade_database(engine)
    Session = sessionmaker(bind=engine)
    setup = Session()
    queue.enqueue(setup, JobType.DISCOVERY_PIPELINE.value, {"run_id": 1})
    queue.enqueue(setup, JobType.DISCOVERY_PIPELINE.value, {"run_id": 2})
    seed_providers(setup)
    set_price(setup, provider_name="hunter", operation="email_find", currency="USD", max_cost_minor=100)
    for scope, name, period in (
        ("global", "", "day"),
        ("global", "", "month"),
        ("provider", "hunter", "day"),
        ("provider", "hunter", "month"),
    ):
        set_budget(setup, scope=scope, provider_name=name, period=period, currency="USD", limit_minor=100)
    setup.close()

    claimed = []

    def claim(worker_id):
        session = Session()
        try:
            handle = queue.claim(session, worker_id=worker_id, job_types=[JobType.DISCOVERY_PIPELINE.value])
            claimed.append(None if handle is None else handle.id)
        finally:
            session.close()

    threads = [threading.Thread(target=claim, args=(f"w{index}",)) for index in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(item for item in claimed if item is not None) == [1, 2] or len(set(claimed)) == 2
    assert None not in claimed

    results = []

    def reserve(key):
        session = Session()
        try:
            reserve_max(session, idempotency_key=key, provider_name="hunter", operation="email_find", run_id=1, person_id=1, attempt=1)
            results.append("ok")
        except BudgetDenied:
            results.append("denied")
        finally:
            session.close()

    threads = [threading.Thread(target=reserve, args=(f"pg-{index}",)) for index in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(results) == ["denied", "ok"]

    again = Session()
    existing = again.query(DiscoveryReservation).one()
    same = reserve_max(
        again,
        idempotency_key=existing.idempotency_key,
        provider_name="hunter",
        operation="email_find",
        run_id=1,
        person_id=1,
        attempt=1,
    )
    assert same.id == existing.id
    assert again.query(DiscoveryReservation).count() == 1
    again.close()

    def plant():
        session = Session()
        try:
            upsert_public_prospect(
                session,
                PublicProspect(
                    full_name="Example Postgres Nora",
                    title="CTO",
                    company_name="Example Postgres",
                    domain="example-postgres.test",
                    profile_url="https://www.linkedin.com/in/example-postgres-nora",
                    country_code="SE",
                ),
            )
        finally:
            session.close()

    threads = [threading.Thread(target=plant) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    check = Session()
    assert check.query(GlobalPerson).count() == 1
    check.close()
    engine.dispose()


def test_postgres_distribution_last_slot_is_atomic():
    from backend.database import upgrade_database
    from backend.services.discovery_engine.distribution import ensure_policy, try_allocate

    engine = _engine()
    _reset(engine)
    upgrade_database(engine)
    Session = sessionmaker(bind=engine)
    setup = Session()
    ensure_policy(setup)
    setup.commit()
    for index in range(2):
        assert try_allocate(
            setup,
            canonical_key="profile:https://www.linkedin.com/in/example-slot",
            customer_key=f"user:{index}",
            person_id=None,
            search_job_id=None,
            lead_record_id=None,
        ) == "allocated"
        setup.commit()
    setup.close()
    results = []

    def claim(customer):
        session = Session()
        try:
            results.append(
                try_allocate(
                    session,
                    canonical_key="profile:https://www.linkedin.com/in/example-slot",
                    customer_key=customer,
                    person_id=None,
                    search_job_id=None,
                    lead_record_id=None,
                )
            )
            session.commit()
        finally:
            session.close()

    threads = [threading.Thread(target=claim, args=(f"user:{index}",)) for index in (2, 3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(results) == ["allocated", "saturated"]
    engine.dispose()
