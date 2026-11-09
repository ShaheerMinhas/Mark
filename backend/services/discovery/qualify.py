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
    expand_company,
    expand_geography,
    expand_industry,
)

logger = logging.getLogger(__name__)


class RejectionReason:
    MISSING_TERMS = "missing_required_terms"
    TITLE_MISMATCH = "title_mismatch"
    NO_COMPANY = "no_usable_company"
    BELOW_SCORE = "below_minimum_score"
    FOREIGN_COLLISION = "foreign_name_collision"


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
#: When the user named an employer, a clear match should rise; a miss should drop
#: but not delete — missing company parse is common on SERP snippets.
COMPANY_MATCH_BONUS = 0.10
COMPANY_MISS_PENALTY = 0.08

#: Rank floor after hard gates. Title already matched; sector/location only
#: move the person up or down the list instead of deleting them. Live pools were
#: throwing away ~60% of title-matched people as ``below_minimum_score``.
DEFAULT_MIN_SCORE = 0.22

#: Soft signals must not stack into a hard reject. Two misses used to subtract
#: 0.33 and dump a correct title below the cut.
MAX_SOFT_PENALTY = 0.22

#: SERP hits for Pakistani universities that are actually foreign namesakes.
_FOREIGN_COLLISION = re.compile(
    r"(?i)\b(namibia|windhoek|nust\.na|university of montana|\bmso\.umt\.edu\b|"
    r"missoula|central university of punjab|cup\.edu\.in|bathinda|"
    r"greater jaipur|jaunpur)\b"
)


class Criteria:
    """Precomputed matching rules, derived once per search."""

    def __init__(self, prompt: str, filters: dict[str, Any] | None) -> None:
        f = filters or {}
        self.prompt = prompt or ""
        self.location = str(f.get("location") or "")
        self.industry = str(f.get("industry") or "")
        self.company = str(f.get("company") or "")
        self.companies = _as_list(self.company)
        for named in list(self.companies):
            self.companies = _extend_unique(self.companies, expand_company(named))
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
        loc_l = self.location.lower()
        self.guard_pakistan = bool(
            re.search(r"(?i)\bpakistan\b", self.prompt)
            or loc_l in {
                "pakistan", "lahore", "islamabad", "karachi", "faisalabad",
                "rawalpindi", "multan", "peshawar", "gujranwala",
            }
            or any(
                re.search(r"(?i)\b(nust|lums|umt|ucp|fast|nuces|uet)\b", c)
                for c in self.companies
            )
        )


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


def _mentions_requested_company(companies: Sequence[str], *texts: str) -> bool:
    """True when any named employer (or alias) appears in the candidate text."""
    if not companies:
        return False
    haystack = " ".join(t for t in texts if t).lower()
    if not haystack:
        return False
    for named in companies:
        token = (named or "").strip().lower()
        if len(token) < 2:
            continue
        if re.search(rf"\b{re.escape(token)}\b", haystack):
            return True
    return False


def _evaluate(candidate: SearchCandidate, criteria: Criteria) -> tuple[bool, str | None, dict]:
    """Return ``(qualified, rejection_reason, filter_match)`` for one candidate."""
    person = candidate.name or ""
    body = candidate.raw_snippet or ""
    role = candidate.title or ""
    company = candidate.company or ""
    # The parsed fields are derived from the SERP title, so recombining them
    # reconstructs the text the title-side matchers originally saw.
    haystack_title = f"{person} {role} {company}".strip()
    evidence = f"{haystack_title} {candidate.location or ''} {body}"

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

    # Drop clear foreign namesakes (NUST Namibia, UMT Montana, CUP India) when
    # the search is Pakistan / Pakistani-university scoped. Does not require a
    # company parse — only rejects the collision itself.
    matched["foreign_collision"] = bool(
        criteria.guard_pakistan and _FOREIGN_COLLISION.search(evidence)
    )
    if matched["foreign_collision"]:
        return False, RejectionReason.FOREIGN_COLLISION, matched

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
    matched["company_match"] = (
        _mentions_requested_company(
            criteria.companies, company, haystack_title, body, candidate.location or ""
        )
        if criteria.companies
        else True
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
    if criteria.companies:
        soft += COMPANY_MATCH_BONUS if matched["company_match"] else -COMPANY_MISS_PENALTY
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
    # Separate evidence-based breakdown (person, company, title, location, identity, email).
    title_match = 0.85 if matched["job_title"] and role else (0.55 if matched["job_title"] else 0.15)
    if criteria.wants_marketing and not matched["role_focus"]:
        title_match = max(0.2, title_match - 0.25)
    company_rel = (
        0.88
        if (matched["usable_company"] and matched["sector"] and matched["company_match"])
        else (0.68 if matched["usable_company"] and (matched["sector"] or matched["company_match"]) else (0.45 if matched["usable_company"] else 0.22))
    )
    loc_match = 0.9 if matched["location"] and candidate.location else (0.72 if matched["location"] else 0.3)
    id_conf = (
        0.92
        if (candidate.linkedin_url and person and matched["usable_company"])
        else (0.76 if candidate.linkedin_url and person else 0.45)
    )
    person_rel = round(min(0.94, max(0.2, (title_match * 0.55 + loc_match * 0.25 + (0.15 if matched["business_associated"] else 0.05)))), 2)
    matched["scores"] = {
        "person_relevance": person_rel,
        "company_relevance": round(company_rel, 2),
        "job_title_match": round(title_match, 2),
        "location_match": round(loc_match, 2),
        "identity_confidence": round(id_conf, 2),
        "email_confidence": 0.0,
    }
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
