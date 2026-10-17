"""Query-plan fan-out.

A search naming one industry and one country used to generate title variations
only: the industry and geography dimensions both slice from index 1, so with a
single value each they produced nothing and the pool stayed tiny.
"""

from __future__ import annotations

from collections import Counter

from backend.services.discovery.queries import (
    COUNTRY_CITIES,
    DEFAULT_MAX_QUERIES,
    build_query_plan,
    expand_geography,
    expand_industry,
)
from backend.services.discovery.runner import (
    MAX_CANDIDATE_POOL,
    MIN_CANDIDATE_POOL,
    candidate_pool_target,
)

SWEDISH_LOGISTICS = {
    "prompt": "Marketing managers in Swedish logistics companies",
    "filters": {
        "job_titles": ["Marketing Manager", "Head of Marketing"],
        "location": "Sweden",
        "industry": "logistics",
        "active_only": False,
    },
}


def plan(**overrides):
    data = {**SWEDISH_LOGISTICS, **overrides}
    return build_query_plan(data["prompt"], data["filters"])


class TestIndustryExpansion:
    def test_logistics_expands_to_related_sectors(self):
        terms = {t.lower() for t in expand_industry("logistics")}
        assert {"freight", "shipping", "supply chain"} <= terms

    def test_matching_is_case_insensitive(self):
        assert expand_industry("LOGISTICS") == expand_industry("logistics")

    def test_a_phrase_still_matches_on_a_contained_word(self):
        assert "freight" in [t.lower() for t in expand_industry("logistics companies")]

    def test_solar_energy_prefers_solar_vocabulary_over_generic_energy(self):
        terms = {t.lower() for t in expand_industry("Solar Energy")}
        assert "photovoltaic" in terms or "pv" in terms
        assert "solar power" in terms or "solar panel" in terms
        # Must not collapse to the generic energy bag (oil and gas).
        assert "oil and gas" not in terms

    def test_an_unknown_industry_expands_to_nothing(self):
        assert expand_industry("artisanal basket weaving") == []

    def test_blank_industry_is_safe(self):
        assert expand_industry("") == []

    def test_expansions_never_include_the_original(self):
        for term in ("logistics", "software", "healthcare"):
            assert term not in [t.lower() for t in expand_industry(term)]


class TestGeographyExpansion:
    def test_a_country_expands_to_its_cities(self):
        cities = expand_geography("Sweden")
        assert "Stockholm" in cities
        assert "Gothenburg" in cities

    def test_a_demonym_in_the_prompt_resolves_the_country(self):
        cities = expand_geography("", "Marketing managers in Swedish logistics")
        assert "Stockholm" in cities

    def test_united_states_of_america_expands_to_states_and_cities(self):
        """The full country name must not die as a single 'United States' token."""
        cities = expand_geography("United States of America")
        assert "California" in cities
        assert "Texas" in cities
        assert "Austin" in cities or "New York" in cities
        assert len(cities) >= 15

    def test_usa_alias_expands_the_same_way(self):
        assert "California" in expand_geography("USA")
        assert "California" in expand_geography("United States")

    def test_an_unknown_place_expands_to_nothing(self):
        assert expand_geography("Atlantis") == []

    def test_a_city_the_user_asked_for_is_not_repeated(self):
        cities = expand_geography("Stockholm", "Swedish logistics companies")
        assert "Stockholm" not in cities
        assert "Gothenburg" in cities

    def test_every_country_entry_has_at_least_one_city(self):
        assert all(cities for cities in COUNTRY_CITIES.values())


class TestPlanFanOut:
    def test_a_single_industry_and_country_still_fans_out(self):
        specs = plan()
        dimensions = Counter(s.dimension for s in specs)
        assert dimensions["industry_term"] > 0, "industry dimension produced nothing"
        assert dimensions["geography"] > 0, "geography dimension produced nothing"

    def test_the_plan_is_several_times_larger_than_title_variants_alone(self):
        specs = plan()
        title_only = [s for s in specs if s.dimension in ("title", "title_synonym")]
        assert len(specs) >= 3 * len(title_only)

    def test_the_queries_are_genuinely_distinct(self):
        specs = plan()
        assert len({s.text for s in specs}) == len(specs)
        assert len({s.hash for s in specs}) == len(specs)

    def test_the_sector_vocabulary_actually_appears_in_queries(self):
        text = " ".join(s.text.lower() for s in plan())
        for term in ("freight", "shipping", "supply chain"):
            assert term in text

    def test_the_cities_actually_appear_in_queries(self):
        text = " ".join(s.text for s in plan())
        for city in ("Stockholm", "Gothenburg", "Malmö"):
            assert city in text

    def test_the_plan_respects_its_ceiling(self):
        assert len(plan()) <= DEFAULT_MAX_QUERIES

    def test_an_explicit_lower_ceiling_is_honoured(self):
        specs = build_query_plan(
            SWEDISH_LOGISTICS["prompt"], SWEDISH_LOGISTICS["filters"], max_queries=12
        )
        assert len(specs) == 12

    def test_the_users_own_terms_are_still_tried_first(self):
        """Expansion must not push the literal request down the queue."""
        specs = plan()
        assert specs[0].dimension == "title"
        assert "logistics" in specs[0].text

    def test_a_vague_prompt_does_not_explode_the_plan(self):
        specs = build_query_plan("find me some people", {})
        assert len(specs) <= DEFAULT_MAX_QUERIES

    def test_an_industry_only_search_fans_out_across_global_markets(self):
        """Without a location, we still need hundreds of candidates — not ~20 queries."""
        specs = build_query_plan(
            "Marketing managers in Solar Energy companies",
            {
                "job_titles": ["Marketing Manager", "Head of Marketing"],
                "industry": "Solar Energy",
                "active_only": False,
            },
        )
        dimensions = Counter(s.dimension for s in specs)
        assert len(specs) >= 80, len(specs)
        assert dimensions["geography"] > 20, dimensions
        text = " ".join(s.text.lower() for s in specs)
        assert "photovoltaic" in text or "solar power" in text
        assert "united states" in text or "germany" in text or "california" in text

    def test_united_states_of_america_fans_out_across_us_markets(self):
        specs = build_query_plan(
            "Marketing managers in Solar Energy companies",
            {
                "job_titles": ["Marketing Manager", "Head of Marketing"],
                "location": "United States of America",
                "industry": "Solar Energy",
                "active_only": False,
            },
        )
        text = " ".join(s.text for s in specs)
        assert "California" in text
        assert "Texas" in text
        assert len(specs) >= 100, len(specs)
        assert Counter(s.dimension for s in specs)["geography"] > 40


class TestCandidatePoolTarget:
    def test_the_pool_is_no_longer_capped_at_max_results(self):
        """``max_results`` is a display preference, not a search width."""
        assert candidate_pool_target({"max_results": 100}) > 100

    def test_the_pool_has_a_floor(self):
        assert candidate_pool_target({"max_results": 10}) == MIN_CANDIDATE_POOL

    def test_a_large_request_scales_the_pool(self):
        # Default floor is already sized for ~1000 leads; a bigger ask must grow it.
        assert candidate_pool_target({"max_results": 1500}) > MIN_CANDIDATE_POOL

    def test_the_pool_has_a_ceiling(self):
        assert candidate_pool_target({"max_results": 100_000}) == MAX_CANDIDATE_POOL

    def test_an_explicit_override_is_respected(self):
        assert candidate_pool_target({"max_candidates": 250}) == 250

    def test_missing_filters_default_to_a_thousand_lead_pool(self):
        # Empty filters use the 1000-lead default, which targets 5× candidates.
        assert candidate_pool_target({}) == min(MAX_CANDIDATE_POOL, 1000 * 5)
