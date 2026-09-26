from __future__ import annotations

import os

from backend.services.EmailModule.providers.base import EmailProvider
from backend.services.EmailModule.providers.findymail import FindymailProvider
from backend.services.EmailModule.providers.hunter import HunterProvider
from backend.services.EmailModule.providers.leadmagic import LeadMagicProvider
from backend.services.EmailModule.providers.prospeo import ProspeoProvider
from backend.services.EmailModule.providers.tomba import TombaProvider

_FACTORY = {
    "hunter": HunterProvider,
    "tomba": TombaProvider,
    "leadmagic": LeadMagicProvider,
    "prospeo": ProspeoProvider,
    "findymail": FindymailProvider,
}

DEFAULT_ORDER = ("hunter", "tomba", "leadmagic", "prospeo", "findymail")


def provider_order() -> list[str]:
    raw = (os.getenv("EMAIL_PROVIDER") or os.getenv("EMAIL_PROVIDERS") or "").strip()
    if not raw:
        return list(DEFAULT_ORDER)
    names = [part.strip().lower() for part in raw.split(",") if part.strip()]
    return names or list(DEFAULT_ORDER)


def configured_providers(overrides: dict[str, EmailProvider] | None = None) -> list[EmailProvider]:
    if overrides is not None:
        return [overrides[name] for name in provider_order() if name in overrides and overrides[name].available()]
    out: list[EmailProvider] = []
    for name in provider_order():
        factory = _FACTORY.get(name)
        if not factory:
            continue
        provider = factory()
        if provider.available():
            out.append(provider)
    return out
