"""Global email enrichment. Company research has no adapter and is never invented.

Calls use the platform credential passed into the provider constructor. They do
not read customer environment keys and they do not run until a reservation exists.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from backend.models import (
    DiscoveryProvider,
    DiscoveryRun,
    DiscoverySettings,
    DiscoveryTarget,
    GlobalEvidence,
    GlobalFieldFreshness,
    GlobalPerson,
)
from backend.services.crypto import decrypt_value
from backend.services.discovery_engine.spend import (
    COMPANY_OPERATION,
    BudgetDenied,
    gate_reason,
    ordered_providers,
    reconcile,
    reserve_max,
)
from backend.services.discovery_engine.ssrf import public_http_url
from backend.services.discovery_engine.suppression import is_suppressed
from backend.services.discovery_engine.targeting import apply_criteria, field_rules
from backend.services.global_cache.policy import load_rules, quality_tier

_ADAPTERS: dict[tuple[str, str], object] = {}
_STATUSES = {"found", "verified", "invalid", "accept_all", "unknown", "not_found", "inferred"}


@dataclass
class AdapterResult:
    email_status: str = "unknown"
    email: str | None = None
    http_status: int | None = None
    credits: int | None = None
    charge_known: bool = False
    actual_minor: int | None = None
    source_url: str | None = None
    detail: str = ""


def use_adapter(provider: str, operation: str, fn) -> None:
    _ADAPTERS[(provider, operation)] = fn


def clear_adapters() -> None:
    _ADAPTERS.clear()


def _secret(db: Session, provider_name: str) -> dict | None:
    from backend.models import DiscoveryProviderSecret

    row = db.get(DiscoveryProviderSecret, provider_name)
    if row is None:
        return None
    return json.loads(decrypt_value(row.ciphertext))


def invoke_provider(provider_name: str, operation: str, secret: dict, person: GlobalPerson) -> AdapterResult:
    """One adapter call. A test hook replaces the network client. A missing hook uses the stored key only."""
    hook = _ADAPTERS.get((provider_name, operation))
    if hook is not None:
        return hook(person)
    if not secret.get("api_key"):
        raise RuntimeError("refusing a global call without a platform credential")
    from backend.services.EmailModule.providers.findymail import FindymailProvider
    from backend.services.EmailModule.providers.hunter import HunterProvider
    from backend.services.EmailModule.providers.leadmagic import LeadMagicProvider
    from backend.services.EmailModule.providers.prospeo import ProspeoProvider
    from backend.services.EmailModule.providers.tomba import TombaProvider

    factories = {
        "hunter": lambda: HunterProvider(api_key=secret["api_key"]),
        "leadmagic": lambda: LeadMagicProvider(api_key=secret["api_key"]),
        "prospeo": lambda: ProspeoProvider(api_key=secret["api_key"]),
        "findymail": lambda: FindymailProvider(api_key=secret["api_key"]),
        "tomba": lambda: TombaProvider(api_key=secret["api_key"], secret=secret.get("secret") or ""),
    }
    factory = factories.get(provider_name)
    if factory is None:
        return AdapterResult(email_status="unknown", detail="unsupported")
    client = factory()
    if operation == "email_find":
        found = client.find_email(
            name=person.full_name or "",
            first=person.given_name or "",
            last=person.family_name or "",
            domain=person.domain or "",
            company=person.company_name or "",
        )
        status = "not_found" if not found.email else "found"
        raw = (found.raw_status or "").lower()
        if "infer" in raw:
            status = "inferred"
        return AdapterResult(
            email_status=status,
            email=found.email,
            http_status=found.http_status,
            credits=found.credits,
            charge_known=False,
            detail="provider reported credits only" if found.credits is not None else "",
        )
    checked = client.verify_email(person.email or "")
    mapped = {
        "valid": "verified",
        "invalid": "invalid",
        "accept_all": "accept_all",
        "unknown": "unknown",
        "disposable": "invalid",
        "role_based": "invalid",
        "blocked": "unknown",
    }.get(checked.status, "unknown")
    return AdapterResult(
        email_status=mapped,
        email=person.email,
        http_status=checked.http_status,
        credits=checked.credits,
        charge_known=False,
        detail="provider reported credits only" if checked.credits is not None else "",
    )


def _fresh(db: Session, person: GlobalPerson, now: datetime) -> bool:
    row = (
        db.query(GlobalFieldFreshness)
        .filter(GlobalFieldFreshness.person_id == person.id, GlobalFieldFreshness.field_name == "email")
        .one_or_none()
    )
    return bool(row and row.next_due_at and row.next_due_at > now and person.email)


def _apply_email(db: Session, person: GlobalPerson, result: AdapterResult, provider_name: str, refresh_hours: int, now: datetime) -> None:
    status = result.email_status if result.email_status in _STATUSES else "unknown"
    if status == "inferred":
        status = "inferred"
    if person.email and person.email_status in {"found", "verified"} and status == "inferred":
        return
    person.email_status = status
    if result.email and status != "not_found":
        person.email = result.email.strip().lower()
    if status == "verified":
        person.email_confidence = "verified"
        person.contact_note = "A provider marked this address verified."
    elif status == "inferred":
        person.email_confidence = "inferred"
        person.contact_note = "A provider returned a pattern. The mailbox was not verified."
    elif status == "found":
        person.email_confidence = "provider"
        person.contact_note = "A provider returned this address. It was not verified."
    rules = load_rules(db)
    tier, score = quality_tier(
        rules=rules,
        email=person.email,
        email_status=person.email_status,
        has_identity=bool(person.profile_url_normalized or person.email),
        has_role=bool(person.title or person.job_function),
    )
    if person.email_status == "inferred" and tier in {"verified", "contactable"}:
        tier, score = "enriched" if (person.profile_url_normalized or person.title) else "basic", score
    person.quality_tier = tier
    person.quality_score = score
    person.enriched_at = now
    if status == "verified":
        person.verified_at = now
    person.updated_at = now
    fresh = (
        db.query(GlobalFieldFreshness)
        .filter(GlobalFieldFreshness.person_id == person.id, GlobalFieldFreshness.field_name == "email")
        .one_or_none()
    )
    if fresh is None:
        fresh = GlobalFieldFreshness(person_id=person.id, field_name="email")
        db.add(fresh)
    fresh.source = provider_name
    fresh.observed_at = now
    fresh.enriched_at = now
    fresh.verified_at = now if status == "verified" else fresh.verified_at
    fresh.next_due_at = now + timedelta(hours=max(1, refresh_hours))
    safe_url = public_http_url(result.source_url)
    db.add(
        GlobalEvidence(
            subject_type="person",
            subject_id=person.id,
            field_name="email",
            value_text=person.email_status,
            source_url=safe_url,
            provider=provider_name,
            share_scope="public",
            eligible=True,
            observed_at=now,
        )
    )


def _call(
    db: Session,
    *,
    provider: DiscoveryProvider,
    operation: str,
    person: GlobalPerson,
    run: DiscoveryRun,
    attempt: int,
    secret: dict,
) -> tuple[str, AdapterResult | None]:
    reason = gate_reason(db, provider, operation)
    if reason:
        return reason, None
    key = f"run:{run.id}:person:{person.id}:op:{operation}:attempt:{attempt}:provider:{provider.name}"
    try:
        reservation = reserve_max(
            db,
            idempotency_key=key,
            provider_name=provider.name,
            operation=operation,
            run_id=run.id,
            person_id=person.id,
            attempt=attempt,
        )
    except BudgetDenied as exc:
        return exc.reason, None
    if reservation.status != "reserved":
        return "already reconciled", None
    try:
        result = invoke_provider(provider.name, operation, secret, person)
    except Exception:
        reconcile(
            db,
            reservation.id,
            charge_known=False,
            actual_minor=None,
            outcome="exception",
            http_status=None,
            success=False,
            credits=None,
        )
        return "exception", None
    success = result.http_status not in {429, 0} and (result.http_status or 200) < 400
    outcome = "rate_limited" if result.http_status == 429 else ("success" if success else "failure")
    reconcile(
        db,
        reservation.id,
        charge_known=result.charge_known,
        actual_minor=result.actual_minor,
        outcome=outcome,
        http_status=result.http_status,
        success=success and outcome == "success",
        credits=result.credits,
    )
    return outcome, result


def _candidates(db: Session, target: DiscoveryTarget, rules: dict) -> list[GlobalPerson]:
    from backend.services.discovery_engine.suppression import suppressed_people_filter

    query = apply_criteria(
        db,
        suppressed_people_filter(db, db.query(GlobalPerson).filter(GlobalPerson.archived_at.is_(None))),
        dict(target.criteria or {}),
        rules,
    )
    limit = max(1, int(target.max_people_per_run or 25))
    return query.order_by(GlobalPerson.id.asc()).limit(limit).all()


def run_email_stage(db: Session, run: DiscoveryRun, target: DiscoveryTarget, settings: DiscoverySettings, attempt: int) -> tuple[str, str]:
    from backend.services.discovery_engine.contacts import process_contact_jobs

    public_detail = process_contact_jobs(db, run.id)
    if not settings.paid_providers_enabled:
        return (
            "skipped",
            public_detail
            + " Paid providers are disabled. No credential or budget ledger is consulted because spend cannot be reserved.",
        )
    if settings.pause_email:
        return "skipped", "Email enrichment is paused. No provider was called."
    rules = field_rules(db)
    email_rule = rules.get("email")
    if email_rule is not None and not email_rule.collection_enabled:
        return "skipped", "Email collection is off. Cached rows were not deleted and no provider was called."
    now = datetime.utcnow()
    refresh_hours = int(email_rule.refresh_hours) if email_rule is not None else 168
    preferred = (email_rule.enrichment_source or "").strip() if email_rule is not None else ""
    providers = ordered_providers(db)
    if preferred:
        providers = sorted(providers, key=lambda row: (0 if row.name == preferred else 1, row.priority, row.name))
    people = _candidates(db, target, rules)
    called = fresh = denied = 0
    for person in people:
        db.refresh(person)
        if is_suppressed(db, email=person.email, domain=person.domain, profile_url=person.profile_url):
            denied += 1
            continue
        if _fresh(db, person, now):
            fresh += 1
            continue
        outcome, result, provider_name = _waterfall(db, providers, "email_find", person, run, attempt)
        if outcome != "success" or result is None:
            denied += 1
            continue
        called += 1
        used = provider_name
        if (
            result.email_status == "found"
            and result.email_status != "inferred"
            and not settings.pause_verify
        ):
            person.email = result.email
            verify_outcome, verify_result, verify_provider = _waterfall(db, providers, "email_verify", person, run, attempt)
            if verify_outcome == "success" and verify_result is not None:
                result = verify_result
                used = verify_provider or used
                called += 1
        _apply_email(db, person, result, used or "global", refresh_hours, now)
        db.commit()
    db.commit()
    if called == 0 and fresh == 0 and denied == 0 and not people:
        return "skipped", public_detail + " No cached person matched this target. No provider was called."
    return "classified", (
        f"{public_detail} Email calls {called}. Fresh skips {fresh}. Denied or suppressed {denied}. "
        "Configured adapters return an email, not a phone number. "
        "Company research was not called. Inferred addresses were not marked verified."
    )


def _waterfall(db, providers, operation, person, run, attempt):
    last = "no provider"
    for provider in providers:
        secret = _secret(db, provider.name) or {}
        outcome, result = _call(
            db,
            provider=provider,
            operation=operation,
            person=person,
            run=run,
            attempt=attempt,
            secret=secret,
        )
        last = outcome
        if outcome == "success" and result is not None:
            return outcome, result, provider.name
        if outcome in {"rate_limited", "credential missing", "price missing", "disabled", "rate cap", "rate cap is not configured", "budget exceeded", "unsupported", "failure", "exception"}:
            continue
    return last, None, None


def run_enrich_stage(settings: DiscoverySettings) -> tuple[str, str]:
    """Company research is unsupported. A skipped paid switch is not reported as finished work."""
    if not settings.paid_providers_enabled:
        return "skipped", "Paid providers are disabled. No credential or budget ledger is consulted because spend cannot be reserved."
    if settings.pause_enrichment:
        return "skipped", "Company enrichment is paused. No provider was called."
    return "unavailable", f"{COMPANY_OPERATION} has no adapter. No company research call was made."


