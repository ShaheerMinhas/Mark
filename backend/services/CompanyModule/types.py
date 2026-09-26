from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class CompanyProfile:
    name: str
    domain: str = ""
    website: str = ""
    location: str = ""
    source: str = ""
    evidence: str = ""

    def to_dict(self) -> dict[str, str]:
        return asdict(self)
