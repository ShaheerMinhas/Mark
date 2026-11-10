"""Operator sign-in is a separate session from a customer sign-in."""

from __future__ import annotations

from datetime import timedelta

from fastapi.testclient import TestClient

from backend.auth_utils import ADMIN_AUDIENCE, create_access_token
from backend.models import User
from backend.services.login_guard import reset_attempts


def _client():
    from backend.main import app

    return TestClient(app)


def _register(client, email, *, organization=False):
    body = {"email": email, "password": "test-password-123", "full_name": "Session Test"}
    if organization:
        body["account_type"] = "organization"
        body["organization_name"] = "Owned Workspace"
    created = client.post("/api/auth/register", json=body)
    assert created.status_code == 200, created.text
    return {"Authorization": f"Bearer {created.json()['access_token']}"}


def test_admin_login_is_separate_from_the_customer_session(db):
    reset_attempts()
    with _client() as client:
        assert client.get("/api/platform/discovery/overview").status_code == 401
        customer = _register(client, "admin-customer@example.com")
        owner = _register(client, "admin-owner@example.com", organization=True)
        for headers in (customer, owner):
            denied = client.post(
                "/api/auth/admin/login",
                json={"email": "admin-customer@example.com" if headers is customer else "admin-owner@example.com", "password": "test-password-123"},
            )
            assert denied.status_code == 401
            assert denied.json()["detail"] == "Invalid email or password"
            assert client.get("/api/platform/discovery/overview", headers=headers).status_code == 401
        unknown = client.post(
            "/api/auth/admin/login",
            json={"email": "nobody-admin@example.com", "password": "test-password-123"},
        )
        assert unknown.status_code == 401
        assert unknown.json()["detail"] == "Invalid email or password"
        row = db.query(User).filter(User.email == "admin-customer@example.com").one()
        row.is_platform_admin = True
        db.commit()
        logged = client.post(
            "/api/auth/admin/login",
            json={"email": "admin-customer@example.com", "password": "test-password-123", "next": "https://evil.example/phish"},
        )
        assert logged.status_code == 200, logged.text
        assert logged.json()["next"] == "/admin"
        assert "organizations" not in logged.json()["operator"]
        admin = {"Authorization": f"Bearer {logged.json()['access_token']}"}
        assert client.get("/api/auth/admin/me", headers=admin).status_code == 200
        assert client.get("/api/platform/discovery/overview", headers=admin).status_code == 200
        assert client.get("/api/auth/me", headers=admin).status_code == 401
        assert client.get("/api/searches", headers=admin).status_code == 401
        assert client.get("/api/auth/me", headers=customer).status_code == 200
        assert client.get("/api/platform/discovery/overview", headers=customer).status_code == 401
        logged_out = client.post("/api/auth/admin/logout", headers=admin)
        assert logged_out.status_code == 200
        assert client.get("/api/platform/discovery/overview", headers=admin).status_code == 401
        assert client.get("/api/auth/me", headers=customer).status_code == 200
        again = client.post(
            "/api/auth/admin/login",
            json={"email": "admin-customer@example.com", "password": "test-password-123"},
        )
        fresh = {"Authorization": f"Bearer {again.json()['access_token']}"}
        row.is_platform_admin = False
        db.commit()
        assert client.get("/api/platform/discovery/overview", headers=fresh).status_code == 403
        expired = create_access_token(
            {"sub": str(row.id), "ver": int(row.admin_token_version or 0)},
            expires_delta=timedelta(minutes=-5),
            audience=ADMIN_AUDIENCE,
        )
        assert client.get(
            "/api/platform/discovery/overview",
            headers={"Authorization": f"Bearer {expired}"},
        ).status_code == 401


def test_admin_login_rate_limit_uses_one_message(db):
    reset_attempts()
    try:
        with _client() as client:
            _register(client, "admin-limit@example.com")
            last = None
            for _ in range(8):
                last = client.post(
                    "/api/auth/admin/login",
                    json={"email": "admin-limit@example.com", "password": "wrong-password"},
                )
                assert last.status_code == 401
            blocked = client.post(
                "/api/auth/admin/login",
                json={"email": "admin-limit@example.com", "password": "test-password-123"},
            )
            assert blocked.status_code == 429
            assert "exist" not in blocked.json()["detail"].lower()
            customer = client.post(
                "/api/auth/login",
                json={"email": "admin-limit@example.com", "password": "test-password-123"},
            )
            assert customer.status_code == 200
    finally:
        reset_attempts()
