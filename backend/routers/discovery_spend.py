"""Platform-admin provider, budget, usage, and cache-health APIs. Secrets are never returned."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from backend.database import get_db
from backend.dependencies import require_platform_admin
from backend.models import DiscoveryBudget, DiscoveryBudgetPeriod, DiscoveryProvider, DiscoveryProviderPrice, DiscoveryProviderSecret, User
from backend.services.discovery_engine.settings import ensure_settings
from backend.services.discovery_engine.spend import cache_health, seed_providers, set_budget, set_credential, set_price, usage_report

router = APIRouter(prefix="/api/platform/discovery", tags=["discovery-spend"])


class SpendSettingsIn(BaseModel):
    paid_providers_enabled: bool = False
    pause_enrichment: bool = True
    pause_email: bool = True
    pause_verify: bool = True


class ProviderIn(BaseModel):
    enabled: bool = False
    priority: int = 100
    rate_per_hour: int = 0


class PriceIn(BaseModel):
    operation: str
    currency: str
    max_cost_minor: int


class CredentialIn(BaseModel):
    api_key: str = Field(min_length=1)
    secret: str = ""


class BudgetIn(BaseModel):
    scope: str
    provider_name: str = ""
    period: str
    currency: str
    limit_minor: int


def _provider_out(db: Session, row: DiscoveryProvider) -> dict:
    prices = db.query(DiscoveryProviderPrice).filter(DiscoveryProviderPrice.provider_name == row.name).all()
    return {
        "name": row.name,
        "enabled": bool(row.enabled),
        "priority": row.priority,
        "rate_per_hour": row.rate_per_hour,
        "supports_email_find": bool(row.supports_email_find),
        "supports_email_verify": bool(row.supports_email_verify),
        "supports_company": False,
        "company_research": "unavailable",
        "credential_configured": db.get(DiscoveryProviderSecret, row.name) is not None,
        "prices": [
            {"operation": price.operation, "currency": price.currency, "max_cost_minor": price.max_cost_minor, "unit": price.unit}
            for price in prices
        ],
    }


@router.get("/spend-settings")
def get_spend_settings(admin: User = Depends(require_platform_admin), db: Session = Depends(get_db)):
    del admin
    row = ensure_settings(db)
    db.commit()
    return {
        "paid_providers_enabled": bool(row.paid_providers_enabled),
        "pause_enrichment": bool(row.pause_enrichment),
        "pause_email": bool(row.pause_email),
        "pause_verify": bool(row.pause_verify),
        "live_public_search": "disabled",
        "detail": "Live public search stays off. These switches do not reveal credentials and do not start a run.",
    }


@router.patch("/spend-settings")
def patch_spend_settings(
    body: SpendSettingsIn,
    admin: User = Depends(require_platform_admin),
    db: Session = Depends(get_db),
):
    del admin
    row = ensure_settings(db)
    row.paid_providers_enabled = bool(body.paid_providers_enabled)
    row.pause_enrichment = bool(body.pause_enrichment)
    row.pause_email = bool(body.pause_email)
    row.pause_verify = bool(body.pause_verify)
    row.live_public_search_enabled = False
    db.commit()
    return {
        "paid_providers_enabled": bool(row.paid_providers_enabled),
        "pause_enrichment": bool(row.pause_enrichment),
        "pause_email": bool(row.pause_email),
        "pause_verify": bool(row.pause_verify),
        "live_public_search": "disabled",
        "detail": "Live public search stays off. These switches do not reveal credentials and do not start a run.",
    }


@router.get("/providers")
def list_providers(admin: User = Depends(require_platform_admin), db: Session = Depends(get_db)):
    del admin
    seed_providers(db)
    db.commit()
    rows = db.query(DiscoveryProvider).order_by(DiscoveryProvider.priority.asc()).all()
    return {"items": [_provider_out(db, row) for row in rows], "company_research": "unavailable"}


@router.patch("/providers/{name}")
def update_provider(
    name: str,
    body: ProviderIn,
    admin: User = Depends(require_platform_admin),
    db: Session = Depends(get_db),
):
    del admin
    seed_providers(db)
    row = db.get(DiscoveryProvider, name)
    if row is None:
        raise HTTPException(status_code=404, detail="Unknown provider")
    if body.rate_per_hour < 0 or body.rate_per_hour > 10000:
        raise HTTPException(status_code=400, detail="rate_per_hour is out of range")
    row.enabled = bool(body.enabled)
    row.priority = int(body.priority)
    row.rate_per_hour = int(body.rate_per_hour)
    row.supports_company = False
    db.commit()
    db.refresh(row)
    return _provider_out(db, row)


@router.put("/providers/{name}/price")
def put_price(
    name: str,
    body: PriceIn,
    admin: User = Depends(require_platform_admin),
    db: Session = Depends(get_db),
):
    del admin
    try:
        row = set_price(
            db,
            provider_name=name,
            operation=body.operation,
            currency=body.currency,
            max_cost_minor=body.max_cost_minor,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"provider": row.provider_name, "operation": row.operation, "currency": row.currency, "max_cost_minor": row.max_cost_minor}


@router.put("/providers/{name}/credential")
def put_credential(
    name: str,
    body: CredentialIn,
    admin: User = Depends(require_platform_admin),
    db: Session = Depends(get_db),
):
    del admin
    try:
        set_credential(db, provider_name=name, api_key=body.api_key, secret=body.secret)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"provider": name, "credential_configured": True}


@router.put("/budgets")
def put_budget(
    body: BudgetIn,
    admin: User = Depends(require_platform_admin),
    db: Session = Depends(get_db),
):
    del admin
    try:
        row = set_budget(
            db,
            scope=body.scope,
            provider_name=body.provider_name,
            period=body.period,
            currency=body.currency,
            limit_minor=body.limit_minor,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"id": row.id, "scope": row.scope, "provider_name": row.provider_name, "period": row.period, "currency": row.currency, "limit_minor": row.limit_minor}


@router.get("/budgets")
def list_budgets(admin: User = Depends(require_platform_admin), db: Session = Depends(get_db)):
    del admin
    rows = db.query(DiscoveryBudget).order_by(DiscoveryBudget.scope.asc(), DiscoveryBudget.provider_name.asc()).all()
    items = []
    for row in rows:
        period = (
            db.query(DiscoveryBudgetPeriod)
            .filter(DiscoveryBudgetPeriod.budget_id == row.id)
            .order_by(DiscoveryBudgetPeriod.period_start.desc())
            .first()
        )
        items.append(
            {
                "scope": row.scope,
                "provider_name": row.provider_name,
                "period": row.period,
                "currency": row.currency,
                "limit_minor": row.limit_minor,
                "reserved_minor": int(period.reserved_minor) if period else 0,
                "settled_minor": int(period.settled_minor) if period else 0,
                "unknown_minor": int(period.unknown_minor) if period else 0,
            }
        )
    return {"items": items}


@router.get("/usage")
def get_usage(admin: User = Depends(require_platform_admin), db: Session = Depends(get_db)):
    del admin
    return usage_report(db)


@router.get("/cache-health")
def get_cache_health(admin: User = Depends(require_platform_admin), db: Session = Depends(get_db)):
    del admin
    return cache_health(db)
