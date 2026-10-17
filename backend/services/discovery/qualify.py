"""Stage B — qualification.

The previous pipeline dropped a candidate by returning early from its extraction
loop, so there was no record of how many people were found or why they were
discarded. Here every candidate keeps its row and gains a ``relevance_score``,
a ``filter_match`` breakdown and, when rejected, a ``rejection_reason``.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Sequence

from sqlalchemy.orm import Session

from backend.lead_finder import (
    _activity_status,
    _as_list,
    _business_associated,
    _canonical_job_titles,
    _hit_matches_location,
    _hit_matches_terms,
    _hit_matches_titles,
    _has_marketing_cue,
    _matches_sector,
    _relevance_score,
    _required_terms,
    _sector_terms,
    _serp_title_head,
    _wants_marketing,
    _wants_usa,
)
from backend.models import CandidateStatus, SearchCandidate
from backend.services.CompanyModule.finder import is_usable_company
from backend.services.discovery.queries import (
    _extend_unique,
    expand_geography,
    expand_industry,
)

logger = logging.getLogger(__name__)


class RejectionReason:
    MISSING_TERMS = "missing_required_terms"
    TITLE_MISMATCH = "title_mismatch"
    NO_COMPANY = "no_usable_company"
    BELOW_SCORE = "below_minimum_score"


# Hard gates disqualify outright: the user named a company we did not match, the
# role is not the one asked for, or there is no employer when one was required.
# Everything else is a soft signal that moves the score instead of deleting the
# candidate — a "Marketing Manager at Nordic Freight AB" should rank below an
# explicit logistics match for a logistics search, not disappear from it.
SECTOR_MATCH_BONUS = 0.06
SECTOR_MISS_PENALTY = 0.18
LOCATION_MATCH_BONUS = 0.04
LOCATION_MISS_PENALTY = 0.15
ROLE_FOCUS_MISS_PENALTY = 0.10
INACTIVE_PENALTY = 0.05
#: A failed company parse is a gap in our data, not evidence the person is wrong.
#: Enrichment resolves the employer later, so rank these down instead of dropping
#: them — on live searches this gate alone discarded ~40% of the candidate pool.
NO_COMPANY_PENALTY = 0.12

#: Rank floor after hard gates. Title already matched; sector/location only
#: move the person up or down the list instead of deleting them. Live pools were
#: throwing away ~60% of title-matched people as ``below_minimum_score``.
DEFAULT_MIN_SCORE = 0.22

#: Soft signals must not stack into a hard reject. Two misses used to subtract
#: 0.33 and dump a correct title below the cut.
MAX_SOFT_PENALTY = 0.22


class Criteria:
    """Precomputed matching rules, derived once per search."""

    def __init__(self, prompt: str, filters: dict[str, Any] | None) -> None:
        f = filters or {}
        self.prompt = prompt or ""
        self.location = str(f.get("location") or "")
        self.industry = str(f.get("industry") or "")
        self.company = str(f.get("company") or "")
        self.companies = _as_list(self.company)
        self.industries = _as_list(self.industry)
        self.job_titles = _canonical_job_titles(_as_list(f.get("job_titles")), self.prompt)
        self.required_terms = _required_terms(
            self.prompt, companies=self.companies, industries=self.industries
        )
        # Qualification has to recognise the same vocabulary discovery searched
        # with. Without this, widening the query plan to "freight"/"shipping" finds
        # people and then rejects them for not saying "logistics" — on a live run
        # that dropped the qualification rate from 55% to 19%.
        self.sector_terms = _extend_unique(
            _sector_terms(self.prompt, self.industry, self.company),
            expand_industry(self.industry or self.prompt),
        )
        self.nearby_places = expand_geography(self.location, self.prompt)
        self.wants_marketing = _wants_marketing(self.prompt, self.job_titles)
        self.wants_us = _wants_usa(self.location, self.prompt)
        self.active_only = bool(f.get("active_only", True))
        self.business_only = bool(f.get("business_only", True))
        # A separate opt-in key: the API fills ``business_only`` with a default on
        # every search, so its presence cannot signal that the user asked for it.
        self.require_company = bool(f.get("require_company", False))
        self.min_score = float(f.get("min_relevance_score") or DEFAULT_MIN_SCORE)


def _mentions_nearby_place(places: Sequence[str], *texts: str) -> bool:
    """True when any expanded city for the requested country appears in the text.

    A profile in Gothenburg satisfies a search for Sweden; the base location
    matcher only compares against the country string the user typed.
    """
    if not places:
        return False
    haystack = " ".join(t for t in texts if t).lower()
    if not haystack:
        return False
    return any(
        re.search(rf"\b{re.escape(place.lower())}\b", haystack) for place in places
    )


def _evaluate(candidate: SearchCandidate, criteria: Criteria) -> tuple[bool, str | None, dict]:
    """Return ``(qualified, rejection_reason, filter_match)`` for one candidate."""
    person = candidate.name or ""
    body = candidate.raw_snippet or ""
    role = candidate.title or ""
    company = candidate.company or ""
    # The parsed fields are derived from the SERP title, so recombining them
    # reconstructs the text the title-side matchers originally saw.
    haystack_title = f"{person} {role} {company}".strip()

    matched: dict[str, Any] = {}

    # --- Hard gates ---------------------------------------------------------
    matched["required_terms"] = _hit_matches_terms(haystack_title, body, criteria.required_terms)
    if not matched["required_terms"]:
        return False, RejectionReason.MISSING_TERMS, matched

    matched["job_title"] = _hit_matches_titles(
        haystack_title, body, criteria.job_titles, prompt=criteria.prompt
    )
    if not matched["job_title"]:
        return False, RejectionReason.TITLE_MISMATCH, matched

    matched["usable_company"] = is_usable_company(company)
    if criteria.require_company and not matched["usable_company"]:
        return False, RejectionReason.NO_COMPANY, matched

    # --- Soft signals -------------------------------------------------------
    activity, activity_boost = _activity_status(haystack_title, body)
    matched["activity_status"] = activity
    matched["business_associated"] = _business_associated(haystack_title, body, company)
    matched["sector"] = _matches_sector(
        criteria.sector_terms, name=person, company=company, role=role, body=body
    )
    matched["location"] = _hit_matches_location(
        haystack_title,
        body,
        candidate.location or "",
        location=criteria.location,
        prompt=criteria.prompt,
        company=company,
    ) or _mentions_nearby_place(
        criteria.nearby_places, candidate.location or "", haystack_title, body
    )
    matched["role_focus"] = (
        bool(
            _has_marketing_cue(role)
            or _has_marketing_cue(_serp_title_head(haystack_title))
            or _has_marketing_cue(body[:280])
        )
        if criteria.wants_marketing
        else True
    )

    score = _relevance_score(
        prompt=criteria.prompt,
        title=haystack_title,
        body=body,
        role=role,
        company=company,
        location=candidate.location or "",
        job_titles=criteria.job_titles,
        wants_marketing=criteria.wants_marketing,
        wants_us=criteria.wants_us,
    )
    score += activity_boost
    soft = 0.0
    soft += SECTOR_MATCH_BONUS if matched["sector"] else -SECTOR_MISS_PENALTY
    soft += LOCATION_MATCH_BONUS if matched["location"] else -LOCATION_MISS_PENALTY
    if not matched["role_focus"]:
        soft -= ROLE_FOCUS_MISS_PENALTY
    if not matched["usable_company"]:
        soft -= NO_COMPANY_PENALTY
    if criteria.active_only and activity == "Unknown activity" and not matched["business_associated"]:
        soft -= INACTIVE_PENALTY
    # Two independent misses (wrong city *and* sector not in the snippet) used
    # to stack past the cut-off and hide a person whose job title matched.
    score += max(soft, -MAX_SOFT_PENALTY)

    score = round(max(0.0, min(0.96, score)), 2)
    matched["relevance_score"] = score
    if score < criteria.min_score:
        return False, RejectionReason.BELOW_SCORE, matched

    return True, None, matched


def qualify_candidates(
    db: Session,
    *,
    search_job_id: int,
    prompt: str,
    filters: dict[str, Any] | None,
    batch_size: int = 200,
) -> dict[str, int]:
    """Score and classify every unqualified candidate for a search."""
    criteria = Criteria(prompt, filters)
    stats = {"evaluated": 0, "qualified": 0, "rejected": 0}

    while True:
        batch = (
            db.query(SearchCandidate)
            .filter(
                SearchCandidate.search_job_id == search_job_id,
                SearchCandidate.status == CandidateStatus.DISCOVERED.value,
            )
            .order_by(SearchCandidate.id)
            .limit(batch_size)
            .all()
        )
        if not batch:
            break

        for candidate in batch:
            qualified, reason, matched = _evaluate(candidate, criteria)
            candidate.filter_match = matched
            candidate.relevance_score = float(matched.get("relevance_score") or 0.0)
            if qualified:
                candidate.status = CandidateStatus.QUALIFIED.value
                candidate.rejection_reason = None
                stats["qualified"] += 1
            else:
                candidate.status = CandidateStatus.REJECTED.value
                candidate.rejection_reason = reason
                stats["rejected"] += 1
            stats["evaluated"] += 1
        db.commit()

    return stats


def rejection_breakdown(db: Session, search_job_id: int) -> dict[str, int]:
    """Counts per rejection reason, for surfacing why a pool shrank."""
    from sqlalchemy import func

    rows = (
        db.query(SearchCandidate.rejection_reason, func.count(SearchCandidate.id))
        .filter(
            SearchCandidate.search_job_id == search_job_id,
            SearchCandidate.rejection_reason.isnot(None),
        )
        .group_by(SearchCandidate.rejection_reason)
        .all()
    )
    return {str(reason): int(count) for reason, count in rows}
