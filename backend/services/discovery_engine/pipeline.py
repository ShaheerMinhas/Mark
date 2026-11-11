"""Stage runner for one discovery run.

Live public search and paid providers are hard-off. Stages still record what
they did, including a skip, so a run is not an empty success with invented people.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from backend.models import DiscoveryRun, DiscoveryStageAttempt, DiscoveryTarget, GlobalPerson
from backend.services.discovery_engine.backoff import RateLimited
from backend.services.discovery_engine.settings import LIVE_PUBLIC_SEARCH_ENABLED, ensure_settings
from backend.services.global_cache.normalize import country_code

STAGES = (
    "discover",
    "normalize",
    "dedup",
    "validate",
    "associate_company",
    "enrich",
    "email",
    "cache",
    "evidence",
    "analytics",
)

PAID_STAGES = {"enrich", "email"}


def _record(db: Session, run: DiscoveryRun, stage: str, status: str, detail: str, attempt: int) -> None:
    now = datetime.utcnow()
    db.add(
        DiscoveryStageAttempt(
            run_id=run.id,
            stage=stage,
            attempt=attempt,
            status=status,
            detail=detail[:2000],
            started_at=now,
            finished_at=now,
        )
    )
    run.current_stage = stage


def _cache_is_fresh(db: Session, target: DiscoveryTarget, now: datetime) -> bool:
    codes = [country_code(str(item)) for item in (target.countries or [])]
    codes = [code for code in codes if code]
    cutoff = now - timedelta(hours=max(1, int(target.refresh_hours or 168)))
    fresh = db.query(GlobalPerson.id).filter(GlobalPerson.updated_at >= cutoff)
    stale = db.query(GlobalPerson.id).filter(GlobalPerson.updated_at < cutoff)
    if codes:
        fresh = fresh.filter(GlobalPerson.country_code.in_(codes))
        stale = stale.filter(GlobalPerson.country_code.in_(codes))
    return fresh.first() is not None and stale.first() is None


def _public_hits(target: DiscoveryTarget) -> tuple[list, str]:
    """Kept so older callers still cannot reach the legacy search client."""
    del target
    if not LIVE_PUBLIC_SEARCH_ENABLED:
        return [], "Live public search is off. No request was sent."
    return [], "Live public search is off. No request was sent."


def execute_run(db: Session, run: DiscoveryRun, *, attempt: int = 1) -> None:
    target = db.get(DiscoveryTarget, run.target_id)
    if target is None:
        run.status = "failed"
        run.error = "Target missing"
        run.finished_at = datetime.utcnow()
        db.commit()
        return
    if run.cancel_requested or run.status == "cancelled":
        run.status = "cancelled"
        run.finished_at = datetime.utcnow()
        _record(db, run, "discover", "cancelled", "Run cancelled before work started", attempt)
        db.commit()
        return

    settings = ensure_settings(db)
    run.status = "running"
    run.started_at = run.started_at or datetime.utcnow()
    db.commit()

    now = datetime.utcnow()
    discover_detail = "Discovery is off. No provider was called."
    discover_status = "skipped"
    if not settings.enabled:
        discover_detail = "Discovery is off. No provider was called."
    elif not getattr(target, "autonomous", False) and _cache_is_fresh(db, target, now):
        discover_detail = "Matching cache rows are still fresh. No refresh was started."
        discover_status = "skipped_fresh"
    elif not getattr(target, "autonomous", False):
        discover_detail = "This target is not set to run on its own. No request was sent."
        discover_status = "skipped"
    else:
        from backend.services.discovery_engine.ingest import collect_public_hits, ingest_hits
        from backend.services.discovery_engine.sources import collect_extra_sources, linkedin_enabled

        hits = []
        notes = []
        public_note = ""
        if linkedin_enabled(db):
            hits, public_note = collect_public_hits(target, db)
            notes.append(public_note)
        else:
            notes.append("LinkedIn search is disabled. No LinkedIn request was sent.")
        extra_hits, extra_note = collect_extra_sources(db, target)
        hits.extend(extra_hits)
        notes.append(extra_note)
        discover_detail = " ".join(note for note in notes if note)
        if hits:
            from backend.services.discovery_engine.contacts import queue_contact_jobs

            stats = ingest_hits(db, hits)
            queued = queue_contact_jobs(db, list(stats.get("new_ids") or []), run.id)
            discover_status = "ingested"
            discover_detail = (
                f"{public_note} "
                f"Ingested {stats['ingested']} public people. "
                f"{stats['duplicates']} were already cached. {stats['suppressed']} were suppressed. "
                f"Queued {queued} contact enrichment jobs. {extra_note} Company research was not called."
            ).strip()
        else:
            discover_status = "blocked"

    for stage in STAGES:
        db.refresh(run)
        if run.cancel_requested:
            run.status = "cancelled"
            run.finished_at = datetime.utcnow()
            _record(db, run, stage, "cancelled", "Cancelled between stages", attempt)
            db.commit()
            return
        if stage in PAID_STAGES:
            from backend.services.discovery_engine.enrich import run_email_stage, run_enrich_stage

            if stage == "enrich":
                paid_status, paid_detail = run_enrich_stage(settings)
            else:
                paid_status, paid_detail = run_email_stage(db, run, target, settings, attempt)
            _record(db, run, stage, paid_status, paid_detail, attempt)
            continue
        if stage == "discover":
            _record(db, run, stage, discover_status, discover_detail, attempt)
            continue
        if stage == "validate":
            from backend.services.discovery_engine.targeting import apply_criteria, field_rules, preferred_as_required
            from backend.services.discovery_engine.suppression import suppressed_people_filter

            rules = field_rules(db)
            criteria = dict(target.criteria or {})

            def base():
                return suppressed_people_filter(db, db.query(GlobalPerson).filter(GlobalPerson.archived_at.is_(None)))

            eligible = apply_criteria(db, base(), criteria, rules).count()
            preferred = apply_criteria(db, base(), criteria, preferred_as_required(rules)).count()
            total = db.query(GlobalPerson).filter(GlobalPerson.archived_at.is_(None)).count()
            _record(
                db,
                run,
                stage,
                "classified",
                (
                    f"{eligible} cached people match required fields. {preferred} also meet preferred fields. "
                    f"{total - eligible} do not match. Non-matching rows were not deleted. "
                    "Enrichment sources are not applied."
                ),
                attempt,
            )
            continue
        _record(db, run, stage, "skipped", "No new public hits to process. Enrichment sources are not applied.", attempt)

    run.status = "succeeded"
    run.error = None
    run.finished_at = datetime.utcnow()
    db.commit()


def execute_queued_job(db: Session, payload: dict, *, attempt: int) -> None:
    run = db.get(DiscoveryRun, int(payload.get("run_id") or 0))
    if run is None:
        return
    if run.status == "cancelled" or run.cancel_requested:
        run.status = "cancelled"
        run.finished_at = datetime.utcnow()
        db.commit()
        return
    try:
        execute_run(db, run, attempt=attempt)
    except RateLimited:
        raise
