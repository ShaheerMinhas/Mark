"""Read models for the operator console. Counts come from the database."""

from __future__ import annotations

import csv
import io
from datetime import datetime, timedelta

from sqlalchemy import func
from sqlalchemy.orm import Session

from backend.models import (
    DiscoveryRun,
    DiscoveryStageAttempt,
    DiscoveryTarget,
    GlobalCompany,
    GlobalEvidence,
    GlobalIdentityKey,
    GlobalPerson,
    GlobalPersonTechnology,
    JobType,
    ProviderUsage,
    QueuedJob,
    QueuedJobStatus,
    SchedulerLock,
)
from backend.services.discovery_engine.schedule import SCHEDULER_LOCK
from backend.services.discovery_engine.settings import ensure_settings

PEOPLE_SORTS = {
    "updated": GlobalPerson.updated_at.desc(),
    "name": GlobalPerson.full_name.asc(),
    "country": GlobalPerson.country_code.asc(),
    "tier": GlobalPerson.quality_tier.asc(),
    "company": GlobalPerson.company_name.asc(),
}
RUN_SORTS = {
    "newest": DiscoveryRun.id.desc(),
    "scheduled": DiscoveryRun.scheduled_for.desc(),
    "status": DiscoveryRun.status.asc(),
}


def _page(page: int, page_size: int) -> tuple[int, int]:
    page = max(1, int(page or 1))
    size = max(1, min(100, int(page_size or 25)))
    return page, size


def worker_status(db: Session) -> dict:
    """Persisted planner lease only. This is not 'the API is up'."""
    row = db.get(SchedulerLock, SCHEDULER_LOCK)
    if row is None or row.lease_expires_at is None:
        return {
            "status": "unknown",
            "detail": "No discovery worker has written a planner lease.",
            "lease_expires_at": None,
        }
    if row.lease_expires_at > datetime.utcnow():
        return {
            "status": "lease_active",
            "detail": "A discovery worker holds the planner lease.",
            "lease_expires_at": row.lease_expires_at,
        }
    return {
        "status": "lease_expired",
        "detail": "The last planner lease has expired. The worker may be stopped.",
        "lease_expires_at": row.lease_expires_at,
    }


def overview(db: Session) -> dict:
    settings = ensure_settings(db)
    people = db.query(func.count(GlobalPerson.id)).scalar() or 0
    archived = db.query(func.count(GlobalPerson.id)).filter(GlobalPerson.archived_at.isnot(None)).scalar() or 0
    companies = db.query(func.count(GlobalCompany.id)).scalar() or 0
    by_tier = dict(
        db.query(GlobalPerson.quality_tier, func.count(GlobalPerson.id))
        .filter(GlobalPerson.archived_at.is_(None))
        .group_by(GlobalPerson.quality_tier)
        .all()
    )
    by_country = [
        {"country_code": code, "count": int(count)}
        for code, count in db.query(GlobalPerson.country_code, func.count(GlobalPerson.id))
        .filter(GlobalPerson.archived_at.is_(None))
        .group_by(GlobalPerson.country_code)
        .all()
    ]
    run_counts = dict(db.query(DiscoveryRun.status, func.count(DiscoveryRun.id)).group_by(DiscoveryRun.status).all())
    target_counts = dict(
        db.query(DiscoveryTarget.scheduling_status, func.count(DiscoveryTarget.id))
        .group_by(DiscoveryTarget.scheduling_status)
        .all()
    )
    since = datetime.utcnow() - timedelta(days=1)
    provider_calls = db.query(func.count(ProviderUsage.id)).filter(ProviderUsage.created_at >= since).scalar() or 0
    queued_jobs = (
        db.query(func.count(QueuedJob.id))
        .filter(
            QueuedJob.job_type == JobType.DISCOVERY_PIPELINE.value,
            QueuedJob.status == QueuedJobStatus.QUEUED.value,
        )
        .scalar()
        or 0
    )
    return {
        "engine": {
            "scheduling_enabled": bool(settings.enabled),
            "live_public_search": "disabled",
            "paid_providers": "disabled",
            "detail": "Live search and paid providers are switched off in this build. A run records skipped stages.",
        },
        "worker": worker_status(db),
        "scheduler": {
            "active_targets": int(target_counts.get("active") or 0),
            "paused_targets": int(target_counts.get("paused") or 0),
            "stopped_targets": int(target_counts.get("stopped") or 0),
        },
        "counts": {
            "people": int(people),
            "archived_people": int(archived),
            "companies": int(companies),
            "runs_queued": int(run_counts.get("queued") or 0),
            "runs_running": int(run_counts.get("running") or 0),
            "runs_succeeded": int(run_counts.get("succeeded") or 0),
            "runs_failed": int(run_counts.get("failed") or 0),
            "runs_cancelled": int(run_counts.get("cancelled") or 0),
            "pipeline_jobs_queued": int(queued_jobs),
        },
        "groupings": {"quality_tier": {str(k or ""): int(v) for k, v in by_tier.items()}, "country": by_country},
        "usage": {
            "provider_calls_24h": int(provider_calls),
            "estimated_cost": None,
            "estimated_cost_status": "unavailable",
        },
        "limits": {
            "max_concurrency": int(settings.max_concurrency or 1),
            "max_runs_per_tick": int(settings.max_runs_per_tick or 1),
            "max_runs_per_hour": int(settings.max_runs_per_hour or 4),
            "max_runs_per_day": int(settings.max_runs_per_day or 24),
        },
    }


def list_people(db: Session, *, q: str, country: str, tier: str, include_archived: bool, sort: str, page: int, page_size: int):
    page, size = _page(page, page_size)
    query = db.query(GlobalPerson)
    if not include_archived:
        query = query.filter(GlobalPerson.archived_at.is_(None))
    if country:
        query = query.filter(GlobalPerson.country_code == country.upper())
    if tier:
        query = query.filter(GlobalPerson.quality_tier == tier)
    if q.strip():
        like = f"%{q.strip().lower()}%"
        query = query.filter(
            func.lower(func.coalesce(GlobalPerson.full_name, "")).like(like)
            | func.lower(func.coalesce(GlobalPerson.company_name, "")).like(like)
            | func.lower(func.coalesce(GlobalPerson.email, "")).like(like)
        )
    total = query.count()
    rows = query.order_by(PEOPLE_SORTS.get(sort, PEOPLE_SORTS["updated"])).offset((page - 1) * size).limit(size).all()
    return total, rows


def person_detail(db: Session, person_id: int) -> dict | None:
    row = db.get(GlobalPerson, person_id)
    if row is None:
        return None
    techs = [
        item.technology
        for item in db.query(GlobalPersonTechnology).filter(GlobalPersonTechnology.person_id == row.id).all()
    ]
    evidence = (
        db.query(GlobalEvidence)
        .filter(GlobalEvidence.subject_type == "person", GlobalEvidence.subject_id == row.id)
        .order_by(GlobalEvidence.id.desc())
        .all()
    )
    keys = (
        db.query(GlobalIdentityKey)
        .filter(GlobalIdentityKey.subject_type == "person", GlobalIdentityKey.subject_id == row.id)
        .all()
    )
    company = db.get(GlobalCompany, row.company_id) if row.company_id else None
    return {
        "person": _person_dict(row),
        "technologies": techs,
        "company": _company_dict(company) if company else None,
        "evidence": [
            {
                "field_name": item.field_name,
                "value_text": item.value_text,
                "source_url": item.source_url,
                "provider": item.provider,
                "eligible": bool(item.eligible),
                "share_scope": item.share_scope,
                "observed_at": item.observed_at,
            }
            for item in evidence
        ],
        "identity_keys": [{"kind": item.kind, "key_value": item.key_value} for item in keys],
        "research": "unavailable",
    }


def list_companies(db: Session, *, q: str, country: str, include_archived: bool, page: int, page_size: int):
    page, size = _page(page, page_size)
    query = db.query(GlobalCompany)
    if not include_archived:
        query = query.filter(GlobalCompany.archived_at.is_(None))
    if country:
        query = query.filter(GlobalCompany.country_code == country.upper())
    if q.strip():
        like = f"%{q.strip().lower()}%"
        query = query.filter(
            func.lower(func.coalesce(GlobalCompany.name, "")).like(like)
            | func.lower(func.coalesce(GlobalCompany.domain, "")).like(like)
        )
    total = query.count()
    rows = query.order_by(GlobalCompany.updated_at.desc()).offset((page - 1) * size).limit(size).all()
    return total, rows


def list_runs(db: Session, *, status: str, page: int, page_size: int, sort: str):
    page, size = _page(page, page_size)
    query = db.query(DiscoveryRun)
    if status:
        query = query.filter(DiscoveryRun.status == status)
    total = query.count()
    rows = query.order_by(RUN_SORTS.get(sort, RUN_SORTS["newest"])).offset((page - 1) * size).limit(size).all()
    return total, rows


def run_detail(db: Session, run_id: int) -> dict | None:
    run = db.get(DiscoveryRun, run_id)
    if run is None:
        return None
    target = db.get(DiscoveryTarget, run.target_id)
    stages = (
        db.query(DiscoveryStageAttempt)
        .filter(DiscoveryStageAttempt.run_id == run.id)
        .order_by(DiscoveryStageAttempt.id.asc())
        .all()
    )
    queued = db.get(QueuedJob, run.queue_job_id) if run.queue_job_id else None
    return {
        "run": _run_dict(run, target.name if target else ""),
        "stages": [
            {
                "stage": item.stage,
                "attempt": item.attempt,
                "status": item.status,
                "detail": item.detail,
                "started_at": item.started_at,
                "finished_at": item.finished_at,
            }
            for item in stages
        ],
        "queue_job": None
        if queued is None
        else {"id": queued.id, "status": queued.status, "attempts": queued.attempts, "last_error": queued.last_error},
    }


def _person_dict(row: GlobalPerson) -> dict:
    return {
        "id": row.id,
        "full_name": row.full_name,
        "title": row.title,
        "company_name": row.company_name,
        "domain": row.domain,
        "country_code": row.country_code,
        "city": row.city,
        "industry": row.industry,
        "email": row.email,
        "email_status": row.email_status,
        "phone": row.phone,
        "quality_tier": row.quality_tier,
        "profile_url": row.profile_url,
        "archived_at": row.archived_at,
        "correction_note": row.correction_note,
        "updated_at": row.updated_at,
    }


def _company_dict(row: GlobalCompany) -> dict:
    return {
        "id": row.id,
        "name": row.name,
        "domain": row.domain,
        "industry": row.industry,
        "country_code": row.country_code,
        "employee_count": row.employee_count,
        "size_band": row.size_band,
        "archived_at": row.archived_at,
        "correction_note": row.correction_note,
        "updated_at": row.updated_at,
    }


def _run_dict(run: DiscoveryRun, target_name: str) -> dict:
    return {
        "id": run.id,
        "target_id": run.target_id,
        "target_name": target_name,
        "status": run.status,
        "cancel_requested": bool(run.cancel_requested),
        "current_stage": run.current_stage,
        "people_limit": run.people_limit,
        "query_limit": run.query_limit,
        "scheduled_for": run.scheduled_for,
        "error": run.error,
        "created_at": run.created_at,
        "finished_at": run.finished_at,
    }


CORRECTION_FIELDS = {"title", "company_name", "city", "industry", "country_code"}


def archive_person(db: Session, person_id: int) -> GlobalPerson | None:
    row = db.get(GlobalPerson, person_id)
    if row is None:
        return None
    if row.archived_at is None:
        row.archived_at = datetime.utcnow()
        db.add(
            GlobalEvidence(
                subject_type="person",
                subject_id=row.id,
                field_name="archived",
                value_text="archived",
                share_scope="public",
                eligible=True,
                observed_at=datetime.utcnow(),
            )
        )
    db.commit()
    db.refresh(row)
    return row


def correct_person(db: Session, person_id: int, fields: dict, note: str | None) -> GlobalPerson | None:
    row = db.get(GlobalPerson, person_id)
    if row is None:
        return None
    for key, value in fields.items():
        if key not in CORRECTION_FIELDS:
            raise ValueError(f"Cannot correct {key}")
        text = (value or "").strip()
        if not text:
            continue
        previous = getattr(row, key)
        if previous and previous != text:
            db.add(
                GlobalEvidence(
                    subject_type="person",
                    subject_id=row.id,
                    field_name=key,
                    value_text=str(previous),
                    share_scope="public",
                    eligible=True,
                    provider="operator_correction",
                    observed_at=datetime.utcnow(),
                )
            )
        setattr(row, key, text)
    if note:
        row.correction_note = note.strip()[:2000]
    row.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(row)
    return row


def archive_company(db: Session, company_id: int) -> GlobalCompany | None:
    row = db.get(GlobalCompany, company_id)
    if row is None:
        return None
    if row.archived_at is None:
        row.archived_at = datetime.utcnow()
    db.commit()
    db.refresh(row)
    return row


def csv_safe(value) -> str:
    text = "" if value is None else str(value)
    if text[:1] in {"=", "+", "-", "@"}:
        return "'" + text
    return text


def people_csv(rows: list[GlobalPerson]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["id", "name", "title", "company", "email", "email_status", "country", "tier"])
    for row in rows:
        writer.writerow(
            [
                row.id,
                csv_safe(row.full_name),
                csv_safe(row.title),
                csv_safe(row.company_name),
                csv_safe(row.email),
                csv_safe(row.email_status),
                csv_safe(row.country_code),
                csv_safe(row.quality_tier),
            ]
        )
    return buffer.getvalue()
