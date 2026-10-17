"""Public-web email lookup: harvest SERP snippets before fetching pages."""

from __future__ import annotations

from backend.services.EmailModule import public


def test_an_email_in_search_snippets_skips_page_fetches(monkeypatch):
    fetches: list[str] = []
    monkeypatch.setattr(
        public,
        "ddg_text",
        lambda q, **kw: [
            {
                "title": "Anna Lind | Nordic Freight",
                "body": "Write to anna.lind@nordicfreight.se",
                "href": "https://nordicfreight.se/team/anna",
            }
        ],
    )
    monkeypatch.setattr(public, "_fetch_text", lambda url: fetches.append(url) or "")

    found = public.discover_public_emails("Anna Lind", company="Nordic Freight AB")

    assert found[0]["email"] == "anna.lind@nordicfreight.se"
    assert fetches == []


def test_pages_are_fetched_only_when_snippets_have_no_email(monkeypatch):
    fetches: list[str] = []
    monkeypatch.setattr(
        public,
        "ddg_text",
        lambda q, **kw: [
            {
                "title": "Team — Nordic Freight",
                "body": "Meet our marketing managers.",
                "href": "https://nordicfreight.se/team",
            }
        ],
    )
    monkeypatch.setattr(public, "allowed", lambda url: True)
    monkeypatch.setattr(
        public,
        "_fetch_text",
        lambda url: fetches.append(url)
        or "Contact Anna Lind at anna.lind@nordicfreight.se",
    )

    found = public.discover_public_emails("Anna Lind", company="Nordic Freight AB")

    assert found[0]["email"] == "anna.lind@nordicfreight.se"
    assert fetches


def test_directory_sites_are_not_fetched(monkeypatch):
    fetches: list[str] = []
    monkeypatch.setattr(
        public,
        "ddg_text",
        lambda q, **kw: [
            {
                "title": "Anna Lind email",
                "body": "Find contact details.",
                "href": "https://www.zoominfo.com/p/Anna-Lind",
            }
        ],
    )
    monkeypatch.setattr(public, "allowed", lambda url: True)
    monkeypatch.setattr(public, "_fetch_text", lambda url: fetches.append(url) or "")

    public.discover_public_emails("Anna Lind", company="Nordic Freight AB")

    assert fetches == []
