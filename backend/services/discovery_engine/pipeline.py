"""Stage runner for one discovery run.

Live public search and paid providers are hard-off. Stages still record what
they did, including a skip, so a run is not an empty success with invented people.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from backend.models import DiscoveryRun, DiscoveryStageAttempt, DiscoveryTarget, GlobalPerson
from backend.services.discovery_engine.backoff import RateLimited
from backend.services.discovery_engine.settings import (
    LIVE_PUBLIC_SEARCH_ENABLED,
    PAID_PROVIDERS_ENABLED,
    ensure_settings,
)
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
    """The only place a live search could be called. The switch is hard-off."""
    if not LIVE_PUBLIC_SEARCH_ENABLED:
        return [], "Live public search is off until a budget ledger exists"
    from backend.services.discovery import runner as discovery_runner

    # This import is the audited public search entry. It does not run while the
    # constant above is false, which is the phase 2 default.
    _ = discovery_runner.search_text
    _ = target
    return [], "Live public search is off"


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
    elif _cache_is_fresh(db, target, now):
        discover_detail = "Matching cache rows are still fresh. No refresh was started."
        discover_status = "skipped_fresh"
    else:
        hits, discover_detail = _public_hits(target)
        if hits:
            raise RuntimeError("Phase 2 refuses to ingest live hits while search is hard-off")
        discover_status = "skipped"

    for stage in STAGES:
        db.refresh(run)
        if run.cancel_requested:
            run.status = "cancelled"
            run.finished_at = datetime.utcnow()
            _record(db, run, stage, "cancelled", "Cancelled between stages", attempt)
            db.commit()
            return
        if stage in PAID_STAGES or not PAID_PROVIDERS_ENABLED:
            if stage in PAID_STAGES:
                _record(
                    db,
                    run,
                    stage,
                    "skipped",
                    "Paid providers are disabled. No credential or budget ledger is consulted because spend cannot be reserved.",
                    attempt,
                )
                continue
        if stage == "discover":
            _record(db, run, stage, discover_status, discover_detail, attempt)
            continue
        _record(db, run, stage, "skipped", "No new public hits to process", attempt)

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
