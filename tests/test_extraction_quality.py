"""Regression tests for candidate extraction and the company gate.

These cover leaks found by running a real search: non-ASCII names were truncated,
the employer was never read from the snippet body, and a failed company parse
silently discarded ~40% of the pool.
"""

from __future__ import annotations

from backend.lead_finder import _clean_person_name, _parse_linkedin_title
from backend.services.discovery.extract import extract_candidate, extract_candidates


def hit(title: str, body: str = "", slug: str = "person-1") -> dict:
    return {
        "title": title,
        "body": body,
        "href": f"https://www.linkedin.com/in/{slug}",
    }


class TestNonAsciiNames:
    """An ASCII-only name class truncated "Stenesjö" and leaked "ö" into the title."""

    def test_swedish_name_survives_intact(self):
        name, role, _company = _parse_linkedin_title(
            "Lina Stenesjö - Marketing Operations Manager"
        )
        assert name == "Lina Stenesjö"
        assert role == "Marketing Operations Manager"

    def test_german_umlauts(self):
        name, role, company = _parse_linkedin_title(
            "Jürgen Müller - Marketing Manager at Deutsche Bahn"
        )
        assert name == "Jürgen Müller"
        assert role == "Marketing Manager"
        assert company == "Deutsche Bahn"

    def test_leading_swedish_uppercase(self):
        name, role, company = _parse_linkedin_title("Åsa Öberg - CMO at Nordic Freight")
        assert name == "Åsa Öberg"
        assert role == "CMO"
        assert company == "Nordic Freight"

    def test_french_accents(self):
        assert _clean_person_name("François Lefèvre") == "François Lefèvre"

    def test_nordic_slashed_o(self):
        assert _clean_person_name("Bjørn Dæhlie") == "Bjørn Dæhlie"

    def test_ascii_names_are_unchanged(self):
        assert _clean_person_name("John Smith") == "John Smith"

    def test_still_rejects_single_tokens(self):
        assert _clean_person_name("Åsa") == ""

    def test_still_rejects_job_titles_as_names(self):
        assert _clean_person_name("Marketing Manager") == ""


class TestCompanyFallback:
    """The SERP title often has no employer; the snippet body does."""

    def test_company_read_from_snippet_experience(self):
        candidate = extract_candidate(
            hit(
                "Caner Tek - Marketing Manager | LinkedIn",
                body="Experience: Scania Logistics · Marketing Manager. Stockholm, Sweden.",
            )
        )
        assert candidate is not None
        assert candidate.title == "Marketing Manager"
        assert candidate.company == "Scania Logistics"

    def test_company_read_from_role_at_company_in_body(self):
        candidate = extract_candidate(
            hit(
                "Evgeny Gorelov - B2B Marketing Manager",
                body="Marketing Manager at Nordic Freight AB. Stockholm.",
            )
        )
        assert candidate is not None
        assert candidate.company == "Nordic Freight AB"

    def test_title_company_still_wins_over_snippet(self):
        candidate = extract_candidate(
            hit(
                "Anna Lind - Marketing Manager at Baltic Logistics AB",
                body="Experience: Some Other Company.",
            )
        )
        assert candidate is not None
        assert candidate.company == "Baltic Logistics AB"

    def test_linkedin_experience_format_without_at(self):
        """LinkedIn renders "Role. Employer. Mar 2024 - Present" with no "at"."""
        candidate = extract_candidate(
            hit(
                "Evgeny Gorelov - B2B Marketing Manager",
                body="Marketing Manager. Bergen Logistics. Apr 2025 - Present 1 year.",
            )
        )
        assert candidate is not None
        assert candidate.company == "Bergen Logistics"

    def test_experience_format_single_word_employer(self):
        candidate = extract_candidate(
            hit(
                "Meg Cook - Marketing",
                body="Field Marketing Manager. Sodexo. 2022 - 2023 1 year.",
            )
        )
        assert candidate is not None
        assert candidate.company == "Sodexo"

    def test_no_company_anywhere_is_still_a_candidate(self):
        """Extraction must not filter; a missing employer is not disqualifying."""
        candidate = extract_candidate(
            hit("Meg Cook - Marketing Manager", body="Stockholm, Sweden.")
        )
        assert candidate is not None
        assert candidate.name == "Meg Cook"
        assert candidate.company == ""

    def test_non_person_urls_are_skipped(self):
        assert extract_candidate({"title": "Acme Inc", "href": "https://example.com"}) is None

    def test_extract_candidates_keeps_every_parseable_person(self):
        hits = [
            hit("Anna Lind - Marketing Manager at Nordic AB", slug="anna"),
            hit("Erik Ohlsson - CMO", body="Experience: Baltic AB.", slug="erik"),
            {"title": "Not a profile", "href": "https://example.com/x"},
        ]
        assert len(extract_candidates(hits)) == 2


class TestCompanyGarbageRejection:
    """Live results contained companies like "and Commercial" and "EMEA hos"."""

    def test_leading_conjunction_is_not_a_company(self):
        from backend.lead_finder import _clean_company

        assert _clean_company("and Commercial", name="x") == ""

    def test_trailing_preposition_is_not_a_company(self):
        from backend.lead_finder import _clean_company

        assert _clean_company("EMEA hos", name="x") == ""

    def test_leading_preposition_is_not_a_company(self):
        from backend.lead_finder import _clean_company

        assert _clean_company("of Marketing", name="x") == ""

    def test_run_together_linkedin_text_is_not_a_company(self):
        """SERP snippets concatenate profiles: "LinkedInKarolin Henningsso"."""
        from backend.lead_finder import _clean_company

        assert _clean_company("LinkedInKarolin Henningsso", name="x") == ""

    def test_bare_country_is_not_a_company(self):
        from backend.lead_finder import _clean_company

        for place in ("Sweden", "Europe", "EMEA", "Nordics", "United Kingdom"):
            assert _clean_company(place, name="x") == "", place

    def test_real_companies_still_pass(self):
        from backend.lead_finder import _clean_company

        for good in (
            "Nordic Freight AB",
            "H&M Group",
            "Scania",
            "Volvo Group",
            "Swedish Match",
            "Paulig Group",
            "HARIBO",
            # A place name inside a longer company name must survive.
            "Europe RPET Certified LLC",
        ):
            assert _clean_company(good, name="x") == good, good

    def test_sentence_tail_is_trimmed(self):
        from backend.lead_finder import _trim_company_tail

        assert _trim_company_tail("Nordic Freight AB. Stockholm.") == "Nordic Freight AB"

    def test_abbreviations_are_not_split(self):
        from backend.lead_finder import _trim_company_tail

        assert _trim_company_tail("Acme Inc.") == "Acme Inc"


class TestCompanyIsNotAHardGate:
    """A relevant person with an unparsed employer must survive qualification."""

    def _run(self, db, user, candidates, filters):
        from backend.models import SearchCandidate, SearchJob, SearchJobStatus
        from backend.services.discovery.qualify import qualify_candidates

        job = SearchJob(
            name="gate",
            prompt="Marketing managers in Swedish logistics companies",
            filters=filters,
            owner_id=user.id,
            status=SearchJobStatus.PENDING,
        )
        db.add(job)
        db.commit()
        db.refresh(job)
        for index, item in enumerate(candidates):
            name, title, company = item[0], item[1], item[2]
            snippet = (
                item[3]
                if len(item) > 3
                else f"{title} at {company or 'unknown'}. Logistics. Stockholm, Sweden."
            )
            location = item[4] if len(item) > 4 else "Stockholm, Sweden"
            db.add(
                SearchCandidate(
                    search_job_id=job.id,
                    identity_key=f"li:{name.lower().replace(' ', '-')}-{index}",
                    name=name,
                    title=title,
                    company=company,
                    location=location,
                    linkedin_url=f"https://www.linkedin.com/in/p{index}",
                    raw_snippet=snippet,
                    status="discovered",
                )
            )
        db.commit()
        qualify_candidates(
            db, search_job_id=job.id, prompt=job.prompt, filters=dict(job.filters)
        )
        return {
            c.name: c
            for c in db.query(SearchCandidate).filter(SearchCandidate.search_job_id == job.id)
        }

    FILTERS = {
        "job_titles": ["Marketing Manager"],
        "location": "Sweden",
        "industry": "logistics",
        "active_only": False,
    }

    def test_missing_company_no_longer_rejects_outright(self, db, user):
        rows = self._run(
            db,
            user,
            [("Caner Tek", "Marketing Manager", "")],
            self.FILTERS,
        )
        row = rows["Caner Tek"]
        assert row.rejection_reason != "no_usable_company"
        assert row.status == "qualified"

    def test_missing_company_scores_below_a_known_employer(self, db, user):
        rows = self._run(
            db,
            user,
            [
                ("Caner Tek", "Marketing Manager", ""),
                ("Anna Lind", "Marketing Manager", "Nordic Logistics AB"),
            ],
            self.FILTERS,
        )
        assert rows["Caner Tek"].relevance_score < rows["Anna Lind"].relevance_score

    def test_the_api_default_business_only_does_not_gate(self, db, user):
        """``business_only`` is written into every search by default, so it must
        not be read as the user opting into a hard company requirement."""
        rows = self._run(
            db,
            user,
            [("Caner Tek", "Marketing Manager", "")],
            {**self.FILTERS, "business_only": True},
        )
        assert rows["Caner Tek"].status == "qualified"

    def test_opting_in_to_require_company_hard_gates(self, db, user):
        rows = self._run(
            db,
            user,
            [("Caner Tek", "Marketing Manager", "")],
            {**self.FILTERS, "require_company": True},
        )
        assert rows["Caner Tek"].status == "rejected"
        assert rows["Caner Tek"].rejection_reason == "no_usable_company"

    def test_a_wrong_role_is_still_rejected(self, db, user):
        """Widening the company gate must not let irrelevant roles through."""
        rows = self._run(
            db,
            user,
            [("Sven Berg", "Truck Driver", "Nordic Logistics AB")],
            self.FILTERS,
        )
        assert rows["Sven Berg"].status == "rejected"

    def test_a_title_match_outside_the_sector_is_kept_and_ranked_lower(self, db, user):
        """Wrong city or sector used to stack past the cut-off and hide the person."""
        rows = self._run(
            db,
            user,
            [
                ("Anna Lind", "Marketing Manager", "Nordic Logistics AB"),
                (
                    "Jill Malone",
                    "Marketing Manager",
                    "Obvious Ventures",
                    "Marketing Manager at Obvious Ventures. San Francisco, United States.",
                    "San Francisco, United States",
                ),
            ],
            self.FILTERS,
        )
        assert rows["Jill Malone"].status == "qualified"
        assert rows["Anna Lind"].status == "qualified"
        assert rows["Jill Malone"].relevance_score < rows["Anna Lind"].relevance_score
