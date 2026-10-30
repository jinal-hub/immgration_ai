"""Tests for the FastAPI application (Sprint P3)."""
from __future__ import annotations

import os
import threading
import time

import pytest
from unittest.mock import patch
from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from fastapi.testclient import TestClient

from db.models import Base
from api.main import app
from scheduler import _sync_lock


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


# ---------------------------------------------------------------------------
# G9 Regression: lifespan must not overwrite pre-set db_engine
# ---------------------------------------------------------------------------

class TestG9LifespanRegression:
    def test_preset_engine_survives_lifespan(self):
        """Pre-set app.state.db_engine is NOT overwritten by lifespan startup.

        Regression for G9: lifespan used to unconditionally re-initialize
        from DATABASE_URL, overriding test engines (or explicit None).
        """
        from api.main import create_app
        eng = create_engine(
            "sqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(bind=eng)
        app2 = create_app()
        app2.state.db_engine = eng
        with TestClient(app2) as c:
            # DB should be the test engine, not the production one
            assert app2.state.db_engine is eng
            resp = c.get("/ready")
            assert resp.status_code == 200
            assert resp.json()["db"] == "connected"

    def test_none_engine_survives_lifespan(self):
        """Pre-set db_engine=None is NOT overwritten to a real DB engine."""
        from api.main import create_app
        app2 = create_app()
        app2.state.db_engine = None
        with TestClient(app2, raise_server_exceptions=False) as c:
            assert app2.state.db_engine is None
            resp = c.get("/ready")
            assert resp.status_code == 503


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

class TestHealth:
    def test_health_liveness(self, client):
        """GET /health always returns 200 — process alive."""
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

    def test_readiness_ok(self, client):
        """GET /ready returns 200 when DB is connected."""
        resp = client.get("/ready")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["db"] == "connected"

    def test_readiness_degraded_without_db(self):
        """GET /ready returns 503 when DB is not configured."""
        from api.main import create_app
        app2 = create_app()
        app2.state.db_engine = None
        with TestClient(app2) as c:
            resp = c.get("/ready")
            assert resp.status_code == 503
            data = resp.json()
            assert data["status"] == "degraded"
            assert data["db"] == "disconnected"

    def test_readiness_degraded_includes_safe_db_diagnostics(self):
        """When the DB is disconnected, /ready reports configured backend/host
        WITHOUT leaking credentials, so an operator can self-diagnose."""
        from api.main import create_app
        app2 = create_app()
        app2.state.db_engine = None
        import os as _os
        from unittest.mock import patch
        with patch.dict(_os.environ, {
            "DATABASE_URL": "postgresql://immigration:SUPERSECRET@localhost:5432/immigration",
        }, clear=False):
            with TestClient(app2) as c:
                resp = c.get("/ready")
                assert resp.status_code == 503
                data = resp.json()
                assert data.get("configured_backend") == "postgresql"
                assert data.get("configured_host") == "localhost"
                body_str = resp.text
                assert "SUPERSECRET" not in body_str
                assert "immigration:" not in body_str

    def test_readiness_reports_scrubbed_db_error_detail_when_not_testing(self):
        """The last DB connection error shows a short, credential-free reason on
        /ready (outside TESTING) so the operator sees the underlying cause."""
        from unittest.mock import patch
        from db.connection import get_engine as _real_get_engine
        from api.main import create_app

        def _boom(*args, **kwargs):
            raise RuntimeError(
                "connection to postgresql://app:realpassword@postgres:5432 failed: "
                "FATAL: password authentication failed"
            )

        app2 = create_app()
        app2.state.db_engine = None
        with patch("db.connection.get_engine", side_effect=_boom), \
             patch.dict(os.environ, {
                 "TESTING": "false",
                 "DATABASE_URL": "postgresql://app:realpassword@postgres:5432/immigration",
             }, clear=False):
            with TestClient(app2) as c:
                resp = c.get("/ready")
                assert resp.status_code == 503
                data = resp.json()
                detail = data.get("db_error_detail", "") or ""
                assert "password authentication failed" in detail
                assert "realpassword" not in detail
                assert "app:***@postgres" in detail

    def test_readiness_suppresses_error_detail_under_testing(self):
        """Under TESTING=true the error detail is not emitted (CI-stable body)."""
        from unittest.mock import patch
        from api.main import create_app
        app2 = create_app()
        app2.state.db_engine = None
        app2.state.db_error = "boom"
        with patch.dict(os.environ, {
            "TESTING": "true",
            "DATABASE_URL": "postgresql://app:realpassword@postgres:5432/immigration",
        }, clear=False):
            with TestClient(app2) as c:
                resp = c.get("/ready")
                data = resp.json()
                assert "db_error_detail" not in data

    def test_readiness_happy_path_unaffected(self, client):
        """An initialized engine must keep returning 200/'connected' — the
        diagnostics changes must not affect the happy path."""
        resp = client.get("/ready")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["db"] == "connected"


# ---------------------------------------------------------------------------
# Applicants
# ---------------------------------------------------------------------------

class TestApplicants:
    def _auth_headers(self, client):
        """Register a test user and return auth headers."""
        client.post(
            "/api/v1/auth/register",
            json={"email": "api-test@example.com", "password": "pass123", "full_name": "API Test"},
        )
        resp = client.post(
            "/api/v1/auth/login",
            json={"email": "api-test@example.com", "password": "pass123"},
        )
        token = resp.json()["access_token"]
        return {"Authorization": f"Bearer {token}"}

    def test_create_applicant(self, client):
        headers = self._auth_headers(client)
        resp = client.post(
            "/api/v1/applicants",
            json={"age": 30, "english_level": "competent", "visa_subclass": "189"},
            headers=headers,
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["age"] == 30
        assert data["english_level"] == "competent"
        assert "applicant_id" in data

    def test_create_applicant_rejects_invalid_age_and_future_birth_date(self, client):
        headers = self._auth_headers(client)
        invalid_age = client.post(
            "/api/v1/applicants",
            json={"age": 121},
            headers=headers,
        )
        future_birth_date = client.post(
            "/api/v1/applicants",
            json={"date_of_birth": "2999-01-01T00:00:00Z"},
            headers=headers,
        )
        assert invalid_age.status_code == 422
        assert future_birth_date.status_code == 422

    def test_list_applicants(self, client):
        headers = self._auth_headers(client)
        client.post("/api/v1/applicants", json={"age": 30}, headers=headers)
        client.post("/api/v1/applicants", json={"age": 25}, headers=headers)
        resp = client.get("/api/v1/applicants", headers=headers)
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 3
        assert {applicant["age"] for applicant in data} == {None, 25, 30}

    def test_get_applicant(self, client):
        headers = self._auth_headers(client)
        create_resp = client.post(
            "/api/v1/applicants",
            json={"age": 30},
            headers=headers,
        )
        applicant_id = create_resp.json()["applicant_id"]
        resp = client.get(f"/api/v1/applicants/{applicant_id}", headers=headers)
        assert resp.status_code == 200
        assert resp.json()["age"] == 30

    def test_get_applicant_not_found(self, client):
        headers = self._auth_headers(client)
        resp = client.get("/api/v1/applicants/nonexistent", headers=headers)
        assert resp.status_code == 404

    def test_update_applicant(self, client):
        headers = self._auth_headers(client)
        create_resp = client.post(
            "/api/v1/applicants",
            json={"age": 30},
            headers=headers,
        )
        applicant_id = create_resp.json()["applicant_id"]
        resp = client.patch(
            f"/api/v1/applicants/{applicant_id}",
            json={"age": 31, "english_level": "proficient"},
            headers=headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["age"] == 31
        assert data["english_level"] == "proficient"

    def test_update_applicant_not_found(self, client):
        headers = self._auth_headers(client)
        resp = client.patch("/api/v1/applicants/nonexistent", json={"age": 31},
                            headers=headers)
        assert resp.status_code == 404

    def test_create_applicant_minimal(self, client):
        headers = self._auth_headers(client)
        resp = client.post(
            "/api/v1/applicants",
            json={},
            headers=headers,
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["age"] is None
        assert "applicant_id" in data


# ---------------------------------------------------------------------------
# Assessments (stub endpoints)
# ---------------------------------------------------------------------------

class TestAssessments:
    def _auth_headers(self, client):
        """Register a test user and return auth headers."""
        client.post(
            "/api/v1/auth/register",
            json={"email": "assess-test@example.com", "password": "pass123"},
        )
        resp = client.post(
            "/api/v1/auth/login",
            json={"email": "assess-test@example.com", "password": "pass123"},
        )
        token = resp.json()["access_token"]
        return {"Authorization": f"Bearer {token}"}

    def _setup_applicant_with_rules(self, client, headers):
        """Create an applicant and seed ACTIVE rules, return applicant_id."""
        from db.connection import get_session
        from db.models import Base, ExtractionRun, Requirement as RequirementModel
        from db.repositories import ProgramRepo, CountryRepo

        # Create applicant
        resp = client.post(
            "/api/v1/applicants",
            json={"age": 30, "english_level": "competent", "points": 65},
            headers=headers,
        )
        applicant_id = resp.json()["applicant_id"]

        # Seed rules into DB
        with get_session(client.app.state.db_engine) as session:
            # Ensure program exists
            country_repo = CountryRepo(session)
            country_repo.upsert("AU", "Australia", "AU")
            program_repo = ProgramRepo(session)
            program_repo.upsert(
                "SKILLED_189", "AU", "Skilled Independent", "189",
                "Skilled Independent visa",
            )

            # Create extraction run + requirement rows (FK prerequisite for Rule)
            er = ExtractionRun(
                run_id="ER-TEST-001",
                document_id="DOC-AU-189-MAIN",
                document_version=1,
                model="test",
                prompt_version="v1",
                status="COMPLETED",
            )
            session.add(er)
            for req_id, field, cat in [
                ("REQ-AGE-189", "age", "age"),
                ("REQ-POINTS-189", "points", "points"),
            ]:
                req = RequirementModel(
                    requirement_id=req_id,
                    semantic_key=f"test-{cat}-key",
                    program_id="SKILLED_189",
                    country_id="AU",
                    category=cat,
                    name=f"{cat} requirement",
                    field=field,
                    operator="LT",
                    status="APPROVED",
                    extraction_run_id=er.run_id,
                )
                session.add(req)
            session.flush()

            from rules.rule_repository import EnhancedRuleRepo
            rule_repo = EnhancedRuleRepo(session)

            # Create two ACTIVE rules
            rule_repo.create(
                rule_id="RULE-AGE-189",
                requirement_id="REQ-AGE-189",
                program_id="SKILLED_189",
                category="age",
                field="age",
                operator="LT",
                value_json="45",
                mandatory=True,
                critical=True,
                message="Age must be under 45",
                status="ACTIVE",
                current_version=1,
                source_id="SRC-AU-189-MAIN",
                document_id="DOC-AU-189-MAIN",
                document_version=1,
                chunk_id=1,
                evidence_text="Applicants must be under 45 years of age",
            )
            rule_repo.create(
                rule_id="RULE-POINTS-189",
                requirement_id="REQ-POINTS-189",
                program_id="SKILLED_189",
                category="points",
                field="points",
                operator="GTE",
                value_json="65",
                mandatory=True,
                critical=False,
                message="Must score at least 65 points",
                status="ACTIVE",
                current_version=1,
                source_id="SRC-AU-189-MAIN",
                document_id="DOC-AU-189-MAIN",
                document_version=1,
                chunk_id=2,
                evidence_text="Minimum 65 points on the points test",
            )
            session.commit()

        return applicant_id

    def test_create_assessment_real(self, client):
        """POST /assessments returns 201 with real eligibility result."""
        headers = self._auth_headers(client)
        applicant_id = self._setup_applicant_with_rules(client, headers)

        resp = client.post(
            "/api/v1/assessments",
            json={"applicant_id": applicant_id, "program_id": "SKILLED_189"},
            headers=headers,
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["business_status"] == "ELIGIBLE"
        assert data["overall_status"] == "PASS"
        assert data["total_rules"] == 2
        assert data["passed_count"] == 2
        assert data["failed_count"] == 0
        assert len(data["rule_results"]) == 2
        assert "assessment_id" in data

    def test_create_assessment_incomplete(self, client):
        """Assessment is INCOMPLETE when applicant data is missing."""
        from db.connection import get_session
        from db.repositories import ProgramRepo, CountryRepo
        from db.models import ExtractionRun, Requirement as RequirementModel
        from rules.rule_repository import EnhancedRuleRepo

        headers = self._auth_headers(client)

        # Create applicant with no data
        resp = client.post(
            "/api/v1/applicants",
            json={},
            headers=headers,
        )
        applicant_id = resp.json()["applicant_id"]

        # Seed a rule requiring english_level
        with get_session(client.app.state.db_engine) as session:
            CountryRepo(session).upsert("AU", "Australia", "AU")
            ProgramRepo(session).upsert(
                "SKILLED_189", "AU", "Skilled Independent", "189",
                "Skilled Independent visa",
            )
            er = ExtractionRun(
                run_id="ER-TEST-INCOMPL",
                document_id="DOC-AU-189-MAIN", document_version=1,
                model="test", prompt_version="v1", status="COMPLETED",
            )
            session.add(er)
            session.add(RequirementModel(
                requirement_id="REQ-ENGLISH-189", semantic_key="test-english-key",
                program_id="SKILLED_189", country_id="AU", category="english",
                name="English requirement", field="english_level", operator="IN",
                status="APPROVED", extraction_run_id=er.run_id,
            ))
            session.flush()

            rule_repo = EnhancedRuleRepo(session)
            rule_repo.create(
                rule_id="RULE-ENGLISH-189",
                requirement_id="REQ-ENGLISH-189",
                program_id="SKILLED_189",
                category="english",
                field="english_level",
                operator="IN",
                value_json='["competent","proficient","superior"]',
                mandatory=True,
                critical=True,
                message="Must have competent English or higher",
                status="ACTIVE",
                current_version=1,
                source_id="SRC-AU-189-MAIN",
                document_id="DOC-AU-189-MAIN",
                document_version=1,
                chunk_id=1,
                evidence_text="Competent English required",
            )
            session.commit()

        resp = client.post(
            "/api/v1/assessments",
            json={"applicant_id": applicant_id, "program_id": "SKILLED_189"},
            headers=headers,
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["business_status"] == "INCOMPLETE"
        assert data["unknown_count"] >= 1
        assert len(data["gaps"]) >= 1

    def test_create_assessment_ineligible(self, client):
        """Assessment is INELIGIBLE when a mandatory rule fails."""
        from db.connection import get_session
        from db.repositories import ProgramRepo, CountryRepo
        from db.models import ExtractionRun, Requirement as RequirementModel
        from rules.rule_repository import EnhancedRuleRepo

        headers = self._auth_headers(client)

        # Create applicant with age 50 (over limit)
        resp = client.post(
            "/api/v1/applicants",
            json={"age": 50},
            headers=headers,
        )
        applicant_id = resp.json()["applicant_id"]

        # Seed age rule: age < 45
        with get_session(client.app.state.db_engine) as session:
            CountryRepo(session).upsert("AU", "Australia", "AU")
            ProgramRepo(session).upsert(
                "SKILLED_189", "AU", "Skilled Independent", "189",
                "Skilled Independent visa",
            )
            er = ExtractionRun(
                run_id="ER-TEST-INELIG",
                document_id="DOC-AU-189-MAIN", document_version=1,
                model="test", prompt_version="v1", status="COMPLETED",
            )
            session.add(er)
            session.add(RequirementModel(
                requirement_id="REQ-AGE-189", semantic_key="test-age-key",
                program_id="SKILLED_189", country_id="AU", category="age",
                name="Age requirement", field="age", operator="LT",
                status="APPROVED", extraction_run_id=er.run_id,
            ))
            session.flush()

            rule_repo = EnhancedRuleRepo(session)
            rule_repo.create(
                rule_id="RULE-AGE-189",
                requirement_id="REQ-AGE-189",
                program_id="SKILLED_189",
                category="age",
                field="age",
                operator="LT",
                value_json="45",
                mandatory=True,
                critical=True,
                message="Age must be under 45",
                status="ACTIVE",
                current_version=1,
                source_id="SRC-AU-189-MAIN",
                document_id="DOC-AU-189-MAIN",
                document_version=1,
                chunk_id=1,
                evidence_text="Under 45 required",
            )
            session.commit()

        resp = client.post(
            "/api/v1/assessments",
            json={"applicant_id": applicant_id, "program_id": "SKILLED_189"},
            headers=headers,
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["business_status"] == "INELIGIBLE"
        assert data["failed_count"] >= 1

    def test_create_assessment_no_rules(self, client):
        """Returns 400 when no ACTIVE rules exist for the program."""
        headers = self._auth_headers(client)
        resp = client.post(
            "/api/v1/applicants",
            json={"age": 30},
            headers=headers,
        )
        applicant_id = resp.json()["applicant_id"]

        resp = client.post(
            "/api/v1/assessments",
            json={"applicant_id": applicant_id, "program_id": "SKILLED_189"},
            headers=headers,
        )
        assert resp.status_code == 400
        assert "No ACTIVE rules" in resp.json()["detail"]

    def test_create_assessment_applicant_not_found(self, client):
        """Returns 404 when applicant doesn't exist (ownership check runs first)."""
        headers = self._auth_headers(client)

        # Seed rules
        from db.connection import get_session
        from db.repositories import ProgramRepo, CountryRepo
        from db.models import ExtractionRun, Requirement as RequirementModel
        from rules.rule_repository import EnhancedRuleRepo

        with get_session(client.app.state.db_engine) as session:
            CountryRepo(session).upsert("AU", "Australia", "AU")
            ProgramRepo(session).upsert(
                "SKILLED_189", "AU", "Skilled Independent", "189",
                "Skilled Independent visa",
            )
            er = ExtractionRun(
                run_id="ER-TEST-NOAPPL",
                document_id="DOC-AU-189-MAIN", document_version=1,
                model="test", prompt_version="v1", status="COMPLETED",
            )
            session.add(er)
            session.add(RequirementModel(
                requirement_id="REQ-AGE-189", semantic_key="test-age-key",
                program_id="SKILLED_189", country_id="AU", category="age",
                name="Age requirement", field="age", operator="LT",
                status="APPROVED", extraction_run_id=er.run_id,
            ))
            session.flush()

            rule_repo = EnhancedRuleRepo(session)
            rule_repo.create(
                rule_id="RULE-AGE-189",
                requirement_id="REQ-AGE-189",
                program_id="SKILLED_189",
                category="age",
                field="age",
                operator="LT",
                value_json="45",
                mandatory=True,
                critical=True,
                message="Age must be under 45",
                status="ACTIVE",
                current_version=1,
                source_id="SRC-AU-189-MAIN",
                document_id="DOC-AU-189-MAIN",
                document_version=1,
                chunk_id=1,
                evidence_text="Under 45",
            )
            session.commit()

        resp = client.post(
            "/api/v1/assessments",
            json={"applicant_id": "nonexistent", "program_id": "SKILLED_189"},
            headers=headers,
        )
        assert resp.status_code == 404
        assert "Applicant not found" in resp.json()["detail"]

    def test_get_assessment(self, client):
        """GET /assessments/{id} returns persisted assessment."""
        headers = self._auth_headers(client)
        applicant_id = self._setup_applicant_with_rules(client, headers)

        # Create
        create_resp = client.post(
            "/api/v1/assessments",
            json={"applicant_id": applicant_id, "program_id": "SKILLED_189"},
            headers=headers,
        )
        assessment_id = create_resp.json()["assessment_id"]

        # Get
        resp = client.get(f"/api/v1/assessments/{assessment_id}", headers=headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["assessment_id"] == assessment_id
        assert data["business_status"] == "ELIGIBLE"
        assert len(data["rule_results"]) == 2

    def test_list_assessments(self, client):
        """GET /assessments?applicant_id= lists assessments."""
        headers = self._auth_headers(client)
        applicant_id = self._setup_applicant_with_rules(client, headers)

        client.post(
            "/api/v1/assessments",
            json={"applicant_id": applicant_id, "program_id": "SKILLED_189"},
            headers=headers,
        )

        resp = client.get(
            "/api/v1/assessments",
            params={"applicant_id": applicant_id},
            headers=headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert data["assessments"][0]["business_status"] == "ELIGIBLE"

    def test_evaluate_inline_real(self, client):
        """POST /assessments/evaluate returns real result without persistence."""
        headers = self._auth_headers(client)
        applicant_id = self._setup_applicant_with_rules(client, headers)

        resp = client.post(
            "/api/v1/assessments/evaluate",
            json={"applicant_id": applicant_id, "program_id": "SKILLED_189"},
            headers=headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["business_status"] == "ELIGIBLE"
        assert data["total_rules"] == 2


# ---------------------------------------------------------------------------
# Request ID middleware
# ---------------------------------------------------------------------------

class TestMiddleware:
    def test_request_id_returned(self, client):
        resp = client.get("/health")
        assert "X-Request-ID" in resp.headers

    def test_custom_request_id_forwarded(self, client):
        resp = client.get("/health", headers={"X-Request-ID": "my-custom-id"})
        assert resp.headers["X-Request-ID"] == "my-custom-id"


# ---------------------------------------------------------------------------
# PostgreSQL code paths + resilience
# ---------------------------------------------------------------------------

class TestPostgreSQLCodePaths:
    def test_pg_engine_uses_connection_pool(self):
        """PostgreSQL URL creates pooled engine (not StaticPool)."""
        from db.connection import get_engine
        eng = get_engine("postgresql+psycopg2://user:pass@localhost:5432/testdb")
        from sqlalchemy.pool import QueuePool
        assert eng.pool.__class__ is QueuePool
        assert eng.pool.size() == 5
        assert eng.pool._max_overflow == 10

    def test_sqlite_engine_uses_static_pool(self):
        """SQLite URL creates StaticPool for thread safety."""
        from db.connection import get_engine
        eng = get_engine("sqlite:///:memory:")
        from sqlalchemy.pool import StaticPool
        assert eng.pool.__class__ is StaticPool

    def test_pg_engine_pool_pre_ping(self):
        """PostgreSQL engine has pool_pre_ping enabled."""
        from db.connection import get_engine
        eng = get_engine("postgresql+psycopg2://user:pass@localhost:5432/testdb")
        assert eng.pool._pre_ping is True or getattr(eng.pool, 'pre_ping', None) is True

    def test_get_db_engine_returns_engine(self):
        """get_db_engine returns the engine from app.state."""
        from db.connection import get_db_engine
        from unittest.mock import MagicMock
        request = MagicMock()
        request.app.state.db_engine = "fake_engine"
        result = get_db_engine(request)
        assert result == "fake_engine"

    def test_get_db_engine_raises_503_when_none(self):
        """get_db_engine raises HTTP 503 when db_engine is None."""
        from db.connection import get_db_engine
        from unittest.mock import MagicMock
        from fastapi import HTTPException
        request = MagicMock()
        request.app.state.db_engine = None
        with pytest.raises(HTTPException) as exc_info:
            get_db_engine(request)
        assert exc_info.value.status_code == 503
        assert "Database not available" in exc_info.value.detail

    def test_get_db_engine_raises_503_when_missing(self):
        """get_db_engine raises HTTP 503 when db_engine attr is missing."""
        from db.connection import get_db_engine
        from unittest.mock import MagicMock
        from fastapi import HTTPException
        request = MagicMock()
        del request.app.state.db_engine
        with pytest.raises(HTTPException) as exc_info:
            get_db_engine(request)
        assert exc_info.value.status_code == 503

    def test_api_routes_return_503_without_db(self):
        """API routes that need DB return 503 when engine is None."""
        from api.main import create_app
        from services.auth_service import create_access_token
        app2 = create_app()
        app2.state.db_engine = None
        with TestClient(app2, raise_server_exceptions=False) as c:
            token_data = create_access_token(user_id="test-user", role="applicant")
            resp = c.get(
                "/api/v1/applicants",
                headers={"Authorization": f"Bearer {token_data['access_token']}"},
            )
            assert resp.status_code == 503


class TestApplicantSnapshot:
    def _auth_headers(self, client):
        client.post(
            "/api/v1/auth/register",
            json={"email": "snap-test@example.com", "password": "pass123"},
        )
        resp = client.post(
            "/api/v1/auth/login",
            json={"email": "snap-test@example.com", "password": "pass123"},
        )
        token = resp.json()["access_token"]
        return {"Authorization": f"Bearer {token}"}

    def _setup_applicant_with_rules(self, client, headers):
        from db.connection import get_session
        from db.repositories import ProgramRepo, CountryRepo
        from db.models import ExtractionRun, Requirement as RequirementModel
        from rules.rule_repository import EnhancedRuleRepo

        resp = client.post(
            "/api/v1/applicants",
            json={"age": 30, "english_level": "competent", "points": 65},
            headers=headers,
        )
        applicant_id = resp.json()["applicant_id"]

        with get_session(client.app.state.db_engine) as session:
            CountryRepo(session).upsert("AU", "Australia", "AU")
            ProgramRepo(session).upsert(
                "SKILLED_189", "AU", "Skilled Independent", "189",
                "Skilled Independent visa",
            )
            er = ExtractionRun(
                run_id="ER-TEST-SNAP",
                document_id="DOC-AU-189-MAIN", document_version=1,
                model="test", prompt_version="v1", status="COMPLETED",
            )
            session.add(er)
            for req_id, field, cat in [
                ("REQ-AGE-189", "age", "age"),
                ("REQ-POINTS-189", "points", "points"),
            ]:
                session.add(RequirementModel(
                    requirement_id=req_id, semantic_key=f"test-{cat}-snap",
                    program_id="SKILLED_189", country_id="AU", category=cat,
                    name=f"{cat} requirement", field=field, operator="LT",
                    status="APPROVED", extraction_run_id=er.run_id,
                ))
            session.flush()

            rule_repo = EnhancedRuleRepo(session)
            rule_repo.create(
                rule_id="RULE-AGE-189",
                requirement_id="REQ-AGE-189",
                program_id="SKILLED_189",
                category="age",
                field="age",
                operator="LT",
                value_json="45",
                mandatory=True,
                critical=True,
                message="Age must be under 45",
                status="ACTIVE",
                current_version=1,
                source_id="SRC-AU-189-MAIN",
                document_id="DOC-AU-189-MAIN",
                document_version=1,
                chunk_id=1,
                evidence_text="Under 45 required",
            )
            rule_repo.create(
                rule_id="RULE-POINTS-189",
                requirement_id="REQ-POINTS-189",
                program_id="SKILLED_189",
                category="points",
                field="points",
                operator="GTE",
                value_json="65",
                mandatory=True,
                critical=False,
                message="Must score at least 65 points",
                status="ACTIVE",
                current_version=1,
                source_id="SRC-AU-189-MAIN",
                document_id="DOC-AU-189-MAIN",
                document_version=1,
                chunk_id=2,
                evidence_text="Minimum 65 points",
            )
            session.commit()

        return applicant_id

    def test_assessment_stores_snapshot(self, client):
        """Assessment persists applicant snapshot at time of assessment."""
        headers = self._auth_headers(client)
        applicant_id = self._setup_applicant_with_rules(client, headers)

        resp = client.post(
            "/api/v1/assessments",
            json={"applicant_id": applicant_id, "program_id": "SKILLED_189"},
            headers=headers,
        )
        assert resp.status_code == 201
        data = resp.json()
        snapshot = data.get("applicant_snapshot")
        assert snapshot is not None
        assert isinstance(snapshot, dict)
        assert "age" in snapshot
        assert "english_level" in snapshot
        assert snapshot["age"] == 30
        assert snapshot["english_level"] == "competent"

    def test_assessment_snapshot_immutable_after_creation(self, client):
        """Changing applicant profile after assessment doesn't affect stored snapshot."""
        headers = self._auth_headers(client)
        applicant_id = self._setup_applicant_with_rules(client, headers)

        resp1 = client.post(
            "/api/v1/assessments",
            json={"applicant_id": applicant_id, "program_id": "SKILLED_189"},
            headers=headers,
        )
        assert resp1.status_code == 201
        assessment_id1 = resp1.json()["assessment_id"]
        snapshot1 = resp1.json()["applicant_snapshot"]

        client.patch(
            f"/api/v1/applicants/{applicant_id}",
            json={"age": 35},
            headers=headers,
        )

        resp2 = client.get(f"/api/v1/assessments/{assessment_id1}", headers=headers)
        assert resp2.status_code == 200
        snapshot2 = resp2.json()["applicant_snapshot"]
        assert snapshot2["age"] == 30


# ---------------------------------------------------------------------------
# Admin sync: POST /api/v1/admin/sync must be admin-gated (regression)
# ---------------------------------------------------------------------------

class TestAdminSyncAuth:
    """POST /api/v1/admin/sync is admin-only.

    Regression: the endpoint previously ran `trigger_sync_now` with no auth —
    anyone could trigger a full government source sync. It must now require a
    valid Bearer token (401) AND role="admin" (403 otherwise) before the sync
    engine is reached (verified by mocking trigger_sync_now and asserting it is
    never invoked on the rejected paths).
    """

    def _register_and_login(self, client, email):
        client.post(
            "/api/v1/auth/register",
            json={"email": email, "password": "pass123", "full_name": "Sync Test"},
        )
        resp = client.post(
            "/api/v1/auth/login",
            json={"email": email, "password": "pass123"},
        )
        token = resp.json()["access_token"]
        return {"Authorization": f"Bearer {token}"}

    def _promote_to_admin(self, client, email):
        from db.connection import get_session
        from db.models import ApiUser
        with get_session(client.app.state.db_engine) as s:
            user = s.query(ApiUser).filter(ApiUser.email == email).first()
            user.role = "admin"
            s.commit()

    def test_no_token_returns_401(self, client):
        with patch("scheduler.trigger_sync_now") as mock_sync:
            resp = client.post("/api/v1/admin/sync")
            assert resp.status_code == 401
        mock_sync.assert_not_called()

    def test_non_admin_token_returns_403(self, client):
        headers = self._register_and_login(client, "applicant-sync@example.com")
        with patch("scheduler.trigger_sync_now") as mock_sync:
            resp = client.post("/api/v1/admin/sync", headers=headers)
            assert resp.status_code == 403
            assert "Admin role required" in resp.json()["detail"]
        mock_sync.assert_not_called()

    def test_admin_token_returns_202_and_starts_background_sync(self, client):
        """A full source sync must not block the HTTP request.

        Returns 202 + a run_id immediately and does the work on a background
        thread, so a slow multi-source crawl can never time out the caller.
        """
        headers = self._register_and_login(client, "admin-sync@example.com")
        self._promote_to_admin(client, "admin-sync@example.com")

        with patch("scheduler.start_sync_background") as mock_start:
            mock_start.return_value = {"run_id": "bg-1234", "status": "STARTED"}
            resp = client.post("/api/v1/admin/sync", headers=headers)

        assert resp.status_code == 202
        assert resp.json() == {"run_id": "bg-1234", "status": "STARTED"}
        mock_start.assert_called_once()

    def test_admin_token_returns_409_when_sync_already_running(self, client):
        """Only one sync ever runs at a time, however it was triggered."""
        headers = self._register_and_login(client, "admin-sync-busy@example.com")
        self._promote_to_admin(client, "admin-sync-busy@example.com")

        with patch("scheduler.start_sync_background") as mock_start:
            mock_start.return_value = {
                "status": "busy", "detail": "A sync is already in progress",
            }
            resp = client.post("/api/v1/admin/sync", headers=headers)

        assert resp.status_code == 409
        assert resp.json()["detail"] == "A sync is already in progress"


class TestAdminSyncBackgroundE2E:
    """End-to-end behaviour of the background sync started via POST /admin/sync."""

    def _admin_headers(self, client, db_engine, email):
        client.post(
            "/api/v1/auth/register",
            json={"email": email, "password": "pass123", "full_name": "Sync E2E"},
        )
        resp = client.post(
            "/api/v1/auth/login", json={"email": email, "password": "pass123"},
        )
        from db.connection import get_session
        from db.models import ApiUser

        with get_session(db_engine) as s:
            user = s.query(ApiUser).filter(ApiUser.email == email).first()
            user.role = "admin"
            s.commit()
        return {"Authorization": f"Bearer {resp.json()['access_token']}"}

    def test_second_sync_returns_409_while_first_is_running(self, client, db_engine):
        """A genuinely in-flight sync makes a concurrent request 409."""
        headers = self._admin_headers(client, db_engine, "bg-second@example.com")

        gate = threading.Event()
        try:
            def _slow_sync(session, dry_run=False, trigger_type=None):
                gate.wait(timeout=10)
                return []

            with patch("orchestration.government_sync.sync_all_sources", _slow_sync):
                first = client.post("/api/v1/admin/sync", headers=headers)
                assert first.status_code == 202
                run_id = first.json()["run_id"]

                second = client.post("/api/v1/admin/sync", headers=headers)
                assert second.status_code == 409

                # Give the worker a moment to actually reach the stub.
                deadline = time.time() + 5
                while time.time() < deadline and not gate.is_set():
                    if not _sync_lock.locked():
                        time.sleep(0.01)
                        continue
                    break
                gate.set()
        finally:
            gate.set()
            deadline = time.time() + 5
            while time.time() < deadline and _sync_lock.locked():
                time.sleep(0.01)
            assert not _sync_lock.locked(), "background sync leaked the lock"

        assert run_id

    def test_sync_then_poll_run_id_reaches_completed(self, client, db_engine):
        """The returned run_id is pollable and reaches a terminal state."""
        headers = self._admin_headers(client, db_engine, "bg-poll@example.com")

        with patch("orchestration.government_sync.sync_all_sources",
                   lambda session, dry_run=False, trigger_type=None: []):
            started = client.post("/api/v1/admin/sync", headers=headers)
        assert started.status_code == 202
        run_id = started.json()["run_id"]

        deadline = time.time() + 10
        body = None
        while time.time() < deadline:
            poll = client.get(f"/api/v1/admin/runs/{run_id}", headers=headers)
            assert poll.status_code == 200, poll.text
            body = poll.json()
            if body.get("status") in ("COMPLETED", "FAILED", "PARTIAL"):
                break
            time.sleep(0.02)

        assert body is not None
        assert body["run_id"] == run_id
        assert body["status"] == "COMPLETED", body
        assert body["result"]["total_sources"] == 0
