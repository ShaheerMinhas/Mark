"""Integer budget reservations for global jobs.

A call is refused until a platform credential, an approved maximum price, a rate
cap, and global plus provider daily and monthly room all exist. Customer
environment keys are not read. Zero is not treated as a price. An unknown final
charge keeps the reserved maximum. It is not refunded and it is not stored as zero.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

from sqlalchemy import func, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.models import (
    DiscoveryBudget,
    DiscoveryBudgetPeriod,
    DiscoveryProvider,
    DiscoveryProviderPrice,
    DiscoveryProviderSecret,
    DiscoveryReservation,
    GlobalCompany,
    GlobalEvidence,
    GlobalFieldFreshness,
    GlobalPerson,
    ProviderUsage,
)
from backend.services.crypto import encrypt_value

PROVIDERS = ("hunter", "tomba", "leadmagic", "prospeo", "findymail")
EMAIL_OPERATIONS = ("email_find", "email_verify")
COMPANY_OPERATION = "company_research"


class BudgetDenied(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def seed_providers(db: Session) -> None:
    """Known adapters, all disabled, with no price and no credential."""
    for index, name in enumerate(PROVIDERS):
        row = db.get(DiscoveryProvider, name)
        if row is None:
            db.add(
                DiscoveryProvider(
                    name=name,
                    enabled=False,
                    priority=(index + 1) * 10,
                    rate_per_hour=0,
                    supports_email_find=True,
                    supports_email_verify=True,
                    supports_company=False,
                )
            )
    db.flush()


def _currency(value: str) -> str:
    code = (value or "").strip().upper()
    if len(code) != 3 or not code.isalpha():
        raise ValueError("currency must be a 3-letter code")
    return code


def set_price(db: Session, *, provider_name: str, operation: str, currency: str, max_cost_minor: int) -> DiscoveryProviderPrice:
    if provider_name not in PROVIDERS:
        raise ValueError("Unknown provider")
    if operation not in EMAIL_OPERATIONS:
        raise ValueError("That operation has no adapter")
    if int(max_cost_minor) < 1:
        raise ValueError("max_cost_minor must be at least 1. Unknown or free pricing is not accepted")
    code = _currency(currency)
    row = (
        db.query(DiscoveryProviderPrice)
        .filter(DiscoveryProviderPrice.provider_name == provider_name, DiscoveryProviderPrice.operation == operation)
        .one_or_none()
    )
    if row is None:
        row = DiscoveryProviderPrice(provider_name=provider_name, operation=operation, currency=code, max_cost_minor=int(max_cost_minor))
        db.add(row)
    else:
        row.currency = code
        row.max_cost_minor = int(max_cost_minor)
        row.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(row)
    return row


def set_credential(db: Session, *, provider_name: str, api_key: str, secret: str = "") -> None:
    if provider_name not in PROVIDERS:
        raise ValueError("Unknown provider")
    if not api_key.strip():
        raise ValueError("api_key is empty")
    payload = json.dumps({"api_key": api_key, "secret": secret})
    row = db.get(DiscoveryProviderSecret, provider_name)
    if row is None:
        db.add(DiscoveryProviderSecret(provider_name=provider_name, ciphertext=encrypt_value(payload)))
    else:
        row.ciphertext = encrypt_value(payload)
        row.updated_at = datetime.utcnow()
    db.commit()


def set_budget(db: Session, *, scope: str, provider_name: str, period: str, currency: str, limit_minor: int) -> DiscoveryBudget:
    if scope not in {"global", "provider"}:
        raise ValueError("scope must be global or provider")
    if period not in {"day", "month"}:
        raise ValueError("period must be day or month")
    if scope == "provider" and provider_name not in PROVIDERS:
        raise ValueError("Unknown provider")
    if scope == "global":
        provider_name = ""
    if int(limit_minor) < 1:
        raise ValueError("limit_minor must be at least 1")
    code = _currency(currency)
    row = (
        db.query(DiscoveryBudget)
        .filter(
            DiscoveryBudget.scope == scope,
            DiscoveryBudget.provider_name == provider_name,
            DiscoveryBudget.period == period,
            DiscoveryBudget.currency == code,
        )
        .one_or_none()
    )
    if row is None:
        row = DiscoveryBudget(
            scope=scope,
            provider_name=provider_name,
            period=period,
            currency=code,
            limit_minor=int(limit_minor),
        )
        db.add(row)
    else:
        row.limit_minor = int(limit_minor)
        row.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(row)
    return row


def _period_start(period: str, now: datetime) -> str:
    if period == "day":
        return now.date().isoformat()
    return now.date().replace(day=1).isoformat()


def _period_row(db: Session, budget: DiscoveryBudget, now: datetime) -> DiscoveryBudgetPeriod:
    start = _period_start(budget.period, now)
    row = (
        db.query(DiscoveryBudgetPeriod)
        .filter(DiscoveryBudgetPeriod.budget_id == budget.id, DiscoveryBudgetPeriod.period_start == start)
        .one_or_none()
    )
    if row is not None:
        return row
    row = DiscoveryBudgetPeriod(budget_id=budget.id, period_start=start, reserved_minor=0, settled_minor=0, unknown_minor=0)
    db.add(row)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        row = (
            db.query(DiscoveryBudgetPeriod)
            .filter(DiscoveryBudgetPeriod.budget_id == budget.id, DiscoveryBudgetPeriod.period_start == start)
            .one()
        )
    return row


def _required_budgets(db: Session, provider_name: str, currency: str) -> list[DiscoveryBudget]:
    wanted = [
        ("global", "", "day"),
        ("global", "", "month"),
        ("provider", provider_name, "day"),
        ("provider", provider_name, "month"),
    ]
    found = []
    for scope, name, period in wanted:
        row = (
            db.query(DiscoveryBudget)
            .filter(
                DiscoveryBudget.scope == scope,
                DiscoveryBudget.provider_name == name,
                DiscoveryBudget.period == period,
                DiscoveryBudget.currency == currency,
            )
            .one_or_none()
        )
        if row is None:
            raise BudgetDenied(f"missing {scope} {period} budget")
        found.append(row)
    return found


def gate_reason(db: Session, provider: DiscoveryProvider, operation: str, *, now: datetime | None = None) -> str | None:
    """Why this provider must not be called. None means the checks before reservation passed."""
    now = now or datetime.utcnow()
    if operation == COMPANY_OPERATION or not _supports(provider, operation):
        return "unsupported"
    if not provider.enabled:
        return "disabled"
    if provider.rate_per_hour < 1:
        return "rate cap is not configured"
    secret = db.get(DiscoveryProviderSecret, provider.name)
    if secret is None:
        return "credential missing"
    try:
        from backend.services.crypto import decrypt_value

        decrypt_value(secret.ciphertext)
    except Exception:
        return "credential unreadable"
    price = (
        db.query(DiscoveryProviderPrice)
        .filter(DiscoveryProviderPrice.provider_name == provider.name, DiscoveryProviderPrice.operation == operation)
        .one_or_none()
    )
    if price is None or price.max_cost_minor < 1:
        return "price missing"
    since = now - timedelta(hours=1)
    used = (
        db.query(func.count(DiscoveryReservation.id))
        .filter(DiscoveryReservation.provider_name == provider.name, DiscoveryReservation.created_at >= since)
        .scalar()
        or 0
    )
    if int(used) >= provider.rate_per_hour:
        return "rate cap"
    return None


def _supports(provider: DiscoveryProvider, operation: str) -> bool:
    if operation == "email_find":
        return bool(provider.supports_email_find)
    if operation == "email_verify":
        return bool(provider.supports_email_verify)
    return False


def price_for(db: Session, provider_name: str, operation: str) -> DiscoveryProviderPrice:
    row = (
        db.query(DiscoveryProviderPrice)
        .filter(DiscoveryProviderPrice.provider_name == provider_name, DiscoveryProviderPrice.operation == operation)
        .one()
    )
    return row


def reserve_max(
    db: Session,
    *,
    idempotency_key: str,
    provider_name: str,
    operation: str,
    run_id: int | None,
    person_id: int | None,
    attempt: int,
    now: datetime | None = None,
) -> DiscoveryReservation:
    now = now or datetime.utcnow()
    existing = (
        db.query(DiscoveryReservation).filter(DiscoveryReservation.idempotency_key == idempotency_key).one_or_none()
    )
    if existing is not None:
        return existing
    price = price_for(db, provider_name, operation)
    budgets = _required_budgets(db, provider_name, price.currency)
    periods = [_period_row(db, budget, now) for budget in budgets]
    amount = int(price.max_cost_minor)
    for period, budget in zip(periods, budgets):
        updated = db.execute(
            text(
                """
                UPDATE discovery_budget_periods
                SET reserved_minor = reserved_minor + :amount
                WHERE id = :id
                  AND reserved_minor + settled_minor + unknown_minor + :amount <= :limit
                """
            ),
            {"amount": amount, "id": period.id, "limit": int(budget.limit_minor)},
        )
        if updated.rowcount != 1:
            db.rollback()
            raise BudgetDenied("budget exceeded")
    row = DiscoveryReservation(
        idempotency_key=idempotency_key,
        provider_name=provider_name,
        operation=operation,
        currency=price.currency,
        amount_minor=amount,
        status="reserved",
        period_ids=[period.id for period in periods],
        run_id=run_id,
        person_id=person_id,
        attempt=attempt,
        created_at=now,
    )
    db.add(row)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return db.query(DiscoveryReservation).filter(DiscoveryReservation.idempotency_key == idempotency_key).one()
    db.refresh(row)
    return row


def reconcile(
    db: Session,
    reservation_id: int,
    *,
    charge_known: bool,
    actual_minor: int | None,
    outcome: str,
    http_status: int | None,
    success: bool,
    credits: int | None,
) -> DiscoveryReservation:
    """Settle once. A second call does not move the counters again."""
    row = db.get(DiscoveryReservation, reservation_id)
    if row is None:
        raise ValueError("reservation missing")
    if row.status != "reserved":
        return row
    amount = int(row.amount_minor)
    known = bool(charge_known) and actual_minor is not None and 0 <= int(actual_minor) <= amount
    if known:
        status = "settled"
        actual = int(actual_minor)
        settled_delta = actual
        unknown_delta = 0
    else:
        status = "unknown_charge"
        actual = None
        settled_delta = 0
        unknown_delta = amount
    for period_id in list(row.period_ids or []):
        updated = db.execute(
            text(
                """
                UPDATE discovery_budget_periods
                SET reserved_minor = reserved_minor - :amount,
                    settled_minor = settled_minor + :settled,
                    unknown_minor = unknown_minor + :unknown
                WHERE id = :id AND reserved_minor >= :amount
                """
            ),
            {"amount": amount, "settled": settled_delta, "unknown": unknown_delta, "id": period_id},
        )
        if updated.rowcount != 1:
            db.rollback()
            raise RuntimeError("reservation counters could not be reconciled")
    claimed = db.execute(
        text(
            """
            UPDATE discovery_reservations
            SET status = :status, actual_minor = :actual, outcome = :outcome,
                http_status = :http_status, settled_at = :settled_at
            WHERE id = :id AND status = 'reserved'
            """
        ),
        {
            "status": status,
            "actual": actual,
            "outcome": outcome,
            "http_status": http_status,
            "settled_at": datetime.utcnow(),
            "id": row.id,
        },
    )
    if claimed.rowcount != 1:
        db.rollback()
        return db.get(DiscoveryReservation, reservation_id)
    db.add(
        ProviderUsage(
            provider=row.provider_name,
            operation=row.operation,
            success=bool(success),
            credits=credits,
            http_status=http_status,
            detail=outcome[:200],
            scope="global",
            reservation_id=row.id,
        )
    )
    db.commit()
    db.expire_all()
    return db.get(DiscoveryReservation, reservation_id)


def recover_stuck(db: Session, *, older_than_seconds: int = 0) -> int:
    """A reserved row whose caller never reconciled keeps its maximum. Nothing is refunded."""
    cutoff = datetime.utcnow() - timedelta(seconds=max(0, older_than_seconds))
    rows = (
        db.query(DiscoveryReservation)
        .filter(DiscoveryReservation.status == "reserved", DiscoveryReservation.created_at <= cutoff)
        .all()
    )
    changed = 0
    for row in rows:
        before = row.status
        reconcile(
            db,
            row.id,
            charge_known=False,
            actual_minor=None,
            outcome="crashed",
            http_status=None,
            success=False,
            credits=None,
        )
        if before == "reserved":
            changed += 1
    return changed


def ordered_providers(db: Session) -> list[DiscoveryProvider]:
    seed_providers(db)
    db.commit()
    return db.query(DiscoveryProvider).order_by(DiscoveryProvider.priority.asc(), DiscoveryProvider.name.asc()).all()


def usage_report(db: Session) -> dict:
    """Counts and money from stored events. Missing money stays null, not zero."""
    global_usage = db.query(ProviderUsage).filter(ProviderUsage.scope == "global")
    requests = global_usage.count()
    successes = global_usage.filter(ProviderUsage.success.is_(True)).count()
    rate_limited = global_usage.filter(ProviderUsage.http_status == 429).count()
    failures = global_usage.filter(ProviderUsage.success.is_(False), ProviderUsage.http_status != 429).count()
    credit_rows = global_usage.filter(ProviderUsage.credits.isnot(None))
    credits = credit_rows.with_entities(func.coalesce(func.sum(ProviderUsage.credits), 0)).scalar()
    credits_known = credit_rows.count()
    by_provider = (
        db.query(ProviderUsage.provider, func.count(ProviderUsage.id))
        .filter(ProviderUsage.scope == "global")
        .group_by(ProviderUsage.provider)
        .all()
    )
    settled = (
        db.query(func.coalesce(func.sum(DiscoveryReservation.actual_minor), 0), func.count(DiscoveryReservation.id))
        .filter(DiscoveryReservation.status == "settled")
        .one()
    )
    unknown = (
        db.query(func.coalesce(func.sum(DiscoveryReservation.amount_minor), 0), func.count(DiscoveryReservation.id))
        .filter(DiscoveryReservation.status == "unknown_charge")
        .one()
    )
    estimated = (
        db.query(func.coalesce(func.sum(DiscoveryReservation.amount_minor), 0), func.count(DiscoveryReservation.id))
        .filter(DiscoveryReservation.status.in_(("reserved", "settled", "unknown_charge")))
        .one()
    )
    discovered = db.query(func.count(GlobalPerson.id)).filter(GlobalPerson.archived_at.is_(None)).scalar() or 0
    enriched = (
        db.query(func.count(GlobalPerson.id))
        .filter(GlobalPerson.archived_at.is_(None), GlobalPerson.enriched_at.isnot(None))
        .scalar()
        or 0
    )
    verified = (
        db.query(func.count(GlobalPerson.id))
        .filter(GlobalPerson.archived_at.is_(None), GlobalPerson.email_status == "verified")
        .scalar()
        or 0
    )
    actual_minor = int(settled[0]) if int(settled[1]) else None
    unknown_minor = int(unknown[0]) if int(unknown[1]) else None
    estimated_minor = int(estimated[0]) if int(estimated[1]) else None
    per_verified = None
    per_verified_status = "unavailable"
    if unknown_minor is None and actual_minor is not None and verified:
        per_verified = actual_minor // int(verified)
        per_verified_status = "known_minor_units"
    elif unknown_minor is not None:
        per_verified_status = "unknown"
    return {
        "requests": int(requests),
        "successes": int(successes),
        "failures": int(failures),
        "rate_limited": int(rate_limited),
        "credits": int(credits) if credits_known else None,
        "credits_status": "known" if credits_known else "unknown",
        "providers": {name: int(count) for name, count in by_provider},
        "estimated_cost_minor": estimated_minor,
        "estimated_cost_status": "reserved_maximum" if estimated_minor is not None else "unavailable",
        "actual_cost_minor": actual_minor,
        "actual_cost_status": "known" if actual_minor is not None else "unavailable",
        "unknown_cost_minor": unknown_minor,
        "unknown_cost_status": "unknown" if unknown_minor is not None else "none",
        "currency_note": "Amounts are integer minor units of the currency stored on each reservation. Credits are not converted to money.",
        "denominators": {
            "discovered_people": int(discovered),
            "enriched_people": int(enriched),
            "verified_people": int(verified),
        },
        "cost_per_verified_minor": per_verified,
        "cost_per_verified_status": per_verified_status,
    }


def cache_health(db: Session, *, now: datetime | None = None) -> dict:
    from backend.services.discovery_engine.distribution import distribution_summary
    now = now or datetime.utcnow()
    day = now - timedelta(days=1)
    week = now - timedelta(days=7)
    status_rows = (
        db.query(GlobalPerson.email_status, func.count(GlobalPerson.id))
        .filter(GlobalPerson.archived_at.is_(None))
        .group_by(GlobalPerson.email_status)
        .all()
    )
    tier_rows = (
        db.query(GlobalPerson.quality_tier, func.count(GlobalPerson.id))
        .filter(GlobalPerson.archived_at.is_(None))
        .group_by(GlobalPerson.quality_tier)
        .all()
    )
    stale = (
        db.query(func.count(GlobalFieldFreshness.id)).filter(GlobalFieldFreshness.next_due_at < now).scalar() or 0
    )
    fresh = (
        db.query(func.count(GlobalFieldFreshness.id)).filter(GlobalFieldFreshness.next_due_at >= now).scalar() or 0
    )
    people = db.query(func.count(GlobalPerson.id)).filter(GlobalPerson.archived_at.is_(None)).scalar() or 0
    companies = db.query(func.count(GlobalCompany.id)).filter(GlobalCompany.archived_at.is_(None)).scalar() or 0
    ineligible = db.query(func.count(GlobalEvidence.id)).filter(GlobalEvidence.eligible.is_(False)).scalar() or 0
    return {
        "people": int(people),
        "companies": int(companies),
        "people_today": int(
            db.query(func.count(GlobalPerson.id)).filter(GlobalPerson.discovered_at >= day).scalar() or 0
        ),
        "people_week": int(
            db.query(func.count(GlobalPerson.id)).filter(GlobalPerson.discovered_at >= week).scalar() or 0
        ),
        "email_status": {str(key or ""): int(value) for key, value in status_rows},
        "quality_tier": {str(key or ""): int(value) for key, value in tier_rows},
        "stale_fields": int(stale),
        "fresh_fields": int(fresh),
        "ineligible_evidence": int(ineligible),
        "detail": "Stale means next_due_at is in the past. Counts are SQL aggregates. Cost is on the usage report.",
        "distribution": distribution_summary(db),
    }
