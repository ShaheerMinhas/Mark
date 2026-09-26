"""CompanyModule — public facts about an employer.

Given a company name, look up website/domain and headquarters location.
Does not invent values.
"""

from backend.services.CompanyModule.finder import lookup_company
from backend.services.CompanyModule.types import CompanyProfile

__all__ = ["CompanyProfile", "lookup_company"]
