"""Quality tiers are policy labels. They are not a claim that an address is correct."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session

DEFAULT_RULES = {
    "verified_email_statuses": ["verified"],
    "contactable_email_statuses": ["found", "unverified"],
    "inferred_is_contactable": False,
    "accept_all_is_verified": False,
    "tier_scores": {"basic": 0.25, "enriched": 0.55, "contactable": 0.8, "verified": 1.0},
}

KNOWN_EMAIL_STATUSES = {
    "found",
    "verified",
    "invalid",
    "accept_all",
    "unknown",
    "not_found",
    "inferred",
    "unverified",
    "verify_uncertain",
}


def ensure_default_policy(db: Session) -> dict:
    from backend.models import CachePolicy

    row = db.query(CachePolicy).filter(CachePolicy.name == "default").one_or_none()
    if row is None:
        row = CachePolicy(name="default", rules=dict(DEFAULT_RULES), updated_at=datetime.utcnow())
        db.add(row)
        db.flush()
    rules = dict(DEFAULT_RULES)
    rules.update(dict(row.rules or {}))
    return rules


def load_rules(db: Session) -> dict:
    return ensure_default_policy(db)


def quality_tier(
    *,
    rules: dict,
    email: str | None,
    email_status: str | None,
    has_identity: bool,
    has_role: bool,
) -> tuple[str, float]:
    status = (email_status or "").strip().lower() or None
    scores = dict(rules.get("tier_scores") or DEFAULT_RULES["tier_scores"])
    verified_statuses = {s.lower() for s in rules.get("verified_email_statuses") or []}
    contactable_statuses = {s.lower() for s in rules.get("contactable_email_statuses") or []}
    if not rules.get("accept_all_is_verified", False):
        verified_statuses.discard("accept_all")
    if not rules.get("inferred_is_contactable", False):
        contactable_statuses.discard("inferred")

    if email and status in verified_statuses and has_identity:
        tier = "verified"
    elif email and status in contactable_statuses and status != "inferred":
        tier = "contactable"
    elif has_identity and has_role:
        tier = "enriched"
    else:
        tier = "basic"
    return tier, float(scores.get(tier, 0.0))
