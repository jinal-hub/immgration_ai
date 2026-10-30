"""Applicant-facing visa pathway list and published-rule availability."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from api.main import app
from db.connection import get_session_factory
from db.models import Base, Country, GovernmentSource, ImmigrationProgram, Rule


@pytest.fixture()
def engine():
    database = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=database)
    with get_session_factory(database)() as session:
        session.add(Country(country_id="AU", name="Australia", iso_code="AUS"))
        session.add_all([
            ImmigrationProgram(
                program_id="SKILLED_189",
                country_id="AU",
                visa_category="SKILLED",
                subclass_code="189",
                name="Skilled Independent visa",
                status="ACTIVE",
            ),
            ImmigrationProgram(
                program_id="STUDENT_500",
                country_id="AU",
                visa_category="STUDENT",
                subclass_code="500",
                name="Student visa",
                status="ACTIVE",
            ),
            ImmigrationProgram(
                program_id="HIDDEN_999",
                country_id="AU",
                visa_category="OTHER",
                subclass_code="999",
                name="Inactive pathway",
                status="INACTIVE",
            ),
            ImmigrationProgram(
                program_id="REFERENCE_820",
                country_id="AU",
                visa_category="WORK",
                subclass_code="820",
                name="Reference-only pathway",
                status="REFERENCE_ONLY",
            ),
        ])
        session.add(Rule(
            rule_id="RULE-189-AGE",
            requirement_id="REQ-189-AGE",
            program_id="SKILLED_189",
            category="AGE",
            field="age",
            operator="LT",
            value_json="45",
            mandatory=True,
            critical=False,
            message="Under 45",
            status="ACTIVE",
            current_version=1,
            source_id="SRC-AU-189-MAIN",
            evidence_text="Applicants must be under 45.",
        ))
        session.add(GovernmentSource(
            source_id="SRC-AU-189-MAIN",
            country_id="AU",
            program_id="SKILLED_189",
            visa_category="SKILLED",
            authority="Department of Home Affairs",
            url="https://immi.homeaffairs.gov.au/visas/skilled-independent-189",
            source_type="HTML",
            priority="HIGH",
            status="ACTIVE",
            fetch_status="OK",
            http_status=200,
        ))
        session.commit()
    return database


@pytest.fixture()
def client(engine):
    app.state.db_engine = engine
    with TestClient(app) as test_client:
        yield test_client


def _applicant_headers(client, email="pathways@example.com"):
    client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": "password123", "full_name": "Pathway User"},
    )
    response = client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": "password123"},
    )
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def test_program_list_includes_active_and_reference_visa_programs(client):
    response = client.get(
        "/api/v1/applicants/programs",
        headers=_applicant_headers(client),
    )

    assert response.status_code == 200
    assert [program["program_id"] for program in response.json()] == [
        "SKILLED_189",
        "STUDENT_500",
        "REFERENCE_820",
    ]
    assert response.json()[0]["active_rule_count"] == 1
    assert response.json()[1]["active_rule_count"] == 0
    assert response.json()[0]["status"] == "ACTIVE"
    assert response.json()[2]["status"] == "REFERENCE_ONLY"
    assert response.json()[2]["country_id"] == "AU"
    assert response.json()[0]["sources"] == [{
        "source_id": "SRC-AU-189-MAIN",
        "authority": "Department of Home Affairs",
        "url": "https://immi.homeaffairs.gov.au/visas/skilled-independent-189",
        "status": "ACTIVE",
        "fetch_status": "OK",
        "http_status": 200,
        "last_checked_at": None,
        "last_successful_fetch_at": None,
    }]


def test_visa_selection_is_persisted_idempotently_and_owner_scoped(client):
    headers = _applicant_headers(client)
    applicant = client.get("/api/v1/applicants", headers=headers).json()[0]
    selection_url = (
        f"/api/v1/applicants/{applicant['applicant_id']}/visa-selections"
    )

    saved = client.post(
        selection_url,
        headers=headers,
        json={"program_id": "SKILLED_189"},
    )
    assert saved.status_code == 201
    repeated = client.post(
        selection_url,
        headers=headers,
        json={"program_id": "SKILLED_189"},
    )
    assert repeated.status_code == 201
    assert repeated.json()["selection_id"] == saved.json()["selection_id"]
    assert repeated.json()["selected_at"] >= saved.json()["selected_at"]

    listed = client.get(selection_url, headers=headers)
    assert [item["program_id"] for item in listed.json()] == ["SKILLED_189"]

    other_headers = _applicant_headers(client, email="other-pathway@example.com")
    denied = client.get(selection_url, headers=other_headers)
    assert denied.status_code == 403
    denied_write = client.post(
        selection_url,
        headers=other_headers,
        json={"program_id": "SKILLED_189"},
    )
    assert denied_write.status_code == 403


def test_program_list_requires_an_authenticated_user(client):
    response = client.get("/api/v1/applicants/programs")

    assert response.status_code == 401


def test_date_of_birth_supplies_age_to_assessment_when_age_is_blank(client):
    headers = _applicant_headers(client)
    applicant = client.post(
        "/api/v1/applicants",
        headers=headers,
        json={"first_name": "Asha", "last_name": "Shah", "date_of_birth": "2000-10-29T00:00:00Z"},
    )
    assert applicant.status_code == 201

    assessment = client.post(
        "/api/v1/assessments",
        headers=headers,
        json={
            "applicant_id": applicant.json()["applicant_id"],
            "program_id": "SKILLED_189",
        },
    )

    assert assessment.status_code == 201
    assert assessment.json()["passed_count"] == 1
    assert assessment.json()["unknown_count"] == 0
    assert assessment.json()["applicant_snapshot"]["age"] == 26
    result = assessment.json()["rule_results"][0]
    assert result["source_id"] == "SRC-AU-189-MAIN"
    assert result["source_url"] == "https://immi.homeaffairs.gov.au/visas/skilled-independent-189"
    assert result["source_authority"] == "Department of Home Affairs"
    assert result["evidence_text"] == "Applicants must be under 45."


def _create_incomplete_age_assessment(client, headers):
    applicant = client.post(
        "/api/v1/applicants",
        headers=headers,
        json={"first_name": "Asha", "last_name": "Shah"},
    )
    assert applicant.status_code == 201
    assessment = client.post(
        "/api/v1/assessments",
        headers=headers,
        json={
            "applicant_id": applicant.json()["applicant_id"],
            "program_id": "SKILLED_189",
        },
    )
    assert assessment.status_code == 201
    assert assessment.json()["gaps"]
    return applicant.json(), assessment.json()


def test_applicant_can_answer_owned_assessment_gap_and_refresh_profile(client):
    headers = _applicant_headers(client)
    applicant, assessment = _create_incomplete_age_assessment(client, headers)
    gap = assessment["gaps"][0]

    response = client.post(
        f"/api/v1/assessments/{assessment['assessment_id']}/gaps/{gap['gap_id']}/answer",
        headers=headers,
        json={"value": 30},
    )

    assert response.status_code == 200
    assert response.json()["answer"] == 30
    assert response.json()["field"] == "age"

    refreshed = client.get(
        f"/api/v1/assessments/{assessment['assessment_id']}",
        headers=headers,
    )
    assert refreshed.status_code == 200
    assert refreshed.json()["gaps"][0]["answer"] == 30

    profiles = client.get("/api/v1/applicants", headers=headers).json()
    updated = next(
        profile for profile in profiles
        if profile["applicant_id"] == applicant["applicant_id"]
    )
    assert updated["age"] == 30


def test_applicant_cannot_answer_another_users_assessment_gap(client):
    owner_headers = _applicant_headers(client, "owner@example.com")
    _, assessment = _create_incomplete_age_assessment(client, owner_headers)
    gap = assessment["gaps"][0]
    other_headers = _applicant_headers(client, "other@example.com")

    response = client.post(
        f"/api/v1/assessments/{assessment['assessment_id']}/gaps/{gap['gap_id']}/answer",
        headers=other_headers,
        json={"value": 30},
    )

    assert response.status_code == 403


def test_invalid_gap_answer_is_rejected_without_profile_or_gap_changes(client):
    headers = _applicant_headers(client)
    applicant, assessment = _create_incomplete_age_assessment(client, headers)
    gap = assessment["gaps"][0]

    response = client.post(
        f"/api/v1/assessments/{assessment['assessment_id']}/gaps/{gap['gap_id']}/answer",
        headers=headers,
        json={"value": 121},
    )

    assert response.status_code == 422
    profiles = client.get("/api/v1/applicants", headers=headers).json()
    updated = next(
        profile for profile in profiles
        if profile["applicant_id"] == applicant["applicant_id"]
    )
    assert updated["age"] is None
    refreshed = client.get(
        f"/api/v1/assessments/{assessment['assessment_id']}",
        headers=headers,
    )
    assert refreshed.json()["gaps"][0]["answer"] is None
