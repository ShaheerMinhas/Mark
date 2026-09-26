from __future__ import annotations

import httpx

from backend.config import HUNTER_API_KEY
from backend.services.EmailModule.extract import name_parts
from backend.services.EmailModule.providers.base import ProviderFind, ProviderVerify
from backend.services.EmailModule.verify import normalize_provider_verify


class HunterProvider:
    name = "hunter"
    _finder = "https://api.hunter.io/v2/email-finder"
    _verifier = "https://api.hunter.io/v2/email-verifier"

    def __init__(self, api_key: str | None = None) -> None:
        self.api_key = (api_key if api_key is not None else HUNTER_API_KEY) or ""

    def available(self) -> bool:
        return bool(self.api_key)

    def find_email(self, *, name: str, first: str, last: str, domain: str, company: str) -> ProviderFind:
        if not self.api_key or not domain:
            return ProviderFind(detail="missing key or domain")
        first = first or name_parts(name)[0]
        last = last or name_parts(name)[1]
        if not first:
            return ProviderFind(detail="missing first name")
        params = {"domain": domain, "first_name": first, "api_key": self.api_key}
        if last:
            params["last_name"] = last
        if company:
            params["company"] = company
        try:
            resp = httpx.get(self._finder, params=params, timeout=15)
            if resp.status_code == 429:
                return ProviderFind(http_status=429, detail="rate_limited")
            if resp.status_code != 200:
                return ProviderFind(http_status=resp.status_code, detail="http_error")
            data = (resp.json() or {}).get("data") or {}
            email = (data.get("email") or "").strip() or None
            return ProviderFind(email=email, raw_status=str(data.get("score") or ""), credits=1)
        except Exception as exc:
            return ProviderFind(detail=str(exc), http_status=0)

    def verify_email(self, email: str) -> ProviderVerify:
        if not self.api_key or not email:
            return ProviderVerify(detail="missing key or email")
        try:
            resp = httpx.get(self._verifier, params={"email": email, "api_key": self.api_key}, timeout=15)
            if resp.status_code == 429:
                return ProviderVerify(status="blocked", http_status=429, detail="rate_limited")
            if resp.status_code != 200:
                return ProviderVerify(status="unknown", http_status=resp.status_code, detail="http_error")
            data = (resp.json() or {}).get("data") or {}
            raw = str(data.get("status") or data.get("result") or "")
            return ProviderVerify(status=normalize_provider_verify(raw), credits=1, detail=raw)
        except Exception as exc:
            return ProviderVerify(status="unknown", detail=str(exc), http_status=0)
