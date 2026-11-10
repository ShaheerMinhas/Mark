"""How many distinct customers may receive one shared identity.

A customer is the organization on the search when there is one. Otherwise it is
the individual account. Teammates in that organization are the same customer.
A repeated search by that customer does not add a recipient.

Saturation stops future distribution. It does not delete leads, notes, or CRM
rows a customer already has. It is not an email-verification or legal-suppression
state. Recipient identities and counts are not returned to customers.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.models import DistributionPolicy, DistributionSlot, LeadAllocation, LeadRecord, SearchJob
from backend.services.identity import normalize_linkedin_url


def customer_key_for(job: SearchJob) -> str:
    if job.organization_id:
        return f"org:{int(job.organization_id)}"
    return f"user:{int(job.owner_id)}"


def canonical_key_for(*, profile_url: str = "", name: str = "", domain: str = "") -> str | None:
    profile = normalize_linkedin_url(profile_url or "")
    if profile:
        return f"profile:{profile}"
    folded_name = " ".join((name or "").lower().split())
    folded_domain = (domain or "").strip().lower()
    if folded_name and folded_domain and " " in folded_name:
        return f"name_domain:{folded_name}|{folded_domain}"
    return None


def ensure_policy(db: Session) -> DistributionPolicy:
    row = db.get(DistributionPolicy, 1)
    if row is None:
        row = DistributionPolicy(id=1, max_customers=3)
        db.add(row)
        db.flush()
    return row


def rebind_canonical(db: Session, old_key: str, new_key: str) -> None:
    """Move recipients onto the surviving identity. A new profile must not start the count over."""
    if not old_key or not new_key or old_key == new_key:
        return
    for row in db.query(LeadAllocation).filter(LeadAllocation.canonical_key == old_key).all():
        taken = (
            db.query(LeadAllocation)
            .filter(LeadAllocation.canonical_key == new_key, LeadAllocation.customer_key == row.customer_key)
            .one_or_none()
        )
        if taken is None:
            row.canonical_key = new_key
        else:
            db.delete(row)
    db.flush()
    old_slot = db.get(DistributionSlot, old_key)
    count = (
        db.query(LeadAllocation.customer_key)
        .filter(LeadAllocation.canonical_key == new_key)
        .distinct()
        .count()
    )
    limit = int(ensure_policy(db).max_customers or 3)
    new_slot = db.get(DistributionSlot, new_key)
    saturated_at = None
    if old_slot is not None and old_slot.saturated_at is not None:
        saturated_at = old_slot.saturated_at
    if new_slot is not None and new_slot.saturated_at is not None:
        saturated_at = new_slot.saturated_at
    if count >= limit and saturated_at is None:
        saturated_at = datetime.utcnow()
    if new_slot is None and count:
        db.add(
            DistributionSlot(
                canonical_key=new_key,
                recipient_count=int(count),
                saturated_at=saturated_at if count >= limit else None,
            )
        )
    elif new_slot is not None:
        new_slot.recipient_count = int(count)
        new_slot.saturated_at = saturated_at if count >= limit else new_slot.saturated_at
    if old_slot is not None:
        db.delete(old_slot)
    db.flush()


def set_max_customers(db: Session, value: int, *, confirm_increase: bool = False) -> DistributionPolicy:
    if value not in {2, 3}:
        raise ValueError("max_customers must be 2 or 3")
    row = ensure_policy(db)
    if value > int(row.max_customers or 3) and not confirm_increase:
        raise ValueError("Increasing the limit requires confirm_increase")
    row.max_customers = value
    row.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(row)
    return row


class _Saturated(Exception):
    pass


def _slot(db: Session, key: str) -> DistributionSlot:
    row = db.get(DistributionSlot, key)
    if row is not None:
        return row
    row = DistributionSlot(canonical_key=key, recipient_count=0)
    try:
        with db.begin_nested():
            db.add(row)
            db.flush()
    except IntegrityError:
        row = db.get(DistributionSlot, key)
    return row


def try_allocate(
    db: Session,
    *,
    canonical_key: str,
    customer_key: str,
    person_id: int | None,
    search_job_id: int | None,
    lead_record_id: int | None,
) -> str:
    """Return allocated, already, or saturated. Does not commit the caller's transaction."""
    if not canonical_key or not customer_key:
        return "saturated"
    existing = (
        db.query(LeadAllocation)
        .filter(LeadAllocation.canonical_key == canonical_key, LeadAllocation.customer_key == customer_key)
        .one_or_none()
    )
    if existing is not None:
        return "already"
    policy = ensure_policy(db)
    limit = int(policy.max_customers or 3)
    _slot(db, canonical_key)
    try:
        with db.begin_nested():
            db.add(
                LeadAllocation(
                    canonical_key=canonical_key,
                    customer_key=customer_key,
                    person_id=person_id,
                    search_job_id=search_job_id,
                    lead_record_id=lead_record_id,
                )
            )
            db.flush()
            updated = db.execute(
                text(
                    """
                    UPDATE distribution_slots
                       SET recipient_count = recipient_count + 1,
                           saturated_at = CASE
                               WHEN recipient_count + 1 >= :limit THEN :now
                               ELSE saturated_at
                           END
                     WHERE canonical_key = :key
                       AND recipient_count < :limit
                    """
                ),
                {"limit": limit, "now": datetime.utcnow(), "key": canonical_key},
            )
            if updated.rowcount != 1:
                raise _Saturated()
    except _Saturated:
        return "saturated"
    except IntegrityError:
        existing = (
            db.query(LeadAllocation)
            .filter(LeadAllocation.canonical_key == canonical_key, LeadAllocation.customer_key == customer_key)
            .one_or_none()
        )
        return "already" if existing is not None else "saturated"
    return "allocated"


def eligible_clause(customer_key: str):
    """SQL predicate: new customers do not receive a saturated identity. The same customer still can."""
    from sqlalchemy import or_

    policy_limit = 3
    return or_(
        DistributionSlot.canonical_key.is_(None),
        DistributionSlot.recipient_count < policy_limit,
        LeadAllocation.customer_key == customer_key,
    )


def distribution_summary(db: Session) -> dict:
    policy = ensure_policy(db)
    limit = int(policy.max_customers or 3)
    active = db.query(DistributionSlot).filter(DistributionSlot.recipient_count < limit).count()
    saturated = db.query(DistributionSlot).filter(DistributionSlot.recipient_count >= limit).count()
    replacements = db.query(LeadRecord).filter(LeadRecord.replaced_at.isnot(None)).count()
    return {
        "max_customers": limit,
        "active_identities": int(active),
        "saturated_identities": int(saturated),
        "replacements": int(replacements),
        "detail": "Saturation stops new customers. It does not delete a lead a customer already has.",
    }


def backfill_from_existing_leads(db: Session) -> int:
    """Record customers who already hold a copy. Do not invent recipients and do not delete copies.

    A legacy table may not have every current column yet. Only columns that exist are read.
    """
    from sqlalchemy import inspect

    from backend.models import GlobalPerson

    columns = {column["name"] for column in inspect(db.get_bind()).get_columns("lead_records")}
    if "search_job_id" not in columns or "id" not in columns:
        return 0
    selected = ["lead_records.id", "lead_records.search_job_id"]
    for name in ("name", "email", "linkedin_url", "company_domain"):
        if name in columns:
            selected.append(f"lead_records.{name}")
    raw_rows = db.execute(text(f"SELECT {', '.join(selected)} FROM lead_records")).mappings().all()
    job_columns = {column["name"] for column in inspect(db.get_bind()).get_columns("search_jobs")}
    if "owner_id" not in job_columns:
        return 0
    job_selected = ["id", "owner_id"]
    if "organization_id" in job_columns:
        job_selected.append("organization_id")
    job_rows = {
        row["id"]: row
        for row in db.execute(text(f"SELECT {', '.join(job_selected)} FROM search_jobs")).mappings().all()
    }
    rows = []
    for raw in raw_rows:
        job_raw = job_rows.get(raw["search_job_id"])
        if job_raw is None:
            continue
        job = type("JobView", (), {})()
        job.id = job_raw["id"]
        job.owner_id = job_raw["owner_id"]
        job.organization_id = job_raw.get("organization_id")
        lead = type("LeadView", (), {})()
        lead.id = raw["id"]
        lead.name = raw.get("name") or ""
        lead.email = raw.get("email")
        lead.linkedin_url = raw.get("linkedin_url")
        lead.company_domain = raw.get("company_domain")
        rows.append((lead, job))
    noted = 0
    for lead, job in rows:
        profile = lead.linkedin_url or ""
        key = canonical_key_for(profile_url=profile, name=lead.name or "", domain=lead.company_domain or "")
        if not key:
            continue
        person = None
        if profile:
            person = (
                db.query(GlobalPerson)
                .filter(GlobalPerson.profile_url_normalized == normalize_linkedin_url(profile))
                .one_or_none()
            )
        customer = customer_key_for(job)
        exists = (
            db.query(LeadAllocation)
            .filter(LeadAllocation.canonical_key == key, LeadAllocation.customer_key == customer)
            .one_or_none()
        )
        if exists is None:
            db.add(
                LeadAllocation(
                    canonical_key=key,
                    customer_key=customer,
                    person_id=person.id if person else None,
                    search_job_id=job.id,
                    lead_record_id=lead.id,
                )
            )
            noted += 1
        _slot(db, key)
    db.flush()
    for slot in db.query(DistributionSlot).all():
        count = (
            db.query(LeadAllocation.customer_key)
            .filter(LeadAllocation.canonical_key == slot.canonical_key)
            .distinct()
            .count()
        )
        slot.recipient_count = int(count)
        limit = int(ensure_policy(db).max_customers or 3)
        if count >= limit and slot.saturated_at is None:
            slot.saturated_at = datetime.utcnow()
    db.commit()
    return noted
