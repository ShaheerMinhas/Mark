from backend.services.EmailModule.intelligence import EmailEnrichmentService
from backend.services.EmailModule.jobs import chunked, enrich_lead_ids
from backend.services.EmailModule.patterns import detect_pattern, generate_candidates, match_pattern
from backend.services.EmailModule.store import MemoryEmailStore
from backend.services.EmailModule.types import EmailStatus, PersonInput
from backend.services.EmailModule.verify import local_verify, normalize_provider_verify
from tests.fakes import FakeProvider


def _person(**kwargs):
    base = dict(
        name="Ahmed Raza",
        company_name="Acme Inc",
        company_domain="acme.com",
        snippet="",
    )
    base.update(kwargs)
    return PersonInput(**base)


def test_public_exact_email_in_snippet():
    svc = EmailEnrichmentService(store=MemoryEmailStore(), providers=[])
    result = svc.enrich(
        _person(snippet="Reach Ahmed at ahmed.raza@acme.com for partnerships."),
        use_providers=False,
        use_public_web=False,
        verify=False,
    )
    assert result.email == "ahmed.raza@acme.com"
    assert result.status == EmailStatus.FOUND.value
    assert result.source == "snippet"


def test_pattern_from_three_employees():
    examples = [
        ("John Smith", "john.smith@acme.com"),
        ("Sarah Jones", "sarah.jones@acme.com"),
        ("Mia Chen", "mia.chen@acme.com"),
    ]
    pattern, confidence, count = detect_pattern(examples)
    assert pattern == "first.last"
    assert count == 3
    assert confidence >= 0.7
    cands = generate_candidates("Ahmed Raza", "acme.com", [(pattern, confidence)])
    assert cands[0]["email"] == "ahmed.raza@acme.com"
    assert cands[0]["status"] == "inferred"


def test_person_has_no_email():
    svc = EmailEnrichmentService(store=MemoryEmailStore(), providers=[])
    result = svc.enrich(_person(), use_providers=False, use_public_web=False, verify=False)
    assert result.email is None
    assert result.status == EmailStatus.NOT_FOUND.value


def test_competing_patterns_lower_confidence():
    examples = [
        ("John Smith", "john.smith@acme.com"),
        ("Sarah Jones", "sjones@acme.com"),
        ("Mia Chen", "mia.chen@acme.com"),
    ]
    pattern, confidence, count = detect_pattern(examples)
    assert pattern == "first.last"
    assert count == 2
    assert confidence < 0.7


def test_catchall_is_not_verified():
    provider = FakeProvider("hunter", email="ahmed.raza@acme.com", verify="accept_all")
    svc = EmailEnrichmentService(store=MemoryEmailStore(), providers=[provider])
    result = svc.enrich(_person(), use_providers=True, use_public_web=False, verify=True)
    assert result.email == "ahmed.raza@acme.com"
    assert result.status == EmailStatus.VERIFY_UNCERTAIN.value
    assert result.status != EmailStatus.VERIFIED.value


def test_provider_valid():
    provider = FakeProvider("hunter", email="ahmed.raza@acme.com", verify="valid")
    svc = EmailEnrichmentService(store=MemoryEmailStore(), providers=[provider])
    result = svc.enrich(_person(), use_providers=True, use_public_web=False, verify=True)
    assert result.status == EmailStatus.VERIFIED.value
    assert result.email == "ahmed.raza@acme.com"


def test_provider_invalid_drops_inferred_or_found_candidate():
    provider = FakeProvider("hunter", email="nope@acme.com", verify="invalid")
    svc = EmailEnrichmentService(store=MemoryEmailStore(), providers=[provider])
    result = svc.enrich(_person(), use_providers=True, use_public_web=False, verify=True)
    assert result.status == EmailStatus.NOT_FOUND.value
    assert result.email is None


def test_provider_unknown_stays_unverified():
    provider = FakeProvider("hunter", email="ahmed.raza@acme.com", verify="unknown")
    svc = EmailEnrichmentService(store=MemoryEmailStore(), providers=[provider])
    result = svc.enrich(_person(), use_providers=True, use_public_web=False, verify=True)
    assert result.email == "ahmed.raza@acme.com"
    assert result.status == EmailStatus.UNVERIFIED.value


def test_provider_without_key_is_skipped():
    missing = FakeProvider("hunter", email="x@acme.com", available=False)
    svc = EmailEnrichmentService(store=MemoryEmailStore(), providers=[missing])
    result = svc.enrich(_person(), use_providers=True, use_public_web=False, verify=False)
    assert result.status == EmailStatus.NOT_FOUND.value
    assert missing.find_calls == 0


def test_conflicting_providers_first_available_wins():
    a = FakeProvider("hunter", email="ahmed.raza@acme.com", verify="valid")
    b = FakeProvider("tomba", email="araza@acme.com", verify="valid")
    svc = EmailEnrichmentService(store=MemoryEmailStore(), providers=[a, b])
    result = svc.enrich(_person(), use_providers=True, use_public_web=False, verify=True)
    assert result.email == "ahmed.raza@acme.com"
    assert a.find_calls == 1
    assert b.find_calls == 0


def test_duplicate_person_uses_prior_verified():
    store = MemoryEmailStore()
    provider = FakeProvider("hunter", email="ahmed.raza@acme.com", verify="valid")
    svc = EmailEnrichmentService(store=store, providers=[provider])
    first = svc.enrich(_person(), use_providers=True, use_public_web=False, verify=True)
    assert first.status == EmailStatus.VERIFIED.value
    second = svc.enrich(_person(), use_providers=True, use_public_web=False, verify=True)
    assert second.cached is True
    assert second.email == first.email
    assert provider.find_calls == 1


def test_previously_enriched_without_force_reuses_verified():
    test_duplicate_person_uses_prior_verified()


def test_stale_cache_requeries():
    store = MemoryEmailStore(cache_days=0)
    provider = FakeProvider("hunter", email="ahmed.raza@acme.com", verify="valid")
    svc = EmailEnrichmentService(store=store, providers=[provider])
    svc.enrich(_person(), use_providers=True, use_public_web=False, verify=True)
    svc.enrich(_person(), use_providers=True, use_public_web=False, verify=True)
    assert provider.find_calls == 2


def test_rate_limit_falls_through():
    busy = FakeProvider("hunter", http_find=429)
    backup = FakeProvider("tomba", email="ahmed.raza@acme.com", verify="valid")
    svc = EmailEnrichmentService(store=MemoryEmailStore(), providers=[busy, backup])
    result = svc.enrich(_person(), use_providers=True, use_public_web=False, verify=True)
    assert result.email == "ahmed.raza@acme.com"
    assert busy.find_calls == 1
    assert backup.find_calls == 1


def test_bulk_enrichment_thirty_leads_with_mocks():
    class FakeLead:
        def __init__(self, i):
            self.id = i
            self.name = f"Person {i}"
            self.company_name = "Acme Inc"
            self.company_domain = "acme.com"
            self.work_designation = "CMO"
            self.linkedin_url = ""
            self.raw_snippet = ""
            self.search_job_id = 1
            self.email = None
            self.email_status = None
            self.email_confidence = None
            self.email_source = None
            self.email_pattern = None
            self.email_source_url = None
            self.email_evidence = None
            self.email_verification = None
            self.email_verified_at = None
            self.email_enriched_at = None

    leads = {i: FakeLead(i) for i in range(1, 31)}

    class FakeDb:
        def get(self, _cls, lead_id):
            return leads.get(lead_id)

        def commit(self):
            return None

        def refresh(self, _obj):
            return None

    provider = FakeProvider("hunter", email="person@acme.com", verify="valid")
    svc = EmailEnrichmentService(store=MemoryEmailStore(), providers=[provider])
    stats = enrich_lead_ids(
        FakeDb(),
        list(leads),
        use_providers=True,
        use_public_web=False,
        batch_size=10,
        service=svc,
    )
    assert stats["processed"] == 30
    assert stats["verified"] == 30


def test_bulk_10000_is_chunked():
    groups = list(chunked(list(range(10_000)), 25))
    assert len(groups) == 400
    assert groups[0] == list(range(25))
    assert groups[-1][-1] == 9999


def test_local_verify_and_normalize():
    assert local_verify("not-an-email")["status"] == "invalid"
    assert normalize_provider_verify("accept_all") == "accept_all"
    assert normalize_provider_verify("deliverable") == "valid"
    assert match_pattern("jsmith@acme.com", "John", "Smith") == "flast"


def test_inferred_never_looks_verified_without_provider():
    store = MemoryEmailStore()
    store.observe_pattern("acme.com", "first.last", "john.smith@acme.com", 0.8, 3)
    store.examples["acme.com"] = [
        ("John Smith", "john.smith@acme.com"),
        ("Sarah Jones", "sarah.jones@acme.com"),
        ("Mia Chen", "mia.chen@acme.com"),
    ]
    svc = EmailEnrichmentService(store=store, providers=[])
    result = svc.enrich(_person(), use_providers=False, use_public_web=False, verify=False)
    assert result.status == EmailStatus.INFERRED.value
    assert result.email == "ahmed.raza@acme.com"
    assert result.status != EmailStatus.VERIFIED.value
    assert result.status != EmailStatus.FOUND.value


def test_findymail_skips_second_verify_call():
    provider = FakeProvider("findymail", email="ahmed.raza@acme.com", verify="valid")
    svc = EmailEnrichmentService(store=MemoryEmailStore(), providers=[provider])
    result = svc.enrich(_person(), use_providers=True, use_public_web=False, verify=True)
    assert result.email == "ahmed.raza@acme.com"
    assert result.status == EmailStatus.VERIFIED.value
    assert result.source == "findymail"
    assert provider.find_calls == 1
    assert provider.verify_calls == 0


def test_provider_uses_company_name_when_domain_missing():
    provider = FakeProvider("findymail", email="ahmed.raza@acme.com", verify="valid")
    svc = EmailEnrichmentService(store=MemoryEmailStore(), providers=[provider])
    result = svc.enrich(
        _person(company_domain=""),
        use_providers=True,
        use_public_web=False,
        verify=True,
    )
    assert result.email == "ahmed.raza@acme.com"
    assert provider.find_calls == 1


if __name__ == "__main__":
    import traceback

    failed = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print("ok", name)
            except Exception:
                failed += 1
                print("FAIL", name)
                traceback.print_exc()
    raise SystemExit(failed)
