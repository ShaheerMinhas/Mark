"""Multi-query generation (Phase 5)."""

from __future__ import annotations

from backend.services.discovery.queries import (
    DEFAULT_MAX_QUERIES,
    build_query_plan,
    expand_company,
    expand_geography,
    prompt_keywords,
    second_page,
)

PROMPT = "Marketing managers in Swedish logistics companies"
FILTERS = {"job_titles": ["Marketing Manager"], "location": "Sweden", "industry": "logistics"}


def plan(prompt=PROMPT, filters=None, **kwargs):
    return build_query_plan(prompt, filters if filters is not None else FILTERS, **kwargs)


def test_plan_varies_the_job_title():
    texts = [spec.text for spec in plan()]

    assert any('"Marketing Manager"' in text for text in texts)
    assert any('"Marketing Director"' in text for text in texts)
    assert any('"Head of Marketing"' in text for text in texts)


def test_every_query_targets_public_profiles():
    assert all(spec.text.startswith("site:linkedin.com/in") for spec in plan())


def test_plan_includes_the_industry_and_geography():
    texts = " ".join(spec.text for spec in plan())

    assert "logistics" in texts
    assert "Sweden" in texts


def test_plan_covers_several_dimensions():
    dimensions = {spec.dimension for spec in plan()}

    assert len(dimensions) >= 2
    assert "title" in dimensions


def test_plan_has_no_duplicate_queries():
    specs = plan()
    hashes = [spec.hash for spec in specs]
    texts = [spec.text for spec in specs]

    assert len(hashes) == len(set(hashes))
    assert len(texts) == len(set(texts))


def test_plan_respects_the_query_budget():
    specs = plan(
        prompt="Heads of growth, VP marketing and brand directors",
        filters={
            "job_titles": ["Head of Growth", "VP Marketing", "Brand Director", "CMO"],
            "location": "Austin, Dallas, Houston, Denver, Miami, Boston",
            "industry": "Technology, Healthcare, Finance",
            "company": "Acme, Globex, Initech, Umbrella, Stark, Wayne, Cyberdyne, Tyrell",
        },
    )

    assert 0 < len(specs) <= DEFAULT_MAX_QUERIES


def test_query_budget_is_configurable():
    assert len(plan(max_queries=3)) == 3


def test_named_companies_get_their_own_queries():
    specs = plan(filters={**FILTERS, "company": "Nordic Freight"})

    assert any(
        spec.dimension == "company" and "Nordic Freight" in spec.text for spec in specs
    )


def test_us_prompts_fan_out_across_regions():
    specs = plan(
        prompt="Marketing managers at solar companies in the United States",
        filters={"job_titles": ["Marketing Manager"], "location": "United States"},
    )

    assert any(spec.dimension == "geography" for spec in specs)


def test_plan_is_empty_for_an_unusable_prompt():
    assert build_query_plan("", {}) == []


def test_prompt_keywords_skip_filler_and_covered_terms():
    keywords = prompt_keywords(PROMPT, exclude=["Marketing Manager", "Sweden", "logistics"])

    lowered = {word.lower() for word in keywords}
    assert "companies" not in lowered
    assert "managers" not in lowered, "plural of a covered title"
    assert "logistics" not in lowered


def test_second_page_keeps_the_query_but_changes_the_hash():
    first = plan()[0]
    follow_up = second_page(first)

    assert follow_up.text == first.text
    assert follow_up.page == 2
    assert follow_up.hash != first.hash


def test_pakistan_expands_to_major_cities():
    cities = expand_geography("Pakistan")
    assert "Lahore" in cities
    assert "Islamabad" in cities
    assert "Faisalabad" in cities


def test_university_company_aliases_expand_query_plan():
    aliases = expand_company("NUST")
    assert any("Sciences and Technology" in alias for alias in aliases)

    specs = plan(
        prompt="Professors at NUST Islamabad Pakistan",
        filters={
            "job_titles": ["Professor", "Associate Professor"],
            "location": "Pakistan",
            "industry": "education",
            "company": "NUST",
        },
    )
    texts = " ".join(spec.text for spec in specs)
    assert "NUST" in texts
    assert "Islamabad" in texts or "Lahore" in texts
    assert any(spec.dimension == "company" for spec in specs)
