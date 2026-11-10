"""Turn public hit dicts into shared cache rows.

The default collector does not call the legacy search clients. Those clients
fetch arbitrary result URLs and are not covered by the outbound URL policy.
A test, or a future cleared source, can replace ``collect_public_hits``.
"""

from __future__ import annotations

from backend.models import GlobalPerson
from backend.services.discovery_engine.ssrf import public_http_url
from backend.services.global_cache.upsert import PrivateEvidenceError, PublicProspect, upsert_public_prospect


def collect_public_hits(target) -> tuple[list[dict], str]:
    del target
    return [], (
        "Live public search is not cleared for network fetching. "
        "The legacy search clients are outside the outbound URL policy. No request was sent."
    )


def ingest_hits(db, hits: list[dict]) -> dict:
    """Normalize, dedup, and store public hits. Suppression skips a hit. Nothing private is copied."""
    before = db.query(GlobalPerson.id).count()
    suppressed = 0
    for hit in hits:
        prospect = PublicProspect(
            full_name=str(hit.get("full_name") or ""),
            title=hit.get("title"),
            company_name=hit.get("company_name"),
            domain=hit.get("domain"),
            industry=hit.get("industry"),
            country_code=hit.get("country_code"),
            city=hit.get("city"),
            profile_url=hit.get("profile_url"),
            email=hit.get("email"),
            email_status=hit.get("email_status"),
            phone=hit.get("phone") if hit.get("phone_evidenced") else None,
            phone_evidenced=bool(hit.get("phone_evidenced")),
            share_scope="public",
            source_url=public_http_url(hit.get("source_url")),
        )
        try:
            upsert_public_prospect(db, prospect)
        except PrivateEvidenceError:
            suppressed += 1
            db.rollback()
    after = db.query(GlobalPerson.id).count()
    ingested = max(0, after - before)
    return {
        "ingested": ingested,
        "suppressed": suppressed,
        "duplicates": max(0, len(hits) - ingested - suppressed),
    }
