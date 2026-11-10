"""Contact enrichment and the pinned interactive search. No live or paid network."""

from __future__ import annotations

from datetime import datetime

from backend.models import ContactEnrichmentJob, DiscoveryRun, DiscoveryTarget, GlobalPerson
from backend.services.discovery_engine.contacts import (
    enrich_person,
    process_contact_jobs,
    queue_contact_jobs,
)
from backend.services.discovery_engine.enrich import clear_adapters, run_email_stage, use_adapter
from backend.services.discovery_engine.settings import ensure_settings
from backend.services.discovery_engine.spend import cache_health, seed_providers, set_budget, set_credential, set_price
from backend.services.discovery_engine.ssrf import SsrfError, pinned_opener
from backend.services.discovery_engine.suppression import add_suppression
from backend.services.global_cache.match import CacheQuery, match_people
from backend.services.global_cache.upsert import PublicProspect, upsert_public_prospect
from backend.services.search_engines.engines import DdgsEngine


PAGE = b"<p>Ada Example ada.example@example.com +1 202 555 0143 info@example.com</p>"


def _person(db, **overrides):
    body = dict(
        full_name="Ada Example",
        title="CTO",
        company_name="Example Co",
        domain="example.com",
        country_code="SE",
        profile_url="https://www.linkedin.com/in/ada-example",
    )
    body.update(overrides)
    return upsert_public_prospect(db, PublicProspect(**body))


def _fetch(pages):
    calls = []

    def fetch(url: str) -> bytes:
        calls.append(url)
        if url.endswith("/robots.txt"):
            return pages["robots"]
        return pages.get(url, b"")

    fetch.calls = calls
    return fetch


def test_published_company_contact_is_found_and_not_verified(db):
    person = _person(db)
    fetch = _fetch({"robots": b"User-agent: *\nAllow: /\n", "https://example.com/contact": PAGE})
    detail = enrich_person(db, person, fetch=fetch, mx_lookup=lambda _domain: [])
    db.commit()
    db.refresh(person)
    assert person.email == "ada.example@example.com"
    assert person.email_status == "found"
    assert person.email_confidence == "published"
    assert person.email_status != "verified"
    assert "Not a provider verification" in (person.contact_note or detail)
    assert person.phone
    assert "info@example.com" != person.email
    assert any(url.endswith("/contact") for url in fetch.calls)
    assert person.quality_tier != "verified"


def test_robots_disallow_and_fetch_refusal_store_nothing(db):
    blocked = _person(db, full_name="Bea Example", profile_url="https://www.linkedin.com/in/bea-example")
    fetch = _fetch({"robots": b"User-agent: *\nDisallow: /\n"})
    enrich_person(db, blocked, fetch=fetch, mx_lookup=lambda _domain: [])
    db.commit()
    db.refresh(blocked)
    assert blocked.email is None
    assert not any(url.endswith("/contact") for url in fetch.calls)

    refused = _person(db, full_name="Cio Example", profile_url="https://www.linkedin.com/in/cio-example", domain="cio.example")

    def boom(_url: str) -> bytes:
        raise SsrfError("resolved address is not public")

    enrich_person(db, refused, fetch=boom, mx_lookup=lambda _domain: [])
    db.commit()
    db.refresh(refused)
    assert refused.email is None
    assert refused.phone is None


def test_pattern_requires_mx_and_stays_inferred(db):
    person = _person(db)
    empty = _fetch({"robots": b"User-agent: *\nAllow: /\n"})
    enrich_person(db, person, fetch=empty, mx_lookup=lambda _domain: [])
    db.commit()
    db.refresh(person)
    assert person.email is None
    enrich_person(db, person, fetch=empty, mx_lookup=lambda _domain: ["mx.example.com"])
    db.commit()
    db.refresh(person)
    assert person.email == "ada.example@example.com"
    assert person.email_status == "inferred"
    assert person.email_confidence == "mx-pattern"
    assert "not verified" in (person.contact_note or "")
    assert person.quality_tier != "verified"


def test_suppression_and_name_only_policy(db):
    person = _person(db)
    add_suppression(db, kind="email", value="ada.example@example.com", reason="deleted", user_id=None)
    fetch = _fetch({"robots": b"User-agent: *\nAllow: /\n", "https://example.com/contact": PAGE})
    enrich_person(db, person, fetch=fetch, mx_lookup=lambda _domain: ["mx.example.com"])
    db.commit()
    db.refresh(person)
    assert person.email != "ada.example@example.com"

    named = _person(db, full_name="Nia Example", profile_url="https://www.linkedin.com/in/nia-example", domain="nia.example")
    settings = ensure_settings(db)
    settings.store_name_only = False
    settings.serve_name_only = False
    db.commit()
    run = DiscoveryRun(target_id=_target(db).id, idempotency_key="contact-name", status="queued", scheduled_for=datetime.utcnow())
    db.add(run)
    db.commit()
    queue_contact_jobs(db, [named.id], run.id)
    process_contact_jobs(db, run.id, fetch=lambda _url: b"", mx_lookup=lambda _domain: [])
    db.refresh(named)
    assert named.archived_at is not None
    verified = _person(db, full_name="Vera Example", profile_url="https://www.linkedin.com/in/vera-example", domain="vera.example", email="vera@vera.example", email_status="verified")
    plain = _person(db, full_name="Plain Example", profile_url="https://www.linkedin.com/in/plain-example", domain="plain.example")
    _total, rows = match_people(db, CacheQuery(country_codes=["SE"], serve_name_only=False, page_size=10))
    names = [row.full_name for row in rows]
    assert "Vera Example" in names
    assert "Plain Example" not in names
    assert "Nia Example" not in names
    settings.serve_name_only = True
    db.commit()
    _total, ordered = match_people(db, CacheQuery(country_codes=["SE"], serve_name_only=True, page_size=10))
    assert ordered[0].full_name == "Vera Example"


def test_paid_adapter_stays_gated_and_cache_counts_coverage(db):
    person = _person(db, full_name="Paid Example", profile_url="https://www.linkedin.com/in/paid-example", domain="paid.example")
    target = _target(db)
    run = DiscoveryRun(target_id=target.id, idempotency_key="contact-paid", status="queued", scheduled_for=datetime.utcnow())
    db.add(run)
    db.commit()
    queue_contact_jobs(db, [person.id], run.id)
    calls = []

    def adapter(_person):
        calls.append(_person.id)
        from backend.services.discovery_engine.enrich import AdapterResult

        return AdapterResult(email_status="found", email="paid.example@paid.example", http_status=200)

    use_adapter("hunter", "email_find", adapter)
    try:
        status, detail = run_email_stage(db, run, target, ensure_settings(db), 1)
        assert status == "skipped"
        assert "Paid providers are disabled" in detail
        assert calls == []
        seed_providers(db)
        set_credential(db, provider_name="hunter", api_key="platform-fixture-key")
        set_price(db, provider_name="hunter", operation="email_find", currency="USD", max_cost_minor=100)
        for scope, name, period in (
            ("global", "", "day"),
            ("global", "", "month"),
            ("provider", "hunter", "day"),
            ("provider", "hunter", "month"),
        ):
            set_budget(db, scope=scope, provider_name=name, period=period, currency="USD", limit_minor=500)
        from backend.models import DiscoveryProvider

        provider = db.get(DiscoveryProvider, "hunter")
        provider.enabled = True
        provider.rate_per_hour = 5
        settings = ensure_settings(db)
        settings.paid_providers_enabled = True
        settings.pause_email = False
        settings.pause_verify = True
        db.commit()
        job = db.query(ContactEnrichmentJob).filter(ContactEnrichmentJob.person_id == person.id).one()
        job.status = "queued"
        db.commit()
        _status, detail = run_email_stage(db, run, target, settings, 1)
        assert calls == [person.id]
        assert "not a phone number" in detail
        db.refresh(person)
        assert person.email == "paid.example@paid.example"
        assert person.email_status == "found"
        assert person.email_confidence == "provider"
        assert person.phone is None
    finally:
        clear_adapters()
    health = cache_health(db)
    assert "with_email" in health
    assert "with_phone" in health
    assert "name_only" in health
    assert health["with_email"] >= 1


def test_interactive_search_uses_the_pinned_fetch(monkeypatch):
    monkeypatch.delenv("MARK_DISABLE_CONTACT_NETWORK", raising=False)
    calls = []

    def fake_fetch(url, *, opener, allowed_hosts, **_kwargs):
        calls.append((url, opener, set(allowed_hosts)))
        if url.endswith("/robots.txt"):
            return b"User-agent: *\nAllow: /html\nAllow: /search\n"
        return (
            b'<a class="result__a" href="https://duckduckgo.com/l/?uddg=https%3A%2F%2Fwww.linkedin.com%2Fin%2Fada">'
            b"Ada Example</a>"
        )

    monkeypatch.setattr("backend.services.discovery_engine.ssrf.safe_fetch", fake_fetch)

    class Client:
        def text(self, *_args, **_kwargs):
            raise AssertionError("unpinned client")

    Client.__module__ = "ddgs.fake"
    hits = DdgsEngine("duckduckgo", min_interval=0)._query(Client(), "cto", 5, 1)
    assert hits[0]["href"] == "https://www.linkedin.com/in/ada"
    assert calls
    assert all(opener is pinned_opener for _url, opener, _hosts in calls)
    assert {"html.duckduckgo.com"} in [hosts for _url, _opener, hosts in calls]

    def refused(*_args, **_kwargs):
        raise SsrfError("resolved address is not public")

    monkeypatch.setattr("backend.services.discovery_engine.ssrf.safe_fetch", refused)
    assert DdgsEngine("duckduckgo", min_interval=0)._query(Client(), "cto", 5, 1) == []


def _target(db):
    row = DiscoveryTarget(name="Contact target", scheduling_status="paused", countries=["SE"], titles=["CTO"])
    db.add(row)
    db.commit()
    db.refresh(row)
    return row
