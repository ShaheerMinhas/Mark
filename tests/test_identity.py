"""LinkedIn URL normalization and identity keys (Phase 6)."""

from __future__ import annotations

import pytest

from backend.services.identity import (
    identity_key,
    normalize_company,
    normalize_linkedin_url,
)


@pytest.mark.parametrize(
    "raw",
    [
        "https://www.linkedin.com/in/jane-doe",
        "http://www.linkedin.com/in/jane-doe",
        "https://linkedin.com/in/jane-doe/",
        "https://de.linkedin.com/in/jane-doe",
        "https://www.linkedin.com/in/jane-doe?trk=public_profile&utm_source=serp",
        "https://www.linkedin.com/in/jane-doe#experience",
        "www.linkedin.com/in/Jane-Doe",
        "//www.linkedin.com/in/jane-doe",
        "https://www.linkedin.com/in/jane-doe/en",
    ],
)
def test_variants_normalize_to_one_url(raw):
    assert normalize_linkedin_url(raw) == "https://www.linkedin.com/in/jane-doe"


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "not a url",
        "https://example.com/in/jane-doe",
        "https://www.linkedin.com/company/acme",
        "https://www.linkedin.com/in/",
        "https://www.linkedin.com/in/search",
        "https://www.linkedin.com/feed/",
    ],
)
def test_non_person_urls_are_rejected(raw):
    assert normalize_linkedin_url(raw) == ""


def test_distinct_slugs_stay_distinct():
    """LinkedIn disambiguates same-name people by slug suffix; keep that."""
    first = identity_key(linkedin_url="https://www.linkedin.com/in/john-smith-1a2b3c")
    second = identity_key(linkedin_url="https://www.linkedin.com/in/john-smith-9z8y7x")
    assert first != second


def test_profile_url_wins_over_name_fields():
    with_url = identity_key(
        linkedin_url="https://www.linkedin.com/in/jane-doe",
        name="Jane Doe",
        company="Acme",
        title="CMO",
    )
    assert with_url == "li:jane-doe"


def test_fallback_key_requires_name_company_and_title():
    assert identity_key(name="Jane Doe", company="Acme Inc", title="CMO")
    assert identity_key(name="Jane Doe", company="Acme Inc", title="") == ""
    assert identity_key(name="Jane Doe", company="", title="CMO") == ""
    assert identity_key(name="", company="Acme", title="CMO") == ""


def test_same_name_different_company_are_not_merged():
    one = identity_key(name="John Smith", company="Acme Logistics", title="Marketing Manager")
    two = identity_key(name="John Smith", company="Globex Freight", title="Marketing Manager")
    assert one and two and one != two


def test_same_name_different_title_are_not_merged():
    one = identity_key(name="John Smith", company="Acme", title="Marketing Manager")
    two = identity_key(name="John Smith", company="Acme", title="Sales Director")
    assert one != two


def test_fallback_key_tolerates_formatting_and_legal_suffixes():
    one = identity_key(name="Jöhn  Smith", company="Acme Logistics, Inc.", title="Head of Marketing")
    two = identity_key(name="john smith", company="Acme Logistics LLC", title="head of marketing")
    assert one == two


def test_company_normalization_keeps_a_distinguishing_name():
    assert normalize_company("Acme Inc") == "acme"
    # Stripping every token would erase the identity, so the fold is kept instead.
    assert normalize_company("Group Holdings") == "group holdings"
