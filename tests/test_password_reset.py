from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from api.main import app
from db.connection import get_session_factory
from db.models import Base, Country, ImmigrationProgram


@pytest.fixture()
def client():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    with get_session_factory(engine)() as session:
        session.add(Country(country_id="AU", name="Australia", iso_code="AUS"))
        session.add(ImmigrationProgram(
            program_id="SKILLED_189",
            country_id="AU",
            visa_category="SKILLED",
            subclass_code="189",
            name="Skilled Independent visa",
            status="ACTIVE",
        ))
        session.commit()
    app.state.db_engine = engine
    with TestClient(app) as test_client:
        yield test_client
    engine.dispose()


def register(client: TestClient, email="profile@example.com"):
    response = client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": "password123", "full_name": "Applicant"},
    )
    assert response.status_code == 201
    return response.json()["access_token"]


def test_applicant_profile_endpoint_saves_and_lists_full_personal_details(client):
    token = register(client)
    response = client.post(
        "/api/v1/applicants",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "first_name": "Asha",
            "last_name": "Shah",
            "date_of_birth": "1994-06-08T00:00:00Z",
            "passport_nationality": "IND",
            "current_visa_subclass": "500",
            "occupation": "Engineer",
        },
    )

    assert response.status_code == 201
    profile = response.json()
    assert profile["date_of_birth"].startswith("1994-06-08")
    assert profile["passport_nationality"] == "IND"
    assert profile["current_visa_subclass"] == "500"

    listed = client.get(
        "/api/v1/applicants",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert listed.status_code == 200
    listed_profile = next(
        entry for entry in listed.json()
        if entry["applicant_id"] == profile["applicant_id"]
    )
    assert listed_profile["passport_nationality"] == "IND"
    updated = client.patch(
        f"/api/v1/applicants/{profile['applicant_id']}",
        headers={"Authorization": f"Bearer {token}"},
        json={"current_visa_subclass": "485"},
    )
    assert updated.status_code == 200
    assert updated.json()["current_visa_subclass"] == "485"


def test_password_reset_email_is_generic_for_unknown_accounts(client, monkeypatch):
    monkeypatch.setenv("SMTP_HOST", "mail.example.test")
    monkeypatch.setenv("SMTP_FROM", "Pathway <no-reply@example.test>")
    monkeypatch.setattr("api.routes.auth.send_password_reset_email", lambda *_: None)
    register(client)

    existing = client.post(
        "/api/v1/auth/password-reset/request",
        json={"email": "profile@example.com"},
    )
    unknown = client.post(
        "/api/v1/auth/password-reset/request",
        json={"email": "missing@example.com"},
    )

    assert existing.status_code == 200
    assert unknown.status_code == 200
    assert existing.json() == unknown.json()


def test_password_reset_link_is_one_use_and_updates_login_credentials(client, monkeypatch):
    monkeypatch.setenv("SMTP_HOST", "mail.example.test")
    monkeypatch.setenv("SMTP_FROM", "Pathway <no-reply@example.test>")
    monkeypatch.setenv("APP_PUBLIC_URL", "http://localhost:5173")
    delivered = {}

    def capture_email(email, token):
        delivered["email"] = email
        delivered["token"] = token

    monkeypatch.setattr("api.routes.auth.send_password_reset_email", capture_email)
    token_before_reset = register(client)

    requested = client.post(
        "/api/v1/auth/password-reset/request",
        json={"email": "profile@example.com"},
    )
    assert requested.status_code == 200
    assert delivered["email"] == "profile@example.com"

    changed = client.post(
        "/api/v1/auth/password-reset/confirm",
        json={"token": delivered["token"], "password": "changedpass123"},
    )
    assert changed.status_code == 200
    assert client.post(
        "/api/v1/auth/login",
        json={"email": "profile@example.com", "password": "password123"},
    ).status_code == 401
    assert client.post(
        "/api/v1/auth/login",
        json={"email": "profile@example.com", "password": "changedpass123"},
    ).status_code == 200
    assert client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {token_before_reset}"},
    ).status_code == 401
    reused = client.post(
        "/api/v1/auth/password-reset/confirm",
        json={"token": delivered["token"], "password": "changedpass123"},
    )
    assert reused.status_code == 400


def test_password_reset_is_explicitly_disabled_without_smtp(client, monkeypatch):
    monkeypatch.delenv("SMTP_HOST", raising=False)
    monkeypatch.delenv("SMTP_FROM", raising=False)
    response = client.post(
        "/api/v1/auth/password-reset/request",
        json={"email": "profile@example.com"},
    )

    assert response.status_code == 503
    assert "SMTP_HOST" in response.json()["detail"]
