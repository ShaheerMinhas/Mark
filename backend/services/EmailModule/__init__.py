"""EmailModule — public discovery, patterns, providers, and verification.

Does not invent addresses. Search-time uses snippets only. Use
``EmailEnrichmentService`` or POST /api/searches/{id}/emails for the full waterfall.
"""

from backend.services.EmailModule.extract import extract_emails, extract_first_email
from backend.services.EmailModule.finder import find_email, find_emails
from backend.services.EmailModule.intelligence import EmailEnrichmentService, enrich_person
from backend.services.EmailModule.types import EmailHit, EmailResult, PersonInput

__all__ = [
    "EmailHit",
    "EmailResult",
    "EmailEnrichmentService",
    "PersonInput",
    "extract_emails",
    "extract_first_email",
    "find_email",
    "find_emails",
    "enrich_person",
]
