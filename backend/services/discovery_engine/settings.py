"""Global discovery switches. Defaults are off. Paid calls stay off."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session

from backend.models import DiscoverySettings

# Phase 2 has no budget ledger. This stays false even if a row is edited.
LIVE_PUBLIC_SEARCH_ENABLED = False
PAID_PROVIDERS_ENABLED = False


def ensure_settings(db: Session) -> DiscoverySettings:
    row = db.get(DiscoverySettings, 1)
    if row is None:
        row = DiscoverySettings(
            id=1,
            enabled=False,
            live_public_search_enabled=False,
            paid_providers_enabled=False,
            max_concurrency=1,
            max_runs_per_tick=5,
            updated_at=datetime.utcnow(),
        )
        db.add(row)
        db.flush()
    row.live_public_search_enabled = False
    row.paid_providers_enabled = False
    if not row.max_runs_per_hour:
        row.max_runs_per_hour = 4
    if not row.max_runs_per_day:
        row.max_runs_per_day = 24
    return row


def update_limits(db: Session, *, enabled: bool, max_concurrency: int, max_runs_per_tick: int, max_runs_per_hour: int, max_runs_per_day: int) -> DiscoverySettings:
    if not 1 <= max_concurrency <= 8:
        raise ValueError("max_concurrency must be 1..8")
    if not 1 <= max_runs_per_tick <= 20:
        raise ValueError("max_runs_per_tick must be 1..20")
    if not 1 <= max_runs_per_hour <= 48:
        raise ValueError("max_runs_per_hour must be 1..48")
    if not 1 <= max_runs_per_day <= 200:
        raise ValueError("max_runs_per_day must be 1..200")
    row = ensure_settings(db)
    row.enabled = bool(enabled)
    row.max_concurrency = max_concurrency
    row.max_runs_per_tick = max_runs_per_tick
    row.max_runs_per_hour = max_runs_per_hour
    row.max_runs_per_day = max_runs_per_day
    row.live_public_search_enabled = False
    row.paid_providers_enabled = False
    row.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(row)
    return row
