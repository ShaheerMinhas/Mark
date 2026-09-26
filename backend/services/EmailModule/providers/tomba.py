from __future__ import annotations

import os

import httpx

from backend.services.EmailModule.extract import name_parts
from backend.services.EmailModule.providers.base import ProviderFind, ProviderVerify
from backend.services.EmailModule.verify import normalize_provider_verify


class TombaProvider:
    name = "tomba"

    def __init__(self, api_key: str | None = None, secret: str | None = None) -> None:
        self.api_key = (api_key if api_key is not None else os.getenv("TOMBA_API_KEY", "")) or ""
        self.secret = (secret if secret is not None else os.getenv("TOMBA_SECRET", "")) or ""

    def available(self) -> bool:
        return bool(self.api_key)

    def _headers(self) -> dict[str, str]:
        headers = {"X-Tomba-Key": self.api_key}
        if self.secret:
            headers["X-Tomba-Secret"] = self.secret
        return headers

    def find_email(self, *, name: str, first: str, last: str, domain: str, company: str) -> ProviderFind:
        if not self.available() or not domain:
            return ProviderFind(detail="missing key or domain")
        first = first or name_parts(name)[0]
        last = last or name_parts(name)[1]
        params = {"domain": domain, "full_name": name or f"{first} {last}".strip()}
        if first:
            params["first_name"] = first
        if last:
            params["last_name"] = last
        try:
            resp = httpx.get(
                "https://api.tomba.io/v1/email-finder",
                params=params,
                headers=self._headers(),
                timeout=15,
            )
            if resp.status_code == 429:
                return ProviderFind(http_status=429, detail="rate_limited")
            if resp.status_code != 200:
                return ProviderFind(http_status=resp.status_code, detail="http_error")
            data = (resp.json() or {}).get("data") or {}
            email = (data.get("email") or "").strip() or None
            return ProviderFind(email=email, credits=1)
        except Exception as exc:
            return ProviderFind(detail=str(exc), http_status=0)

    def verify_email(self, email: str) -> ProviderVerify:
        if not self.available() or not email:
            return ProviderVerify(detail="missing key or email")
        try:
            resp = httpx.get(
                "https://api.tomba.io/v1/email-verifier",
                params={"email": email},
                headers=self._headers(),
                timeout=15,
            )
            if resp.status_code == 429:
                return ProviderVerify(status="blocked", http_status=429, detail="rate_limited")
            if resp.status_code != 200:
                return ProviderVerify(status="unknown", http_status=resp.status_code)
            data = (resp.json() or {}).get("data") or {}
            raw = str(data.get("result") or data.get("status") or data.get("email") or "")
            if isinstance(data.get("email"), dict):
                raw = str(data["email"].get("result") or data["email"].get("status") or "")
            return ProviderVerify(status=normalize_provider_verify(raw), credits=1, detail=raw)
        except Exception as exc:
            return ProviderVerify(status="unknown", detail=str(exc), http_status=0)
