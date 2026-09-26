from __future__ import annotations

from backend.services.EmailModule.extract import name_parts
from backend.services.EmailModule.providers.hunter import HunterProvider
from backend.services.EmailModule.types import EmailHit


def hunter_find(name: str, domain: str, *, api_key: str | None = None) -> EmailHit | None:
    first, last = name_parts(name)
    found = HunterProvider(api_key).find_email(
        name=name, first=first, last=last, domain=domain, company=""
    )
    if not found.email:
        return None
    return EmailHit(
        email=found.email,
        source="hunter",
        confidence="found",
        evidence=found.detail or "Hunter.io",
        domain=domain,
    )
