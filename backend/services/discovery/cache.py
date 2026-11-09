"""Public-query result cache.

Repeated and overlapping searches hit the same ``site:linkedin.com/in`` queries,
and every live call costs ~1.8s of throttle plus rate-limit risk. Caching the raw
hits makes repeat searches faster and markedly more reliable.

The cache key is the query text and page only. It deliberately contains nothing
user-specific: no owner, no organization, no permissions. Qualification and
scoring always re-run per requesting user, so two users issuing the same public
query share the SERP payload but never each other's results.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.config import SEARCH_CACHE_HOURS
from backend.models import SearchQueryCache
from backend.observability import log_event

logger = logging.getLogger(__name__)


def enabled() -> bool:
    return SEARCH_CACHE_HOURS > 0


def get_cached(db: Session, query_hash: str) -> list[dict[str, Any]] | None:
    """Return cached hits, or ``None`` on a miss or expiry."""
    if not enabled() or not query_hash:
        return None
    row = (
        db.query(SearchQueryCache)
        .filter(SearchQueryCache.query_hash == query_hash)
        .first()
    )
    if row is None:
        return None
    if row.expires_at <= datetime.utcnow():
        db.delete(row)
        db.commit()
        return None
    hits = list(row.hits or [])
    if "site:linkedin.com" in (row.query_text or "").lower():
        if not any("linkedin.com" in str(h.get("href") or "").lower() for h in hits):
            db.delete(row)
            db.commit()
            return None
    return hits


def store(
    db: Session,
    *,
    query_hash: str,
    query_text: str,
    page: int,
    hits: list[dict[str, Any]],
) -> None:
    """Cache hits for a query. Empty results are not cached, so a transient
    zero-result response does not poison later searches."""
    if not enabled() or not query_hash or not hits:
        return
    if "site:linkedin.com" in (query_text or "").lower():
        hits = [h for h in hits if "linkedin.com" in str(h.get("href") or "").lower()]
        if not hits:
            return
    expires_at = datetime.utcnow() + timedelta(hours=SEARCH_CACHE_HOURS)
    existing = (
        db.query(SearchQueryCache)
        .filter(SearchQueryCache.query_hash == query_hash)
        .first()
    )
    if existing is not None:
        existing.hits = hits
        existing.hit_count = len(hits)
        existing.expires_at = expires_at
        db.commit()
        return
    db.add(
        SearchQueryCache(
            query_hash=query_hash,
            query_text=query_text,
            page=page,
            hits=hits,
            hit_count=len(hits),
            expires_at=expires_at,
        )
    )
    try:
        db.commit()
    except IntegrityError:
        # Another worker cached the same query first; its copy is just as good.
        db.rollback()


def purge_expired(db: Session) -> int:
    removed = (
        db.query(SearchQueryCache)
        .filter(SearchQueryCache.expires_at <= datetime.utcnow())
        .delete(synchronize_session=False)
    )
    if removed:
        db.commit()
        log_event(logger, "query_cache.purged", count=removed)
    return int(removed)
