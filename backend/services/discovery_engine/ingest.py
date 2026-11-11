"""Turn public hit dicts into shared cache rows.

The default collector does not call the legacy search clients. Those clients
fetch arbitrary result URLs and are not covered by the outbound URL policy.
A test, or a future cleared source, can replace ``collect_public_hits``.
"""

from __future__ import annotations

from backend.models import GlobalEvidence, GlobalPerson
from backend.services.discovery_engine.ssrf import public_http_url
from backend.services.global_cache.upsert import PrivateEvidenceError, PublicProspect, upsert_public_prospect


def collect_public_hits(target, db=None) -> tuple[list[dict], str]:
    from backend.services.discovery_engine.public_source import search_public_profiles

    return search_public_profiles(target, db=db)


def _search_backend_evidence(db, person_id: int, hit: dict) -> None:
    """One evidence row per engine that returned this profile."""
    backends = []
    for item in hit.get("backends") or []:
        name = str(item or "").strip()[:64]
        if name and name not in backends:
            backends.append(name)
    if not backends:
        return
    source = public_http_url(hit.get("source_url"))
    profile = str(hit.get("profile_url") or "")[:2000]
    for backend in backends:
        existing = (
            db.query(GlobalEvidence)
            .filter(
                GlobalEvidence.subject_type == "person",
                GlobalEvidence.subject_id == person_id,
                GlobalEvidence.field_name == "search_backend",
                GlobalEvidence.provider == backend,
            )
            .first()
        )
        if existing:
            continue
        db.add(
            GlobalEvidence(
                subject_type="person",
                subject_id=person_id,
                field_name="search_backend",
                value_text=profile or backend,
                source_url=source,
                provider=backend,
                share_scope="public",
                eligible=True,
            )
        )


def ingest_hits(db, hits: list[dict]) -> dict:
    """Normalize, dedup, and store public hits. Suppression skips a hit. Nothing private is copied."""
    before = db.query(GlobalPerson.id).count()
    suppressed = 0
    new_ids: list[int] = []
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
            email_confidence=hit.get("email_confidence"),
            phone=hit.get("phone") if hit.get("phone_evidenced") else None,
            phone_evidenced=bool(hit.get("phone_evidenced")),
            share_scope="public",
            source_url=public_http_url(hit.get("source_url")),
            provider=hit.get("provider"),
            provider_record_id=hit.get("provider_record_id"),
        )
        try:
            count_before = db.query(GlobalPerson.id).count()
            person = upsert_public_prospect(db, prospect)
            if person is not None:
                _search_backend_evidence(db, person.id, hit)
        except PrivateEvidenceError:
            suppressed += 1
            db.rollback()
            continue
        if db.query(GlobalPerson.id).count() > count_before and person is not None:
            new_ids.append(person.id)
    after = db.query(GlobalPerson.id).count()
    ingested = max(0, after - before)
    return {
        "ingested": ingested,
        "suppressed": suppressed,
        "duplicates": max(0, len(hits) - ingested - suppressed),
        "new_ids": new_ids,
    }
