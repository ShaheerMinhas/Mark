"""Phase 5 budget gates. Adapters are fixtures. No provider network call is made."""

from __future__ import annotations

import threading

from fastapi.testclient import TestClient

from backend.database import SessionLocal
from backend.models import (
    DiscoveryBudgetPeriod,
    DiscoveryReservation,
    DiscoveryRun,
    DiscoverySettings,
    DiscoveryTarget,
    GlobalFieldFreshness,
    GlobalPerson,
    User,
)
from backend.services.EmailModule.intelligence import EmailEnrichmentService
from backend.services.EmailModule.providers.base import ProviderFind, ProviderVerify
from backend.services.EmailModule.types import PersonInput
from backend.services.discovery_engine.enrich import AdapterResult, clear_adapters, run_email_stage, use_adapter
from backend.services.discovery_engine.pipeline import execute_run
from backend.services.discovery_engine.settings import ensure_settings
from backend.services.discovery_engine.spend import (
    BudgetDenied,
    recover_stuck,
    reserve_max,
    seed_providers,
    set_budget,
    set_credential,
    set_price,
    usage_report,
)
from backend.services.discovery_engine.ssrf import public_http_url
from backend.services.discovery_engine.suppression import add_suppression
from backend.services.global_cache.upsert import PublicProspect, upsert_public_prospect


def _client():
    from backend.main import app

    return TestClient(app)


def _admin(client, db, email):
    created = client.post(
        "/api/auth/register",
        json={"email": email, "password": "test-password-123", "full_name": "Budget Admin"},
    )
    assert created.status_code == 200, created.text
    row = db.query(User).filter(User.email == email).one()
    row.is_platform_admin = True
    db.commit()
    logged = client.post("/api/auth/admin/login", json={"email": email, "password": "test-password-123"})
    assert logged.status_code == 200, logged.text
    return {"Authorization": f"Bearer {logged.json()['access_token']}"}


def _auth(client, email, *, organization=False):
    body = {"email": email, "password": "test-password-123", "full_name": "Budget User"}
    if organization:
        body["account_type"] = "organization"
        body["organization_name"] = "Owned Workspace"
    created = client.post("/api/auth/register", json=body)
    assert created.status_code == 200, created.text
    return {"Authorization": f"Bearer {created.json()['access_token']}"}


def _arm(db, *, limit=500, rate=10, price=100):
    seed_providers(db)
    db.commit()
    set_credential(db, provider_name="hunter", api_key="platform-fixture-key")
    set_price(db, provider_name="hunter", operation="email_find", currency="USD", max_cost_minor=price)
    set_price(db, provider_name="hunter", operation="email_verify", currency="USD", max_cost_minor=price)
    for scope, name, period in (
        ("global", "", "day"),
        ("global", "", "month"),
        ("provider", "hunter", "day"),
        ("provider", "hunter", "month"),
    ):
        set_budget(db, scope=scope, provider_name=name, period=period, currency="USD", limit_minor=limit)
    row = db.get(__import__("backend.models", fromlist=["DiscoveryProvider"]).DiscoveryProvider, "hunter")
    row.enabled = True
    row.rate_per_hour = rate
    settings = ensure_settings(db)
    settings.paid_providers_enabled = True
    settings.pause_email = False
    settings.pause_verify = True
    settings.pause_enrichment = True
    db.commit()
    return settings


def _person(db, **overrides):
    body = dict(
        full_name="Example Budget Nora",
        title="CTO",
        company_name="Example Budget",
        domain="example-budget.test",
        country_code="SE",
        profile_url="https://www.linkedin.com/in/example-budget-nora",
        email_status="unknown",
    )
    body.update(overrides)
    return upsert_public_prospect(db, PublicProspect(**body))


def _run(db, target):
    run = DiscoveryRun(
        target_id=target.id,
        idempotency_key=f"budget-{target.id}",
        status="queued",
        scheduled_for=__import__("datetime").datetime.utcnow(),
    )
    db.add(run)
    db.commit()
    return run


def test_missing_price_or_credential_denies_before_the_call(db, monkeypatch):
    monkeypatch.setenv("HUNTER_API_KEY", "customer-workspace-key")
    seed_providers(db)
    db.commit()
    calls = []
    use_adapter("hunter", "email_find", lambda person: calls.append(person.id) or AdapterResult(email_status="found", email="a@example-budget.test"))
    try:
        person = _person(db)
        target = DiscoveryTarget(name="Example budget", scheduling_status="paused", criteria={}, max_people_per_run=5)
        db.add(target)
        db.commit()
        settings = ensure_settings(db)
        settings.paid_providers_enabled = True
        settings.pause_email = False
        row = db.get(__import__("backend.models", fromlist=["DiscoveryProvider"]).DiscoveryProvider, "hunter")
        row.enabled = True
        row.rate_per_hour = 5
        db.commit()
        run = _run(db, target)
        status, detail = run_email_stage(db, run, target, settings, 1)
        assert calls == []
        assert db.query(DiscoveryReservation).count() == 0
        assert "Denied" in detail or status == "classified"
        set_credential(db, provider_name="hunter", api_key="platform-only")
        run_email_stage(db, run, target, settings, 1)
        assert calls == []
        assert db.query(DiscoveryReservation).count() == 0
    finally:
        clear_adapters()
    assert person.id


def test_zero_price_and_unknown_provider_are_rejected(db):
    seed_providers(db)
    db.commit()
    with _client() as client:
        headers = _admin(client, db, "budget-admin@example.com")
        member = _auth(client, "budget-member@example.com")
        owner = _auth(client, "budget-owner@example.com", organization=True)
        for denied in (member, owner):
            assert client.get("/api/platform/discovery/providers", headers=denied).status_code == 401
            assert client.get("/api/platform/discovery/usage", headers=denied).status_code == 401
            assert client.get("/api/platform/discovery/cache-health", headers=denied).status_code == 401
        zero = client.put(
            "/api/platform/discovery/providers/hunter/price",
            json={"operation": "email_find", "currency": "USD", "max_cost_minor": 0},
            headers=headers,
        )
        assert zero.status_code == 400
        company = client.put(
            "/api/platform/discovery/providers/hunter/price",
            json={"operation": "company_research", "currency": "USD", "max_cost_minor": 25},
            headers=headers,
        )
        assert company.status_code == 400
        saved = client.put(
            "/api/platform/discovery/providers/hunter/credential",
            json={"api_key": "platform-fixture-key"},
            headers=headers,
        )
        assert saved.status_code == 200
        assert "platform-fixture-key" not in saved.text
        listed = client.get("/api/platform/discovery/providers", headers=headers)
        assert "platform-fixture-key" not in listed.text
        assert listed.json()["items"][0]["credential_configured"] is True or any(
            item["credential_configured"] for item in listed.json()["items"]
        )
        assert listed.json()["company_research"] == "unavailable"


def test_reservation_bounds_retry_and_crash_accounting(db):
    from backend.services.discovery_engine.spend import reconcile

    _arm(db, limit=1000, price=100, rate=10)
    first = reserve_max(db, idempotency_key="once", provider_name="hunter", operation="email_find", run_id=1, person_id=1, attempt=1)
    again = reserve_max(db, idempotency_key="once", provider_name="hunter", operation="email_find", run_id=1, person_id=1, attempt=1)
    assert again.id == first.id
    assert db.query(DiscoveryBudgetPeriod).filter(DiscoveryBudgetPeriod.reserved_minor == 100).count() == 4
    reconcile(db, first.id, charge_known=False, actual_minor=None, outcome="failure", http_status=500, success=False, credits=None)
    db.expire_all()
    assert db.query(DiscoveryBudgetPeriod).filter(DiscoveryBudgetPeriod.unknown_minor == 100, DiscoveryBudgetPeriod.reserved_minor == 0).count() == 4
    second = reserve_max(db, idempotency_key="retry", provider_name="hunter", operation="email_find", run_id=1, person_id=1, attempt=2)
    assert second.id != first.id
    held = reserve_max(db, idempotency_key="known-charge", provider_name="hunter", operation="email_find", run_id=3, person_id=3, attempt=1)
    reconcile(db, held.id, charge_known=True, actual_minor=40, outcome="success", http_status=200, success=True, credits=1)
    db.expire_all()
    assert db.query(DiscoveryBudgetPeriod).filter(DiscoveryBudgetPeriod.settled_minor == 40).count() == 4
    over = reserve_max(db, idempotency_key="over", provider_name="hunter", operation="email_find", run_id=4, person_id=4, attempt=1)
    reconcile(db, over.id, charge_known=True, actual_minor=9999, outcome="success", http_status=200, success=True, credits=None)
    db.expire_all()
    saved = db.get(DiscoveryReservation, over.id)
    assert saved.status == "unknown_charge"
    assert saved.actual_minor is None
    stuck = reserve_max(db, idempotency_key="crash", provider_name="hunter", operation="email_find", run_id=5, person_id=5, attempt=1)
    recover_stuck(db, older_than_seconds=0)
    db.expire_all()
    assert db.get(DiscoveryReservation, stuck.id).status == "unknown_charge"
    unknown_before = [row.unknown_minor for row in db.query(DiscoveryBudgetPeriod).all()]
    recover_stuck(db, older_than_seconds=0)
    db.expire_all()
    unknown_after = [row.unknown_minor for row in db.query(DiscoveryBudgetPeriod).all()]
    assert unknown_before == unknown_after


def test_a_budget_that_cannot_hold_another_maximum_denies(db):
    _arm(db, limit=100, price=100, rate=10)
    reserve_max(db, idempotency_key="fills-the-budget", provider_name="hunter", operation="email_find", run_id=1, person_id=1, attempt=1)
    try:
        reserve_max(db, idempotency_key="does-not-fit", provider_name="hunter", operation="email_find", run_id=1, person_id=1, attempt=2)
        raised = False
    except BudgetDenied:
        raised = True
    assert raised


def test_two_connections_cannot_both_take_the_last_unit(db):
    _arm(db, limit=100, price=100, rate=10)
    results = []

    def work(key):
        session = SessionLocal()
        try:
            reserve_max(session, idempotency_key=key, provider_name="hunter", operation="email_find", run_id=None, person_id=None, attempt=1)
            results.append("ok")
        except BudgetDenied:
            results.append("denied")
        finally:
            session.close()

    threads = [threading.Thread(target=work, args=(f"race-{index}",)) for index in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(results) == ["denied", "ok"]


def test_email_stage_uses_the_fixture_and_skips_fresh_rows(db):
    _arm(db, limit=500, price=80, rate=10)
    person = _person(db, email="nora@example-budget.test", email_status="found")
    fresh = GlobalFieldFreshness(
        person_id=person.id,
        field_name="email",
        source="hunter",
        next_due_at=__import__("datetime").datetime.utcnow() + __import__("datetime").timedelta(days=2),
    )
    db.add(fresh)
    target = DiscoveryTarget(name="Example fresh", scheduling_status="paused", criteria={}, max_people_per_run=5)
    db.add(target)
    db.commit()
    calls = []
    use_adapter("hunter", "email_find", lambda _person: calls.append("called") or AdapterResult(email_status="verified", email="nora@example-budget.test", http_status=200, charge_known=True, actual_minor=80))
    try:
        settings = db.get(DiscoverySettings, 1)
        run = _run(db, target)
        run_email_stage(db, run, target, settings, 1)
        assert calls == []
        fresh.next_due_at = __import__("datetime").datetime.utcnow() - __import__("datetime").timedelta(hours=1)
        db.commit()
        run_email_stage(db, run, target, settings, 2)
        assert calls == ["called"]
        db.refresh(person)
        assert person.email_status == "verified"
        assert person.quality_tier == "verified"
        stamp = db.query(GlobalFieldFreshness).filter(GlobalFieldFreshness.person_id == person.id).one()
        assert stamp.enriched_at is not None
        assert stamp.next_due_at > __import__("datetime").datetime.utcnow()
    finally:
        clear_adapters()


def test_inferred_is_not_verified_and_suppression_skips_the_call(db):
    _arm(db)
    person = _person(db)
    target = DiscoveryTarget(name="Example inferred", scheduling_status="paused", criteria={}, max_people_per_run=5)
    db.add(target)
    db.commit()
    use_adapter(
        "hunter",
        "email_find",
        lambda _person: AdapterResult(email_status="inferred", email="nora@example-budget.test", http_status=200, charge_known=False),
    )
    try:
        settings = db.get(DiscoverySettings, 1)
        run = _run(db, target)
        run_email_stage(db, run, target, settings, 1)
        db.refresh(person)
        assert person.email_status == "inferred"
        assert person.quality_tier not in {"verified", "contactable"}
        add_suppression(db, kind="email", value="nora@example-budget.test", reason="example", user_id=None)
        db.commit()
        calls = []
        clear_adapters()
        use_adapter("hunter", "email_find", lambda _person: calls.append("x") or AdapterResult(email_status="found", email="nora@example-budget.test", http_status=200))
        run_email_stage(db, run, target, settings, 2)
        assert calls == []
    finally:
        clear_adapters()


def test_fallback_after_rate_limit_and_company_stage_is_unavailable(db, monkeypatch):
    def _boom(*_args, **_kwargs):
        raise AssertionError("company lookup called")

    monkeypatch.setattr("backend.services.CompanyModule.finder.lookup_company", _boom)
    _arm(db, limit=500, price=50, rate=10)
    from backend.models import DiscoveryProvider

    second = db.get(DiscoveryProvider, "tomba")
    second.enabled = True
    second.rate_per_hour = 5
    db.commit()
    set_credential(db, provider_name="tomba", api_key="platform-tomba")
    set_price(db, provider_name="tomba", operation="email_find", currency="USD", max_cost_minor=50)
    for period in ("day", "month"):
        set_budget(db, scope="provider", provider_name="tomba", period=period, currency="USD", limit_minor=500)
    person = _person(db, profile_url="https://www.linkedin.com/in/example-budget-fallback")
    target = DiscoveryTarget(name="Example fallback", scheduling_status="paused", criteria={}, max_people_per_run=5)
    db.add(target)
    db.commit()
    use_adapter("hunter", "email_find", lambda _person: AdapterResult(email_status="unknown", http_status=429, charge_known=False))
    use_adapter("tomba", "email_find", lambda _person: AdapterResult(email_status="found", email="nora@example-budget.test", http_status=200, charge_known=False))
    try:
        settings = db.get(DiscoverySettings, 1)
        settings.pause_enrichment = False
        db.commit()
        run = _run(db, target)
        execute_run(db, run, attempt=1)
        db.refresh(person)
        assert person.email_status == "found"
        report = usage_report(db)
        assert report["rate_limited"] == 1
        assert report["unknown_cost_minor"] is not None
        assert report["actual_cost_minor"] is None
        assert report["estimated_cost_status"] == "reserved_maximum"
        assert report["unknown_cost_status"] == "unknown"
        from backend.models import DiscoveryStageAttempt

        enrich = (
            db.query(DiscoveryStageAttempt)
            .filter(DiscoveryStageAttempt.run_id == run.id, DiscoveryStageAttempt.stage == "enrich")
            .one()
        )
        assert enrich.status == "unavailable"
    finally:
        clear_adapters()


def test_customer_enrichment_does_not_open_a_global_reservation(db):
    class _Fake:
        name = "hunter"

        def available(self):
            return True

        def find_email(self, **kwargs):
            return ProviderFind(email="ada@example-customer.test", raw_status="found", credits=2, http_status=200)

        def verify_email(self, email):
            return ProviderVerify(status="valid", credits=1, http_status=200)

    EmailEnrichmentService(providers=[_Fake()]).enrich(
        PersonInput(name="Ada Example", company_domain="example-customer.test"),
        use_providers=True,
        use_public_web=False,
        verify=False,
    )
    assert db.query(DiscoveryReservation).count() == 0


def test_usage_does_not_turn_an_empty_ledger_into_zero(db):
    report = usage_report(db)
    assert report["actual_cost_minor"] is None
    assert report["unknown_cost_minor"] is None
    assert report["estimated_cost_minor"] is None
    assert report["credits"] is None
    assert public_http_url("http://169.254.169.254/latest") is None
    assert public_http_url("http://127.0.0.1/admin") is None
    assert public_http_url("https://example.com/profile") == "https://example.com/profile"


def test_paid_switch_off_still_does_not_call(db, monkeypatch):
    def _boom(*_args, **_kwargs):
        raise AssertionError("provider called")

    monkeypatch.setattr("backend.services.discovery.runner.search_text", _boom)
    monkeypatch.setattr("backend.services.discovery_engine.enrich.invoke_provider", _boom)
    target = DiscoveryTarget(name="Example off", scheduling_status="paused")
    db.add(target)
    db.commit()
    run = _run(db, target)
    execute_run(db, run, attempt=1)
    from backend.models import DiscoveryStageAttempt

    details = {row.stage: row.detail for row in db.query(DiscoveryStageAttempt).filter(DiscoveryStageAttempt.run_id == run.id)}
    assert "Paid providers are disabled" in details["email"]
    assert "Paid providers are disabled" in details["enrich"]
