from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass
class ProviderFind:
    email: str | None = None
    raw_status: str = ""
    credits: int | None = None
    detail: str = ""
    http_status: int = 200


@dataclass
class ProviderVerify:
    status: str = "unknown"
    credits: int | None = None
    detail: str = ""
    http_status: int = 200


class EmailProvider(Protocol):
    name: str

    def available(self) -> bool: ...
    def find_email(self, *, name: str, first: str, last: str, domain: str, company: str) -> ProviderFind: ...
    def verify_email(self, email: str) -> ProviderVerify: ...
