"""Candidate persistence and deduplication.

Candidates are written as each query returns, so a search that dies partway
through still leaves its discovered pool behind. Deduplication is enforced by the
``(search_job_id, identity_key)`` unique index rather than by in-memory
bookkeeping alone, so a retried job cannot double-insert.
"""

from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.models import CandidateStatus, SearchCandidate
from backend.observability import log_event
from backend.services.discovery.extract import ExtractedCandidate

logger = logging.getLogger(__name__)


def _row(search_job_id: int, candidate: ExtractedCandidate, query_text: str) -> SearchCandidate:
    return SearchCandidate(
        search_job_id=search_job_id,
        identity_key=candidate.identity_key,
        name=(candidate.name or "")[:255] or None,
        title=(candidate.title or "")[:255] or None,
        company=(candidate.company or "")[:255] or None,
        location=(candidate.location or "")[:255] or None,
        linkedin_url=candidate.linkedin_url or None,
        linkedin_url_normalized=(candidate.linkedin_url or "")[:255] or None,
        source_url=candidate.source_url or None,
        query_text=query_text,
        raw_snippet=candidate.raw_snippet or None,
        status=CandidateStatus.DISCOVERED.value,
        discovered_at=datetime.utcnow(),
    )


class CandidateWriter:
    """Buffers known identity keys for one search to avoid a query per candidate."""

    def __init__(self, db: Session, search_job_id: int) -> None:
        self.db = db
        self.search_job_id = search_job_id
        self._seen: set[str] = {
            key
            for (key,) in db.query(SearchCandidate.identity_key)
            .filter(SearchCandidate.search_job_id == search_job_id)
            .all()
        }

    @property
    def known_count(self) -> int:
        return len(self._seen)

    def add_many(
        self, candidates: list[ExtractedCandidate], *, query_text: str
    ) -> tuple[int, int]:
        """Persist new candidates. Returns ``(inserted, duplicates)``."""
        fresh: list[ExtractedCandidate] = []
        duplicates = 0
        for candidate in candidates:
            if candidate.identity_key in self._seen:
                duplicates += 1
                continue
            self._seen.add(candidate.identity_key)
            fresh.append(candidate)

        if not fresh:
            return 0, duplicates

        for candidate in fresh:
            self.db.add(_row(self.search_job_id, candidate, query_text))
        try:
            self.db.commit()
            return len(fresh), duplicates
        except IntegrityError:
            # A retry of this job already wrote one of these keys. Fall back to
            # per-row inserts so a single conflict cannot drop the whole batch.
            self.db.rollback()

        inserted = 0
        for candidate in fresh:
            try:
                self.db.add(_row(self.search_job_id, candidate, query_text))
                self.db.commit()
                inserted += 1
            except IntegrityError:
                self.db.rollback()
                duplicates += 1
            except Exception:
                self.db.rollback()
                log_event(
                    logger,
                    "candidate.write_failed",
                    level=logging.WARNING,
                    search_id=self.search_job_id,
                )
        return inserted, duplicates


def candidate_counts(db: Session, search_job_id: int) -> dict[str, int]:
    """Discovered / qualified / rejected totals for one search."""
    counts = {status.value: 0 for status in CandidateStatus}
    rows = (
        db.query(SearchCandidate.status, func.count(SearchCandidate.id))
        .filter(SearchCandidate.search_job_id == search_job_id)
        .group_by(SearchCandidate.status)
        .all()
    )
    for status, count in rows:
        counts[status] = int(count)
    counts["total"] = sum(int(count) for _, count in rows)
    return counts
