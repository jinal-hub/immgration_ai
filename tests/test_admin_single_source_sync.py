"""Tests for POST /api/v1/admin/sources/{source_id}/sync (Part 1a).

Runs exactly one government source's sync in the background, reusing the
same process-wide sync lock as the global POST /api/v1/admin/sync trigger
and the scheduled job, so it must return 409 if ANY sync is already running.
"""
from __future__ import annotations

import time

import pytest
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool
from fastapi.testclient import TestClient

from db.models import Base, Country, ImmigrationProgram, GovernmentSource
from db.connection import get_session_factory
from api.main import app
from scheduler import _sync_lock


@pytest.fixture()
def engine():
    eng = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=eng)
    return eng


@pytest.fixture()
def client(engine):
    app.state.db_engine = engine
    with TestClient(app) as c:
        yield c


@pytest.fixture(autouse=True)
def _ensure_lock_free():
    # Defensive: a prior test in this process that failed mid-background-sync
    # could otherwise leave the module-level lock held across test cases.
    if _sync_lock.locked():
        _sync_lock.release()
    yield
    if _sync_lock.locked():
        _sync_lock.release()


@pytest.fixture()
def admin_headers(client):
    client.post(
        "/api/v1/auth/register",
        json={"email": "single-sync-admin@test.com", "password": "admin123"},
    )
    with get_session_factory(client.app.state.db_engine)() as s:
        from db.models import ApiUser
        user = s.query(ApiUser).filter(ApiUser.email == "single-sync-admin@test.com").first()
        user.role = "admin"
        s.commit()
    resp = client.post(
        "/api/v1/auth/login",
        json={"email": "single-sync-admin@test.com", "password": "admin123"},
    )
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


@pytest.fixture()
def non_admin_headers(client):
    client.post(
        "/api/v1/auth/register",
        json={"email": "single-sync-plain@test.com", "password": "pass123"},
    )
    resp = client.post(
        "/api/v1/auth/login",
        json={"email": "single-sync-plain@test.com", "password": "pass123"},
    )
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


@pytest.fixture()
def seeded_source(engine):
    with get_session_factory(engine)() as s:
        s.merge(Country(country_id="AU", name="Australia", iso_code="AUS"))
        s.merge(ImmigrationProgram(
            program_id="SKILLED_189", country_id="AU", visa_category="SKILLED",
            subclass_code="189", name="Skilled Independent", status="ACTIVE",
        ))
        s.merge(GovernmentSource(
            source_id="SRC-AU-TEST-SYNC1", country_id="AU", program_id="SKILLED_189",
            visa_category="SKILLED", authority="Home Affairs",
            url="https://example.gov.au/189", source_type="HTML",
            priority="PRIMARY", monitoring_method="WEB", crawl_frequency="DAILY",
            status="ACTIVE",
        ))
        s.commit()
    return "SRC-AU-TEST-SYNC1"


def _wait_for_run_terminal(client, headers, run_id, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        resp = client.get(f"/api/v1/admin/runs/{run_id}", headers=headers)
        if resp.status_code == 200 and resp.json().get("status") in ("COMPLETED", "FAILED"):
            return resp.json()
        time.sleep(0.05)
    raise AssertionError(f"run {run_id} did not reach a terminal state in time")


class TestSingleSourceSync:
    def test_source_response_exposes_persisted_fetch_diagnostics(
        self, client, admin_headers, engine, seeded_source,
    ):
        with get_session_factory(engine)() as session:
            source = session.get(GovernmentSource, seeded_source)
            source.fetch_status = "ACCESS_BLOCKED"
            source.http_status = 403
            source.last_error = "Playwright request was refused"
            source.last_error_type = "http_error"
            session.commit()

        response = client.get(
            f"/api/v1/admin/sources/{seeded_source}", headers=admin_headers,
        )

        assert response.status_code == 200
        assert response.json()["fetch_status"] == "ACCESS_BLOCKED"
        assert response.json()["http_status"] == 403
        assert response.json()["last_error"] == "Playwright request was refused"
        assert response.json()["last_error_type"] == "http_error"

    def test_admin_can_start_a_single_source_sync(self, client, admin_headers, seeded_source, monkeypatch):
        # Per the project's hard testing safety rules, automated tests never
        # make a real network call - stub sync_source itself (the single
        # function scheduler._run_single_source_sync_job calls) rather than
        # letting it reach the fake source URL.
        import orchestration.government_sync as gov_sync
        from orchestration.government_sync import SyncResult

        def _fake_sync_source(session, source_id, dry_run=False, trigger_type="ON_DEMAND"):
            return SyncResult(source_id=source_id, status="unchanged")

        monkeypatch.setattr(gov_sync, "sync_source", _fake_sync_source)

        resp = client.post(
            f"/api/v1/admin/sources/{seeded_source}/sync", headers=admin_headers,
        )
        assert resp.status_code == 202
        body = resp.json()
        assert body["status"] == "STARTED"
        assert "run_id" in body
        # Progress must be readable through the existing runs endpoint with
        # zero changes needed there.
        final = _wait_for_run_terminal(client, admin_headers, body["run_id"])
        assert final["status"] == "COMPLETED"
        assert final["result"] == {
            "source_id": seeded_source,
            "status": "unchanged",
            "errors": [],
            "fetch_status": None,
            "http_status": None,
            "last_error": None,
            "last_error_type": None,
            "last_checked_at": None,
        }

    def test_non_admin_gets_403(self, client, non_admin_headers, seeded_source):
        resp = client.post(
            f"/api/v1/admin/sources/{seeded_source}/sync", headers=non_admin_headers,
        )
        assert resp.status_code == 403

    def test_no_token_gets_401(self, client, seeded_source):
        resp = client.post(f"/api/v1/admin/sources/{seeded_source}/sync")
        assert resp.status_code == 401

    def test_unknown_source_returns_404(self, client, admin_headers):
        resp = client.post(
            "/api/v1/admin/sources/SRC-DOES-NOT-EXIST/sync", headers=admin_headers,
        )
        assert resp.status_code == 404

    def test_409_when_a_sync_is_already_running(self, client, admin_headers, seeded_source):
        _sync_lock.acquire()
        try:
            resp = client.post(
                f"/api/v1/admin/sources/{seeded_source}/sync", headers=admin_headers,
            )
            assert resp.status_code == 409
        finally:
            _sync_lock.release()

    def test_409_also_applies_when_global_sync_is_running(
        self, client, admin_headers, seeded_source,
    ):
        """The lock is shared with POST /api/v1/admin/sync - a single-source
        sync must not be able to start while a global sync is in flight,
        and vice versa."""
        _sync_lock.acquire()
        try:
            global_resp = client.post("/api/v1/admin/sync", headers=admin_headers)
            assert global_resp.status_code == 409
            single_resp = client.post(
                f"/api/v1/admin/sources/{seeded_source}/sync", headers=admin_headers,
            )
            assert single_resp.status_code == 409
        finally:
            _sync_lock.release()
