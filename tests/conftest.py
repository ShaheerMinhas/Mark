"""Shared fixtures. No test in this suite is allowed to touch the real internet."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

# Point the app at a throwaway SQLite file and disable the inline worker before any
# backend module reads configuration at import time.
_TMP_DIR = tempfile.mkdtemp(prefix="mark-tests-")
os.environ["MARK_DATABASE_URL"] = f"sqlite:///{Path(_TMP_DIR) / 'test.db'}"
os.environ["MARK_INLINE_WORKER"] = "0"
os.environ.pop("OPENAI_API_KEY", None)
os.environ.pop("HUNTER_API_KEY", None)

from backend.database import Base, SessionLocal, engine, init_db  # noqa: E402
from backend.services import provider_health  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def _schema():
    init_db()
    yield
    Base.metadata.drop_all(bind=engine)


@pytest.fixture(autouse=True)
def _clean_tables(_schema):
    """Truncate between tests so counters and dedup state never leak across cases."""
    yield
    with engine.begin() as conn:
        for table in reversed(Base.metadata.sorted_tables):
            conn.exec_driver_sql(f"DELETE FROM {table.name}")
    provider_health.reset()


@pytest.fixture
def db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """Fail loudly if any code under test tries a real HTTP or search call.

    ``httpx.Client`` has to be blocked too: the email and company modules use the
    context-manager form, so patching only the module-level helpers lets real
    requests through.
    """

    def _blocked(*_args, **_kwargs):
        raise AssertionError("network access attempted in a test")

    import httpx

    monkeypatch.setattr(httpx, "get", _blocked, raising=False)
    monkeypatch.setattr(httpx, "post", _blocked, raising=False)
    monkeypatch.setattr(httpx, "Client", _blocked, raising=False)
    monkeypatch.setattr("backend.services.ddg._make_client", _blocked, raising=False)
    # The shared throttle sleeps ~1.8s per query; pointless in tests.
    monkeypatch.setattr("backend.services.ddg._throttle", lambda: None)
    # Same for the per-engine throttles in the multi-engine dispatcher.
    monkeypatch.setattr(
        "backend.services.search_engines.base.Throttle.wait", lambda self: None
    )
    # Engine health is process-global; leaking a benched engine between tests
    # would make later tests fail for the wrong reason.
    from backend.services.search_engines import health as _engine_health

    _engine_health.reset()
    # Likewise the retry backoff. Tests that assert on the delay re-patch this.
    monkeypatch.setattr(
        "backend.services.discovery.runner.time.sleep", lambda _s: None, raising=False
    )


@pytest.fixture
def user(db):
    from backend.auth_utils import hash_password
    from backend.models import User

    record = User(
        email="owner@example.com",
        full_name="Owner Example",
        hashed_password=hash_password("test-password-123"),
    )
    db.add(record)
    db.commit()
    db.refresh(record)
    return record


def serp_hit(slug: str, name: str, role: str, company: str, body: str = "") -> dict:
    """A SERP hit shaped like the ones DuckDuckGo returns for a profile."""
    return {
        "href": f"https://www.linkedin.com/in/{slug}",
        "title": f"{name} - {role} - {company} | LinkedIn",
        "body": body or f"{role} at {company}. Experience: {company}. Austin, TX.",
    }
