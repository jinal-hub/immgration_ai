"""Tests for authentication (Sprint P4)."""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool
from fastapi.testclient import TestClient

from db.models import Base
from db.connection import get_session_factory
from api.main import app


@pytest.fixture()
def db_engine():
    eng = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=eng)
    return eng


@pytest.fixture()
def client(db_engine):
    app.state.db_engine = db_engine
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def auth_token(client):
    """Register a user and return their JWT token."""
    resp = client.post(
        "/api/v1/auth/register",
        json={"email": "test@example.com", "password": "secret123", "full_name": "Test User"},
    )
    return resp.json()["access_token"]


@pytest.fixture()
def auth_headers(auth_token):
    return {"Authorization": f"Bearer {auth_token}"}


# ---------------------------------------------------------------------------
# Register
# ---------------------------------------------------------------------------

class TestRegister:
    def test_register_success(self, client):
        resp = client.post(
            "/api/v1/auth/register",
            json={"email": "new@example.com", "password": "pass123", "full_name": "New User"},
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["email"] == "new@example.com"
        assert data["full_name"] == "New User"
        assert data["role"] == "applicant"
        assert "user_id" in data

    def test_register_duplicate_email(self, client):
        client.post(
            "/api/v1/auth/register",
            json={"email": "dup@example.com", "password": "pass123"},
        )
        resp = client.post(
            "/api/v1/auth/register",
            json={"email": "dup@example.com", "password": "pass456"},
        )
        # Deterministic 409 — never a raw 500 from the DB constraint.
        assert resp.status_code == 409
        assert resp.json()["detail"] == "Email already registered"


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------

class TestRegistrationPolicy:
    def test_email_is_normalised(self, client):
        resp = client.post(
            "/api/v1/auth/register",
            json={"email": "  Mixed.Case@Example.COM ", "password": "pass123"},
        )
        assert resp.status_code == 201
        assert resp.json()["email"] == "mixed.case@example.com"

    @pytest.mark.parametrize("email", ["no-at-sign", "a@b", "@example.com", "a b@example.com", ""])
    def test_invalid_email_is_422(self, client, email):
        resp = client.post(
            "/api/v1/auth/register", json={"email": email, "password": "pass123"},
        )
        assert resp.status_code == 422

    def test_password_minimum_is_enforced_at_the_api_default(
        self, client, monkeypatch
    ):
        monkeypatch.delenv("PASSWORD_MIN_LENGTH", raising=False)
        resp = client.post(
            "/api/v1/auth/register",
            json={"email": "short@example.com", "password": "seven77"},
        )
        assert resp.status_code == 422

    def test_password_over_bcrypt_limit_is_422(self, client):
        resp = client.post(
            "/api/v1/auth/register",
            json={"email": "long@example.com", "password": "a" * 73},
        )
        assert resp.status_code == 422

    def test_login_still_accepts_a_short_wrong_password(self, client):
        """Login must answer 401 (not a schema 422) for a legacy short account."""
        client.post(
            "/api/v1/auth/register",
            json={"email": "legacy@example.com", "password": "mypass"},
        )
        resp = client.post(
            "/api/v1/auth/login",
            json={"email": "legacy@example.com", "password": "x"},
        )
        assert resp.status_code == 401


class TestLoginThrottle:
    def test_repeated_failures_lock_the_email_and_ip(
        self, client, monkeypatch
    ):
        monkeypatch.setenv("LOGIN_MAX_ATTEMPTS", "2")
        monkeypatch.setenv("LOGIN_LOCKOUT_SECONDS", "60")
        client.post(
            "/api/v1/auth/register",
            json={"email": "lock@example.com", "password": "pass123"},
        )
        first = client.post(
            "/api/v1/auth/login",
            json={"email": "lock@example.com", "password": "wrong1"},
        )
        assert first.status_code == 401
        second = client.post(
            "/api/v1/auth/login",
            json={"email": "lock@example.com", "password": "wrong2"},
        )
        assert second.status_code == 429
        assert second.headers["Retry-After"]

        # Even the correct password is refused while the key is locked.
        correct = client.post(
            "/api/v1/auth/login",
            json={"email": "lock@example.com", "password": "pass123"},
        )
        assert correct.status_code == 429

    def test_a_success_resets_the_counter(self, client, monkeypatch):
        monkeypatch.setenv("LOGIN_MAX_ATTEMPTS", "2")
        client.post(
            "/api/v1/auth/register",
            json={"email": "reset@example.com", "password": "pass123"},
        )
        assert client.post(
            "/api/v1/auth/login",
            json={"email": "reset@example.com", "password": "wrong1"},
        ).status_code == 401
        assert client.post(
            "/api/v1/auth/login",
            json={"email": "reset@example.com", "password": "pass123"},
        ).status_code == 200
        assert client.post(
            "/api/v1/auth/login",
            json={"email": "reset@example.com", "password": "wrong2"},
        ).status_code == 401

    def test_throttle_is_scoped_per_email(self, client, monkeypatch):
        monkeypatch.setenv("LOGIN_MAX_ATTEMPTS", "2")
        for email in ("one@example.com", "two@example.com"):
            client.post(
                "/api/v1/auth/register",
                json={"email": email, "password": "pass123"},
            )
        client.post("/api/v1/auth/login", json={"email": "one@example.com", "password": "no1"})
        client.post("/api/v1/auth/login", json={"email": "one@example.com", "password": "no2"})
        assert client.post(
            "/api/v1/auth/login", json={"email": "one@example.com", "password": "pass123"},
        ).status_code == 429
        assert client.post(
            "/api/v1/auth/login", json={"email": "two@example.com", "password": "pass123"},
        ).status_code == 200


class TestLogin:
    def test_login_success(self, client):
        client.post(
            "/api/v1/auth/register",
            json={"email": "login@example.com", "password": "mypass"},
        )
        resp = client.post(
            "/api/v1/auth/login",
            json={"email": "login@example.com", "password": "mypass"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "access_token" in data
        assert data["token_type"] == "bearer"
        assert data["expires_in"] > 0
        assert data["role"] == "applicant"

    def test_login_wrong_password(self, client):
        client.post(
            "/api/v1/auth/register",
            json={"email": "wrong@example.com", "password": "correct"},
        )
        resp = client.post(
            "/api/v1/auth/login",
            json={"email": "wrong@example.com", "password": "incorrect"},
        )
        assert resp.status_code == 401

    def test_login_nonexistent_user(self, client):
        resp = client.post(
            "/api/v1/auth/login",
            json={"email": "ghost@example.com", "password": "pass"},
        )
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Me (protected route)
# ---------------------------------------------------------------------------

class TestMe:
    def test_me_authenticated(self, client, auth_headers):
        resp = client.get("/api/v1/auth/me", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["email"] == "test@example.com"
        assert data["full_name"] == "Test User"
        assert data["role"] == "applicant"

    def test_me_no_token(self, client):
        resp = client.get("/api/v1/auth/me")
        assert resp.status_code == 401

    def test_me_invalid_token(self, client):
        resp = client.get(
            "/api/v1/auth/me",
            headers={"Authorization": "Bearer invalid-token-here"},
        )
        assert resp.status_code == 401

    def test_me_malformed_header(self, client):
        resp = client.get(
            "/api/v1/auth/me",
            headers={"Authorization": "Token abc123"},
        )
        assert resp.status_code == 401

    def test_user_routes_require_authentication(self, client):
        assert client.get("/api/v1/applicants").status_code == 401
        assert client.get(
            "/api/v1/assessments", params={"applicant_id": "missing"}
        ).status_code == 401
        assert client.post(
            "/api/v1/assessments/evaluate",
            json={"applicant_id": "missing", "program_id": "missing"},
        ).status_code == 401


# ---------------------------------------------------------------------------
# Applicant ownership
# ---------------------------------------------------------------------------

class TestOwnership:
    def test_applicant_created_with_user_id(self, client, auth_headers):
        resp = client.post(
            "/api/v1/applicants",
            json={"age": 30},
            headers=auth_headers,
        )
        assert resp.status_code == 201
        data = resp.json()
        assert "user_id" in data

    def test_user_sees_own_applicants(self, client, auth_headers):
        client.post("/api/v1/applicants", json={"age": 30}, headers=auth_headers)
        client.post("/api/v1/applicants", json={"age": 25}, headers=auth_headers)
        resp = client.get("/api/v1/applicants", headers=auth_headers)
        assert resp.status_code == 200
        assert len(resp.json()) == 3

    def test_registration_creates_an_owned_applicant_profile(self, client):
        response = client.post(
            "/api/v1/auth/register",
            json={
                "email": "new-profile@example.com",
                "password": "password123",
                "full_name": "Asha Shah",
            },
        )
        assert response.status_code == 201
        headers = {"Authorization": f"Bearer {response.json()['access_token']}"}

        profiles = client.get("/api/v1/applicants", headers=headers)
        assert profiles.status_code == 200
        assert len(profiles.json()) == 1
        assert profiles.json()[0]["first_name"] == "Asha"
        assert profiles.json()[0]["last_name"] == "Shah"
        assert profiles.json()[0]["user_id"] == response.json()["user_id"]

    def test_login_backfills_profile_for_existing_applicant_account(self, client, db_engine):
        registration = client.post(
            "/api/v1/auth/register",
            json={
                "email": "existing-account@example.com",
                "password": "password123",
                "full_name": "Asha Shah",
            },
        )
        user_id = registration.json()["user_id"]
        with get_session_factory(db_engine)() as session:
            from db.models import ApplicantProfile

            session.query(ApplicantProfile).filter_by(user_id=user_id).delete()
            session.commit()

        login = client.post(
            "/api/v1/auth/login",
            json={"email": "existing-account@example.com", "password": "password123"},
        )
        assert login.status_code == 200
        profiles = client.get(
            "/api/v1/applicants",
            headers={"Authorization": f"Bearer {login.json()['access_token']}"},
        )
        assert profiles.status_code == 200
        assert len(profiles.json()) == 1
        assert profiles.json()[0]["first_name"] == "Asha"
        assert profiles.json()[0]["last_name"] == "Shah"

    def test_user_cannot_access_another_users_applicant(self, client, auth_headers):
        created = client.post(
            "/api/v1/applicants", json={"age": 30}, headers=auth_headers
        ).json()
        other = client.post(
            "/api/v1/auth/register",
            json={"email": "other@example.com", "password": "secret123"},
        ).json()
        other_headers = {"Authorization": f"Bearer {other['access_token']}"}
        applicant_id = created["applicant_id"]

        assert client.get(
            f"/api/v1/applicants/{applicant_id}", headers=other_headers
        ).status_code == 403
        assert client.patch(
            f"/api/v1/applicants/{applicant_id}",
            json={"age": 31},
            headers=other_headers,
        ).status_code == 403
