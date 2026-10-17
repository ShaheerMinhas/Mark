"""Phone extraction and public-web discovery."""

from __future__ import annotations

from backend.services.PhoneModule import extract, public


def test_extract_phones_from_snippet():
    phones = extract.extract_phones("Call Anna at +46 70 123 45 67")
    assert phones == ["+46 70 123 45 67"]


def test_year_runs_are_not_phones():
    assert extract.extract_phones("Active on LinkedIn 2020 2021 2022") == []


def test_snippet_phone_skips_page_fetches(monkeypatch):
    fetches: list[str] = []
    monkeypatch.setattr(public, "ddg_text", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no search")))
    monkeypatch.setattr(public, "_fetch_text", lambda url: fetches.append(url) or "")

    found = public.discover_public_phones(
        "Anna Lind",
        company="Nordic Freight",
        snippet="Reach Anna on +46 70 987 65 43",
    )

    assert found[0]["phone"] == "+46 70 987 65 43"
    assert fetches == []


def test_serp_phone_is_returned(monkeypatch):
    monkeypatch.setattr(
        public,
        "ddg_text",
        lambda q, **kw: [
            {
                "title": "Anna Lind — Nordic Freight",
                "body": "Mobile +46 70 111 22 33",
                "href": "https://nordicfreight.se/team/anna",
            }
        ],
    )
    monkeypatch.setattr(public, "_fetch_text", lambda url: "")

    found = public.discover_public_phones("Anna Lind", company="Nordic Freight")

    assert found[0]["phone"] == "+46 70 111 22 33"


def test_directory_sites_are_not_fetched_for_phones(monkeypatch):
    fetches: list[str] = []
    monkeypatch.setattr(
        public,
        "ddg_text",
        lambda q, **kw: [
            {
                "title": "Anna Lind phone",
                "body": "Find her number",
                "href": "https://www.zoominfo.com/p/Anna-Lind",
            }
        ],
    )
    monkeypatch.setattr(public, "allowed", lambda url: True)
    monkeypatch.setattr(public, "_fetch_text", lambda url: fetches.append(url) or "")

    public.discover_public_phones("Anna Lind", company="Nordic Freight")

    assert fetches == []
