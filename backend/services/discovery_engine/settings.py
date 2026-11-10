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
    return row
