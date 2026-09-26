from __future__ import annotations

import os

import httpx

from backend.services.EmailModule.extract import name_parts
from backend.services.EmailModule.providers.base import ProviderFind, ProviderVerify
from backend.services.EmailModule.verify import normalize_provider_verify


class ProspeoProvider:
    name = "prospeo"

    def __init__(self, api_key: str | None = None) -> None:
        self.api_key = (api_key if api_key is not None else os.getenv("PROSPEO_API_KEY", "")) or ""

    def available(self) -> bool:
        return bool(self.api_key)

    def _headers(self) -> dict[str, str]:
        return {"X-KEY": self.api_key, "Content-Type": "application/json"}

    def find_email(self, *, name: str, first: str, last: str, domain: str, company: str) -> ProviderFind:
        if not self.available():
            return ProviderFind(detail="missing key")
        first = first or name_parts(name)[0]
        last = last or name_parts(name)[1]
        try:
            resp = httpx.post(
                "https://api.prospeo.io/email-finder",
                json={"first_name": first, "last_name": last, "company": domain or company},
                headers=self._headers(),
                timeout=15,
            )
            if resp.status_code == 429:
                return ProviderFind(http_status=429, detail="rate_limited")
            if resp.status_code >= 400:
                return ProviderFind(http_status=resp.status_code, detail="http_error")
            data = (resp.json() or {}).get("response") or resp.json() or {}
            email = (data.get("email") or "").strip() or None
            return ProviderFind(email=email, credits=1)
        except Exception as exc:
            return ProviderFind(detail=str(exc), http_status=0)

    def verify_email(self, email: str) -> ProviderVerify:
        if not self.available() or not email:
            return ProviderVerify(detail="missing key or email")
        try:
            resp = httpx.post(
                "https://api.prospeo.io/email-verifier",
                json={"email": email},
                headers=self._headers(),
                timeout=15,
            )
            if resp.status_code == 429:
                return ProviderVerify(status="blocked", http_status=429, detail="rate_limited")
            if resp.status_code >= 400:
                return ProviderVerify(status="unknown", http_status=resp.status_code)
            data = (resp.json() or {}).get("response") or resp.json() or {}
            raw = str(data.get("email_status") or data.get("status") or "")
            return ProviderVerify(status=normalize_provider_verify(raw), credits=1, detail=raw)
        except Exception as exc:
            return ProviderVerify(status="unknown", detail=str(exc), http_status=0)
