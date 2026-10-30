"""Consultancy tenant membership and application-case isolation tests."""
from __future__ import annotations

import hashlib
import io
import zipfile

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from api.main import app
from db.connection import get_session_factory
from db.models import (
    ApiUser,
    ApplicationCase,
    Assessment,
    Base,
    Country,
    Gap,
    ImmigrationProgram,
    TenantInvitation,
)
from services.case_document_storage import CaseDocumentStorage


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
        session.add(ImmigrationProgram(
            program_id="SKILLED_189",
            country_id="AU",
            visa_category="SKILLED",
            subclass_code="189",
            name="Skilled Independent visa",
            status="ACTIVE",
        ))
        session.commit()
    return database


@pytest.fixture()
def client(engine):
    app.state.db_engine = engine
    with TestClient(app) as test_client:
        yield test_client


def register_user(client, email: str, name: str) -> dict[str, str]:
    registered = client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": "password123", "full_name": name},
    )
    assert registered.status_code == 201
    return {"Authorization": f"Bearer {registered.json()['access_token']}"}


def create_tenant(client, headers, slug):
    response = client.post(
        "/api/v1/tenants",
        headers=headers,
        json={"name": slug.replace("-", " ").title(), "slug": slug},
    )
    assert response.status_code == 201
    return response.json()


def add_member(client, headers, tenant_id, email, role):
    response = client.post(
        f"/api/v1/tenants/{tenant_id}/members",
        headers=headers,
        json={"email": email, "role": role},
    )
    assert response.status_code == 201
    return response.json()


def test_tenant_creation_is_private_and_membership_scoped(client):
    owner_a = register_user(client, "tenant-a@example.com", "Owner A")
    owner_b = register_user(client, "tenant-b@example.com", "Owner B")
    tenant_a = create_tenant(client, owner_a, "agency-a")
    tenant_b = create_tenant(client, owner_b, "agency-b")

    visible_tenants = client.get("/api/v1/tenants", headers=owner_a).json()
    assert [tenant["tenant_id"] for tenant in visible_tenants] == [
        tenant_a["tenant_id"],
    ]
    assert visible_tenants[0]["role"] == "OWNER"

    hidden_members = client.get(
        f"/api/v1/tenants/{tenant_b['tenant_id']}/members",
        headers=owner_a,
    )
    assert hidden_members.status_code == 404


def test_only_tenant_owner_can_add_members(client):
    owner = register_user(client, "owner@example.com", "Owner")
    consultant = register_user(client, "consultant@example.com", "Consultant")
    tenant = create_tenant(client, owner, "consultancy")
    added = add_member(
        client, owner, tenant["tenant_id"], "consultant@example.com", "CONSULTANT",
    )

    assert added["role"] == "CONSULTANT"
    denied = client.post(
        f"/api/v1/tenants/{tenant['tenant_id']}/members",
        headers=consultant,
        json={"email": "owner@example.com", "role": "APPLICANT"},
    )
    assert denied.status_code == 403


def test_applicant_cannot_read_consultancy_member_roster(client):
    owner = register_user(client, "roster-owner@example.com", "Owner")
    applicant = register_user(client, "roster-applicant@example.com", "Applicant")
    tenant = create_tenant(client, owner, "roster-agency")
    add_member(
        client, owner, tenant["tenant_id"],
        "roster-applicant@example.com", "APPLICANT",
    )

    response = client.get(
        f"/api/v1/tenants/{tenant['tenant_id']}/members",
        headers=applicant,
    )

    assert response.status_code == 403


def test_application_case_is_visible_only_to_its_tenant_members(client):
    owner_a = register_user(client, "owner-a@example.com", "Owner A")
    owner_b = register_user(client, "owner-b@example.com", "Owner B")
    applicant = register_user(client, "applicant@example.com", "Applicant")
    consultant_a = register_user(client, "consultant-a@example.com", "Consultant A")
    consultant_b = register_user(client, "consultant-b@example.com", "Consultant B")
    tenant_a = create_tenant(client, owner_a, "case-agency-a")
    tenant_b = create_tenant(client, owner_b, "case-agency-b")

    add_member(
        client, owner_a, tenant_a["tenant_id"], "applicant@example.com", "APPLICANT",
    )
    add_member(
        client, owner_a, tenant_a["tenant_id"], "consultant-a@example.com", "CONSULTANT",
    )
    add_member(
        client, owner_b, tenant_b["tenant_id"], "consultant-b@example.com", "CONSULTANT",
    )

    applicant_profiles = client.get("/api/v1/applicants", headers=applicant).json()
    applicant_id = applicant_profiles[0]["applicant_id"]
    created = client.post(
        f"/api/v1/tenants/{tenant_a['tenant_id']}/cases",
        headers=consultant_a,
        json={
            "applicant_id": applicant_id,
            "program_id": "SKILLED_189",
            "title": "189 pathway",
        },
    )
    assert created.status_code == 201
    case = created.json()
    assert case["status"] == "DRAFT"
    assert case["assigned_consultant_id"]

    assert client.get(
        f"/api/v1/cases/{case['case_id']}", headers=consultant_a,
    ).status_code == 200
    assert client.get(
        f"/api/v1/cases/{case['case_id']}", headers=owner_a,
    ).status_code == 200
    assert client.get(
        f"/api/v1/cases/{case['case_id']}", headers=applicant,
    ).status_code == 200
    applicant_cases = client.get(
        f"/api/v1/applicants/{applicant_id}/cases",
        headers=applicant,
    )
    assert applicant_cases.status_code == 200
    assert applicant_cases.json()[0]["case_id"] == case["case_id"]
    assert applicant_cases.json()[0]["tenant_name"] == "Case Agency A"
    assert client.get(
        f"/api/v1/applicants/{applicant_id}/cases",
        headers=consultant_a,
    ).status_code == 403

    in_progress = client.patch(
        f"/api/v1/cases/{case['case_id']}/status",
        headers=applicant,
        json={"status": "IN_PROGRESS"},
    )
    assert in_progress.status_code == 200
    ready_for_review = client.patch(
        f"/api/v1/cases/{case['case_id']}/status",
        headers=applicant,
        json={"status": "READY_FOR_REVIEW"},
    )
    assert ready_for_review.status_code == 200
    approved = client.patch(
        f"/api/v1/cases/{case['case_id']}/status",
        headers=consultant_a,
        json={"status": "APPROVED"},
    )
    assert approved.status_code == 200
    applicant_cannot_approve_package = client.patch(
        f"/api/v1/cases/{case['case_id']}/status",
        headers=applicant,
        json={"status": "PACKAGE_READY"},
    )
    assert applicant_cannot_approve_package.status_code == 409

    cross_tenant = client.get(
        f"/api/v1/cases/{case['case_id']}", headers=consultant_b,
    )
    assert cross_tenant.status_code == 404
    assert client.get(
        f"/api/v1/tenants/{tenant_b['tenant_id']}/cases",
        headers=consultant_b,
    ).json() == []


def test_case_documents_are_private_validated_and_tenant_scoped(
    client, monkeypatch, tmp_path,
):
    monkeypatch.setenv("APPLICANT_DOCUMENT_DIR", str(tmp_path))
    owner = register_user(client, "document-owner@example.com", "Owner")
    applicant = register_user(client, "document-applicant@example.com", "Applicant")
    other_user = register_user(client, "document-other@example.com", "Other")
    tenant = create_tenant(client, owner, "document-agency")
    add_member(
        client, owner, tenant["tenant_id"],
        "document-applicant@example.com", "APPLICANT",
    )
    add_member(
        client, owner, tenant["tenant_id"],
        "document-other@example.com", "CONSULTANT",
    )
    applicant_id = client.get(
        "/api/v1/applicants", headers=applicant,
    ).json()[0]["applicant_id"]
    case = client.post(
        f"/api/v1/tenants/{tenant['tenant_id']}/cases",
        headers=owner,
        json={"applicant_id": applicant_id, "program_id": "SKILLED_189"},
    ).json()
    case_id = case["case_id"]

    uploaded = client.post(
        f"/api/v1/cases/{case_id}/documents?filename=passport.pdf",
        content=b"%PDF-1.7\npassport",
        headers={"Authorization": applicant["Authorization"], "Content-Type": "application/pdf"},
    )
    assert uploaded.status_code == 201
    document = uploaded.json()
    assert document["review_status"] == "PENDING_REVIEW"
    assert document["uploaded_by"]
    assert document["sha256"]

    listed = client.get(
        f"/api/v1/cases/{case_id}/documents", headers=owner,
    )
    assert [item["document_id"] for item in listed.json()] == [
        document["document_id"],
    ]
    downloaded = client.get(
        f"/api/v1/cases/{case_id}/documents/{document['document_id']}",
        headers=owner,
    )
    assert downloaded.status_code == 200
    assert downloaded.content == b"%PDF-1.7\npassport"
    assert downloaded.headers["cache-control"] == "private, no-store"

    cross_tenant = client.get(
        f"/api/v1/cases/{case_id}/documents/{document['document_id']}",
        headers=other_user,
    )
    assert cross_tenant.status_code == 404
    invalid = client.post(
        f"/api/v1/cases/{case_id}/documents?filename=spoofed.pdf",
        content=b"not a pdf",
        headers={
            "Authorization": applicant["Authorization"],
            "Content-Type": "application/pdf",
        },
    )
    assert invalid.status_code == 415


def test_document_review_requires_tenant_staff_and_delete_is_scoped(
    client, monkeypatch, tmp_path,
):
    monkeypatch.setenv("APPLICANT_DOCUMENT_DIR", str(tmp_path))
    owner = register_user(client, "document-review-owner@example.com", "Owner")
    applicant = register_user(
        client, "document-review-applicant@example.com", "Applicant",
    )
    consultant = register_user(
        client, "document-review-consultant@example.com", "Consultant",
    )
    tenant = create_tenant(client, owner, "document-review-agency")
    add_member(
        client, owner, tenant["tenant_id"],
        "document-review-applicant@example.com", "APPLICANT",
    )
    add_member(
        client, owner, tenant["tenant_id"],
        "document-review-consultant@example.com", "CONSULTANT",
    )
    applicant_id = client.get(
        "/api/v1/applicants", headers=applicant,
    ).json()[0]["applicant_id"]
    case = client.post(
        f"/api/v1/tenants/{tenant['tenant_id']}/cases",
        headers=consultant,
        json={"applicant_id": applicant_id, "program_id": "SKILLED_189"},
    ).json()
    case_id = case["case_id"]
    document = client.post(
        f"/api/v1/cases/{case_id}/documents?filename=identity.png",
        content=b"\x89PNG\r\n\x1a\nimage",
        headers={
            "Authorization": applicant["Authorization"],
            "Content-Type": "image/png",
        },
    ).json()

    denied_review = client.patch(
        f"/api/v1/cases/{case_id}/documents/{document['document_id']}/review",
        headers=applicant,
        json={"review_status": "ACCEPTED"},
    )
    assert denied_review.status_code == 403
    reviewed = client.patch(
        f"/api/v1/cases/{case_id}/documents/{document['document_id']}/review",
        headers=consultant,
        json={"review_status": "ACCEPTED"},
    )
    assert reviewed.status_code == 200
    assert reviewed.json()["review_status"] == "ACCEPTED"

    denied_delete = client.delete(
        f"/api/v1/cases/{case_id}/documents/{document['document_id']}",
        headers=consultant,
    )
    assert denied_delete.status_code == 403
    deleted = client.delete(
        f"/api/v1/cases/{case_id}/documents/{document['document_id']}",
        headers=applicant,
    )
    assert deleted.status_code == 204
    assert client.get(
        f"/api/v1/cases/{case_id}/documents/{document['document_id']}",
        headers=owner,
    ).status_code == 404


def test_s3_document_storage_signs_private_encrypted_requests(monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setenv("APPLICANT_DOCUMENT_STORAGE", "s3")
    monkeypatch.setenv("APPLICANT_DOCUMENT_S3_BUCKET", "private-documents")
    monkeypatch.setenv("APPLICANT_DOCUMENT_S3_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test-access-key")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test-secret-key")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "test-session-token")
    requests_seen = []

    def fake_request(method, url, **kwargs):
        requests_seen.append((method, url, kwargs))
        return SimpleNamespace(
            content=b"stored-data",
            raise_for_status=lambda: None,
        )

    monkeypatch.setattr(
        "services.case_document_storage.requests.request",
        fake_request,
    )
    storage = CaseDocumentStorage()
    tenant_id = "12345678-1234-1234-1234-123456789abc"
    key = storage.store(tenant_id, b"private-data", "application/pdf")

    assert key.startswith(f"{tenant_id}/")
    method, url, kwargs = requests_seen[0]
    assert method == "PUT"
    assert url.startswith("https://s3.amazonaws.com/private-documents/")
    assert kwargs["headers"]["x-amz-server-side-encryption"] == "AES256"
    assert kwargs["headers"]["x-amz-security-token"] == "test-session-token"
    assert "SignedHeaders=" in kwargs["headers"]["Authorization"]
    assert storage.retrieve(key) == b"stored-data"
    storage.delete(key)
    assert [call[0] for call in requests_seen] == ["PUT", "GET", "DELETE"]


def test_applicant_cannot_create_case_for_another_profile(client):
    owner = register_user(client, "owner-case@example.com", "Owner")
    applicant = register_user(client, "case-applicant@example.com", "Applicant")
    tenant = create_tenant(client, owner, "applicant-case-agency")
    add_member(
        client, owner, tenant["tenant_id"],
        "case-applicant@example.com", "APPLICANT",
    )
    other_profiles = client.get("/api/v1/applicants", headers=owner).json()

    response = client.post(
        f"/api/v1/tenants/{tenant['tenant_id']}/cases",
        headers=applicant,
        json={
            "applicant_id": other_profiles[0]["applicant_id"],
            "program_id": "SKILLED_189",
        },
    )
    assert response.status_code == 403


def test_admin_applicant_detail_connects_tenant_and_case_records(client, engine):
    admin = register_user(client, "tenant-admin-view@example.com", "Admin")
    applicant = register_user(
        client, "tenant-admin-view-applicant@example.com", "Applicant",
    )
    tenant = create_tenant(client, admin, "admin-view-agency")
    add_member(
        client, admin, tenant["tenant_id"],
        "tenant-admin-view-applicant@example.com", "APPLICANT",
    )
    applicant_id = client.get(
        "/api/v1/applicants", headers=applicant,
    ).json()[0]["applicant_id"]
    created_case = client.post(
        f"/api/v1/tenants/{tenant['tenant_id']}/cases",
        headers=admin,
        json={
            "applicant_id": applicant_id,
            "program_id": "SKILLED_189",
            "title": "Skilled pathway review",
        },
    )
    assert created_case.status_code == 201

    with get_session_factory(engine)() as session:
        user = session.query(ApiUser).filter_by(
            email="tenant-admin-view@example.com",
        ).one()
        user.role = "admin"
        session.commit()

    detail = client.get(
        f"/api/v1/admin/applicants/{applicant_id}",
        headers=admin,
    )

    assert detail.status_code == 200
    assert detail.json()["tenant_memberships"] == [{
        "tenant_id": tenant["tenant_id"],
        "tenant_name": "Admin View Agency",
        "role": "APPLICANT",
        "status": "ACTIVE",
    }]
    assert detail.json()["cases"][0]["title"] == "Skilled pathway review"
    assert detail.json()["cases"][0]["tenant_id"] == tenant["tenant_id"]


def test_consultant_can_claim_only_unassigned_case_in_own_tenant(client):
    owner = register_user(client, "claim-owner@example.com", "Owner")
    applicant = register_user(client, "claim-applicant@example.com", "Applicant")
    consultant = register_user(client, "claim-consultant@example.com", "Consultant")
    tenant = create_tenant(client, owner, "claim-agency")
    add_member(client, owner, tenant["tenant_id"], "claim-applicant@example.com", "APPLICANT")
    add_member(client, owner, tenant["tenant_id"], "claim-consultant@example.com", "CONSULTANT")
    applicant_id = client.get("/api/v1/applicants", headers=applicant).json()[0]["applicant_id"]
    case = client.post(
        f"/api/v1/tenants/{tenant['tenant_id']}/cases",
        headers=owner,
        json={"applicant_id": applicant_id, "program_id": "SKILLED_189"},
    ).json()

    queue = client.get(
        f"/api/v1/tenants/{tenant['tenant_id']}/unassigned-cases",
        headers=consultant,
    )
    assert queue.status_code == 200
    assert queue.json() == [{
        "case_id": case["case_id"],
        "tenant_id": tenant["tenant_id"],
        "program_id": "SKILLED_189",
        "status": "DRAFT",
        "title": None,
        "created_at": queue.json()[0]["created_at"],
    }]
    assert "applicant_id" not in queue.json()[0]

    consultant_id = client.get("/api/v1/auth/me", headers=consultant).json()["user_id"]
    claimed = client.post(
        f"/api/v1/tenants/{tenant['tenant_id']}/cases/{case['case_id']}/assign",
        headers=consultant,
        json={"consultant_id": consultant_id},
    )
    assert claimed.status_code == 200
    assert claimed.json()["assigned_consultant_id"] == consultant_id
    assert client.get(
        f"/api/v1/tenants/{tenant['tenant_id']}/unassigned-cases",
        headers=consultant,
    ).json() == []


def test_tenant_invitation_is_hashed_single_use_and_email_bound(client, engine):
    owner = register_user(client, "invite-owner@example.com", "Owner")
    invitee = register_user(client, "invitee@example.com", "Invitee")
    wrong_user = register_user(client, "wrong-invitee@example.com", "Wrong User")
    tenant = create_tenant(client, owner, "invite-agency")

    created = client.post(
        f"/api/v1/tenants/{tenant['tenant_id']}/invitations",
        headers=owner,
        json={"email": "INVITEE@example.com", "role": "CONSULTANT"},
    )
    assert created.status_code == 201
    invitation = created.json()
    token = invitation["invitation_token"]
    assert token
    assert invitation["email"] == "invitee@example.com"

    with get_session_factory(engine)() as session:
        stored = session.get(TenantInvitation, invitation["invitation_id"])
        assert stored.token_hash == hashlib.sha256(token.encode()).hexdigest()
        assert stored.token_hash != token

    denied = client.post(
        "/api/v1/tenants/invitations/accept",
        headers=wrong_user,
        json={"token": token},
    )
    assert denied.status_code == 403
    accepted = client.post(
        "/api/v1/tenants/invitations/accept",
        headers=invitee,
        json={"token": token},
    )
    assert accepted.status_code == 200
    assert accepted.json()["role"] == "CONSULTANT"
    duplicate = client.post(
        "/api/v1/tenants/invitations/accept",
        headers=invitee,
        json={"token": token},
    )
    assert duplicate.status_code == 409


def test_form_answers_require_valid_data_and_applicant_confirmation(client):
    owner = register_user(client, "form-owner@example.com", "Owner")
    applicant = register_user(client, "form-applicant@example.com", "Applicant")
    other_owner = register_user(client, "form-other-owner@example.com", "Other Owner")
    tenant = create_tenant(client, owner, "form-agency")
    other_tenant = create_tenant(client, other_owner, "other-form-agency")
    add_member(client, owner, tenant["tenant_id"], "form-applicant@example.com", "APPLICANT")
    applicant_id = client.get("/api/v1/applicants", headers=applicant).json()[0]["applicant_id"]
    case = client.post(
        f"/api/v1/tenants/{tenant['tenant_id']}/cases",
        headers=owner,
        json={"applicant_id": applicant_id, "program_id": "SKILLED_189"},
    ).json()
    template = client.post(
        f"/api/v1/tenants/{tenant['tenant_id']}/form-templates",
        headers=owner,
        json={
            "form_key": "applicant-details",
            "version": 1,
            "title": "Applicant details",
            "program_id": "SKILLED_189",
            "schema_json": {
                "questions": [
                    {"key": "full_name", "label": "Full name", "type": "text", "required": True},
                    {"key": "age", "label": "Age", "type": "number", "required": True},
                ],
            },
        },
    )
    assert template.status_code == 201
    template_id = template.json()["template_id"]
    assert template.json()["tenant_id"] == tenant["tenant_id"]
    assert client.get(
        f"/api/v1/tenants/{other_tenant['tenant_id']}/form-templates",
        headers=other_owner,
    ).json() == []
    same_key_in_other_tenant = client.post(
        f"/api/v1/tenants/{other_tenant['tenant_id']}/form-templates",
        headers=other_owner,
        json={
            "form_key": "applicant-details",
            "version": 1,
            "title": "Other tenant form",
            "program_id": "SKILLED_189",
            "schema_json": {"questions": []},
        },
    )
    assert same_key_in_other_tenant.status_code == 201

    invalid = client.post(
        f"/api/v1/cases/{case['case_id']}/forms/{template_id}",
        headers=applicant,
        json={"answers_json": {"age": "not-a-number"}},
    )
    assert invalid.status_code == 422
    missing = client.post(
        f"/api/v1/cases/{case['case_id']}/forms/{template_id}/submit",
        headers=applicant,
    )
    assert missing.status_code == 422

    saved = client.post(
        f"/api/v1/cases/{case['case_id']}/forms/{template_id}",
        headers=applicant,
        json={"answers_json": {"full_name": "Applicant", "age": 31}},
    )
    assert saved.status_code == 200
    submitted = client.post(
        f"/api/v1/cases/{case['case_id']}/forms/{template_id}/submit",
        headers=applicant,
    )
    assert submitted.status_code == 200
    assert submitted.json()["status"] == "SUBMITTED"
    premature_approval = client.post(
        f"/api/v1/cases/{case['case_id']}/forms/{template_id}/review",
        headers=owner,
        json={"status": "APPROVED"},
    )
    assert premature_approval.status_code == 409
    confirmed = client.post(
        f"/api/v1/cases/{case['case_id']}/forms/{template_id}/confirm",
        headers=applicant,
        json={},
    )
    assert confirmed.status_code == 200
    approved = client.post(
        f"/api/v1/cases/{case['case_id']}/forms/{template_id}/review",
        headers=owner,
        json={"status": "APPROVED"},
    )
    assert approved.status_code == 200
    assert approved.json()["status"] == "APPROVED"


def test_case_assessment_is_available_only_to_case_members(client, engine):
    owner = register_user(client, "assessment-owner@example.com", "Owner")
    applicant = register_user(client, "assessment-applicant@example.com", "Applicant")
    outsider = register_user(client, "assessment-outsider@example.com", "Outsider")
    tenant = create_tenant(client, owner, "assessment-agency")
    add_member(
        client, owner, tenant["tenant_id"], "assessment-applicant@example.com", "APPLICANT",
    )
    applicant_id = client.get("/api/v1/applicants", headers=applicant).json()[0]["applicant_id"]
    case = client.post(
        f"/api/v1/tenants/{tenant['tenant_id']}/cases",
        headers=owner,
        json={"applicant_id": applicant_id, "program_id": "SKILLED_189"},
    ).json()
    with get_session_factory(engine)() as session:
        assessment = Assessment(
            applicant_id=applicant_id,
            program_id="SKILLED_189",
            business_status="INCOMPLETE",
            overall_status="UNKNOWN",
            total_rules=1,
            passed_count=0,
            failed_count=0,
            unknown_count=1,
            not_applicable_count=0,
        )
        session.add(assessment)
        session.flush()
        linked_case = session.get(ApplicationCase, case["case_id"])
        linked_case.assessment_id = assessment.assessment_id
        session.add(Gap(
            assessment_id=assessment.assessment_id,
            field="age",
            priority="HIGH",
            message="Provide your age.",
        ))
        session.commit()
        assessment_id = assessment.assessment_id

    owner_result = client.get(
        f"/api/v1/cases/{case['case_id']}/assessment",
        headers=owner,
    )
    applicant_result = client.get(
        f"/api/v1/cases/{case['case_id']}/assessment",
        headers=applicant,
    )
    denied = client.get(
        f"/api/v1/cases/{case['case_id']}/assessment",
        headers=outsider,
    )
    assert owner_result.status_code == 200
    assert owner_result.json()["assessment_id"] == assessment_id
    assert owner_result.json()["gaps"][0]["field"] == "age"
    assert applicant_result.status_code == 200
    assert denied.status_code == 404


def test_upload_scanner_blocks_infected_files_and_reports_clean_status(
    client, monkeypatch, tmp_path,
):
    from services.case_document_scanner import DocumentScanResult

    class Scanner:
        def __init__(self, status):
            self.status = status

        def scan(self, content):
            return DocumentScanResult(self.status, "test scan")

    monkeypatch.setenv("APPLICANT_DOCUMENT_DIR", str(tmp_path))
    owner = register_user(client, "scan-owner@example.com", "Owner")
    applicant = register_user(client, "scan-applicant@example.com", "Applicant")
    tenant = create_tenant(client, owner, "scan-agency")
    add_member(client, owner, tenant["tenant_id"], "scan-applicant@example.com", "APPLICANT")
    applicant_id = client.get("/api/v1/applicants", headers=applicant).json()[0]["applicant_id"]
    case = client.post(
        f"/api/v1/tenants/{tenant['tenant_id']}/cases",
        headers=owner,
        json={"applicant_id": applicant_id, "program_id": "SKILLED_189"},
    ).json()
    upload_url = f"/api/v1/cases/{case['case_id']}/documents?filename=passport.pdf"
    upload_headers = {
        "Authorization": applicant["Authorization"],
        "Content-Type": "application/pdf",
    }

    monkeypatch.setattr(app.state, "document_scanner", Scanner("INFECTED"), raising=False)
    infected = client.post(upload_url, content=b"%PDF-1.7\nmalware", headers=upload_headers)
    assert infected.status_code == 422
    assert client.get(
        f"/api/v1/cases/{case['case_id']}/documents", headers=owner,
    ).json() == []

    monkeypatch.setattr(app.state, "document_scanner", Scanner("CLEAN"), raising=False)
    clean = client.post(upload_url, content=b"%PDF-1.7\nclean", headers=upload_headers)
    assert clean.status_code == 201
    assert clean.json()["scanner_status"] == "CLEAN"
    assert clean.json()["scanner_detail"] == "test scan"


def test_application_package_contains_summary_and_accepted_documents(
    client, monkeypatch, tmp_path,
):
    monkeypatch.setenv("APPLICANT_DOCUMENT_DIR", str(tmp_path))
    owner = register_user(client, "package-owner@example.com", "Owner")
    applicant = register_user(client, "package-applicant@example.com", "Applicant")
    tenant = create_tenant(client, owner, "package-agency")
    add_member(client, owner, tenant["tenant_id"], "package-applicant@example.com", "APPLICANT")
    applicant_id = client.get("/api/v1/applicants", headers=applicant).json()[0]["applicant_id"]
    case = client.post(
        f"/api/v1/tenants/{tenant['tenant_id']}/cases",
        headers=owner,
        json={"applicant_id": applicant_id, "program_id": "SKILLED_189"},
    ).json()
    uploaded = client.post(
        f"/api/v1/cases/{case['case_id']}/documents?filename=passport.pdf",
        content=b"%PDF-1.7\npassport evidence",
        headers={
            "Authorization": applicant["Authorization"],
            "Content-Type": "application/pdf",
        },
    )
    assert uploaded.status_code == 201
    approved = client.patch(
        f"/api/v1/cases/{case['case_id']}/documents/{uploaded.json()['document_id']}/review",
        headers=owner,
        json={"review_status": "ACCEPTED"},
    )
    assert approved.status_code == 200

    package = client.get(f"/api/v1/cases/{case['case_id']}/package", headers=owner)
    assert package.status_code == 200
    assert package.headers["content-type"] == "application/zip"
    with zipfile.ZipFile(io.BytesIO(package.content)) as archive:
        assert "case-summary.pdf" in archive.namelist()
        assert archive.read("case-summary.pdf").startswith(b"%PDF-")
        document_path = next(name for name in archive.namelist() if name.startswith("documents/"))
        assert archive.read(document_path) == b"%PDF-1.7\npassport evidence"
        manifest = archive.read("manifest.json").decode("utf-8")
        assert '"included_in_package": true' in manifest
