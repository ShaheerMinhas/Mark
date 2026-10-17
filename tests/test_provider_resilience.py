"""Provider timeouts, 429s and the circuit breaker (Phase 11)."""

from __future__ import annotations

import pytest

from backend.models import EnrichmentStatus, LeadRecord, SearchJob, SearchJobStatus
from backend.services import provider_health
from backend.services.EmailModule.intelligence import EmailEnrichmentService
from backend.services.EmailModule.providers.base import ProviderFind, ProviderVerify
from backend.services.EmailModule.types import PersonInput


class StubProvider:
    """Records every call so we can prove a tripped provider is skipped."""

    def __init__(self, name: str, *, find: ProviderFind | None = None, raises=None) -> None:
        self.name = name
        self._find = find or ProviderFind(email=f"hit@{name}.com")
        self._raises = raises
        self.find_calls = 0
        self.verify_calls = 0

    def available(self) -> bool:
        return True

    def find_email(self, *, name, first, last, domain, company) -> ProviderFind:
        self.find_calls += 1
        if self._raises:
            raise self._raises
        return self._find

    def verify_email(self, email: str) -> ProviderVerify:
        self.verify_calls += 1
        return ProviderVerify(status="valid")


def person() -> PersonInput:
    return PersonInput(
        name="Anna Lind",
        job_title="Marketing Manager",
        company_name="Nordic Freight AB",
        company_domain="nordicfreight.se",
    )


# --- Circuit breaker ------------------------------------------------------


def test_a_429_trips_the_circuit_immediately():
    provider_health.record_failure("hunter", http_status=429)

    assert provider_health.is_open("hunter") is True
    assert provider_health.retry_after("hunter") > 0


def test_server_errors_need_repeated_failures_to_trip():
    provider_health.record_failure("tomba", http_status=503)
    assert provider_health.is_open("tomba") is False

    provider_health.record_failure("tomba", http_status=503)
    provider_health.record_failure("tomba", http_status=503)
    assert provider_health.is_open("tomba") is True


def test_repeat_offences_back_off_exponentially():
    provider_health.record_failure("hunter", http_status=429)
    first = provider_health.retry_after("hunter")
    provider_health.record_failure("hunter", http_status=429)
    second = provider_health.retry_after("hunter")

    assert second > first


def test_success_closes_the_circuit():
    provider_health.record_failure("hunter", http_status=429)
    provider_health.record_success("hunter")

    assert provider_health.is_open("hunter") is False
    assert "hunter" not in provider_health.open_providers()


@pytest.mark.parametrize(
    "exc",
    [
        RuntimeError("HTTP 429 Too Many Requests"),
        RuntimeError("rate limit exceeded"),
        type("RatelimitException", (Exception,), {})("slow down"),
    ],
)
def test_rate_limit_detection_from_exceptions(exc):
    assert provider_health.looks_rate_limited(exc) is True


def test_other_exceptions_are_not_mistaken_for_rate_limits():
    assert provider_health.looks_rate_limited(ValueError("bad domain")) is False


# --- Waterfall behaviour --------------------------------------------------


def test_a_429_provider_is_skipped_and_the_next_one_is_used(db):
    limited = StubProvider("limited", find=ProviderFind(http_status=429, detail="rate_limited"))
    healthy = StubProvider("healthy", find=ProviderFind(email="anna.lind@nordicfreight.se"))
    service = EmailEnrichmentService(providers=[limited, healthy])

    result = service.enrich(person(), use_providers=True, use_public_web=False, verify=False)

    assert result.email == "anna.lind@nordicfreight.se"
    assert result.source == "healthy"
    assert provider_health.is_open("limited") is True


def test_a_tripped_provider_is_not_called_again(db):
    limited = StubProvider("limited", find=ProviderFind(http_status=429, detail="rate_limited"))
    healthy = StubProvider("healthy", find=ProviderFind(email="anna.lind@nordicfreight.se"))
    service = EmailEnrichmentService(providers=[limited, healthy])

    service.enrich(person(), use_providers=True, use_public_web=False, verify=False)
    calls_after_trip = limited.find_calls
    service.enrich(
        PersonInput(name="Erik Ohlsson", company_domain="baltic.se"),
        use_providers=True,
        use_public_web=False,
        verify=False,
        force=True,
    )

    assert limited.find_calls == calls_after_trip, "a failing provider must not be hammered"
    assert healthy.find_calls == 2


def test_a_clean_no_match_does_not_trip_the_circuit(db):
    empty = StubProvider("empty", find=ProviderFind(email=None))
    service = EmailEnrichmentService(providers=[empty])

    service.enrich(person(), use_providers=True, use_public_web=False, verify=False)

    assert provider_health.is_open("empty") is False


def test_server_error_from_a_provider_is_recorded(db):
    broken = StubProvider("broken", find=ProviderFind(http_status=502, detail="bad gateway"))
    service = EmailEnrichmentService(providers=[broken])

    for _ in range(3):
        service.enrich(person(), use_providers=True, use_public_web=False, verify=False, force=True)

    assert provider_health.is_open("broken") is True


# --- Enrichment stage never drops a lead ---------------------------------


@pytest.fixture
def enriching_job(db, user):
    job = SearchJob(
        name="Resilience",
        prompt="Marketing managers in Swedish logistics companies",
        filters={},
        owner_id=user.id,
        status=SearchJobStatus.COMPLETED,
        qualified_leads=1,
        trace_id="trace-resilience",
    )
    db.add(job)
    db.commit()
    db.add(
        LeadRecord(
            search_job_id=job.id,
            identity_key="li:anna-lind",
            name="Anna Lind",
            company_name="Nordic Freight AB",
            work_designation="Marketing Manager",
            enrichment_status=EnrichmentStatus.PENDING.value,
        )
    )
    db.commit()
    db.refresh(job)
    return job


def test_a_provider_timeout_leaves_the_lead_in_place(db, enriching_job, monkeypatch):
    from backend.services.discovery import enrichment

    monkeypatch.setattr(enrichment, "_enrich_company", lambda *a, **k: None)
    monkeypatch.setattr(enrichment, "discover_public_phones", lambda *a, **k: [])
    monkeypatch.setattr(
        EmailEnrichmentService,
        "enrich",
        lambda *a, **k: (_ for _ in ()).throw(TimeoutError("read timeout")),
    )

    enrichment.run_enrichment(db, enriching_job)

    lead = db.query(LeadRecord).filter(LeadRecord.search_job_id == enriching_job.id).one()
    assert lead.enrichment_status == EnrichmentStatus.FAILED.value
    assert lead.enrichment_error == "TimeoutError"
    assert lead.name == "Anna Lind", "the lead itself must survive"


def test_a_429_during_enrichment_schedules_a_retry(db, enriching_job, monkeypatch):
    from backend.services.discovery import enrichment

    monkeypatch.setattr(enrichment, "_enrich_company", lambda *a, **k: None)
    monkeypatch.setattr(enrichment, "discover_public_phones", lambda *a, **k: [])
    monkeypatch.setattr(
        EmailEnrichmentService,
        "enrich",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("HTTP 429 rate limit")),
    )

    enrichment.run_enrichment(db, enriching_job)

    lead = db.query(LeadRecord).filter(LeadRecord.search_job_id == enriching_job.id).one()
    assert lead.enrichment_status == EnrichmentStatus.RATE_LIMITED.value
    assert lead.enrichment_locked_until is not None, "backoff before retrying"


def test_enrichment_failure_does_not_undo_the_search_result(db, enriching_job, monkeypatch):
    from backend.services.discovery import enrichment

    monkeypatch.setattr(enrichment, "_enrich_company", lambda *a, **k: None)
    monkeypatch.setattr(enrichment, "discover_public_phones", lambda *a, **k: [])
    monkeypatch.setattr(
        EmailEnrichmentService,
        "enrich",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("provider down")),
    )

    enrichment.run_enrichment(db, enriching_job)

    db.refresh(enriching_job)
    assert enriching_job.status == SearchJobStatus.COMPLETED
    assert enriching_job.qualified_leads == 1
    assert db.query(LeadRecord).count() == 1


def test_a_snippet_email_counts_without_a_network_lookup(db, enriching_job, monkeypatch):
    """Volume only becomes emails if we keep the ones we already harvested."""
    from backend.services.discovery import enrichment

    lead = db.query(LeadRecord).filter(LeadRecord.search_job_id == enriching_job.id).one()
    lead.email = "anna.lind@nordicfreight.se"
    lead.email_status = "found"
    lead.email_source = "snippet"
    lead.phone = "+46 70 123 45 67"
    lead.phone_source = "snippet"
    db.commit()

    monkeypatch.setattr(
        enrichment,
        "_network_enrich",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("already had email and phone")),
    )

    enrichment.run_enrichment(db, enriching_job)

    db.refresh(lead)
    db.refresh(enriching_job)
    assert lead.email == "anna.lind@nordicfreight.se"
    assert lead.enrichment_status == EnrichmentStatus.ENRICHED.value
    assert enriching_job.emails_found == 1


def test_a_miss_does_not_wipe_an_existing_email():
    from types import SimpleNamespace

    from backend.services.discovery.enrichment import _apply_email
    from backend.services.EmailModule.types import EmailResult, EmailStatus

    lead = SimpleNamespace(
        email="anna.lind@nordicfreight.se",
        email_status="found",
        email_source="snippet",
        email_confidence=None,
        email_pattern=None,
        email_source_url=None,
        email_evidence=None,
        email_verification=None,
        email_enriched_at=None,
        email_verified_at=None,
        company_domain=None,
    )
    kept = _apply_email(lead, EmailResult(status=EmailStatus.NOT_FOUND.value))
    assert kept is True
    assert lead.email == "anna.lind@nordicfreight.se"
