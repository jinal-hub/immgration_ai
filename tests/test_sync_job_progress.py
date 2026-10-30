"""Durable batch progress: persistence, interruption, resume and skips."""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from db.models import Base, Country, GovernmentSource, ImmigrationProgram
from db.repositories import SyncJobRepo
from orchestration import government_sync as gs


@pytest.fixture()
def session():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine)
    s = factory()
    yield s
    s.close()


@pytest.fixture(autouse=True)
def _no_real_browser(monkeypatch):
    class _Session:
        def close(self):
            pass

    monkeypatch.setattr(gs, "new_fetch_session", lambda: _Session())


def _seed_sources(session, *source_ids):
    session.add(Country(country_id="AU", name="Australia", iso_code="AU"))
    session.add(ImmigrationProgram(
        program_id="491", country_id="AU", visa_category="SKILLED",
        subclass_code="491", name="Skilled Work Regional",
    ))
    for source_id in source_ids:
        session.add(GovernmentSource(
            source_id=source_id, country_id="AU", program_id="491",
            visa_category="SKILLED", authority="Department of Home Affairs",
            url=f"https://example.gov.au/{source_id}",
            source_type="WEB", priority="HIGH", monitoring_method="WEB",
            crawl_frequency="DAILY", status="ACTIVE",
        ))
    session.commit()


def _result(source_id: str, status: str = "unchanged"):
    return gs.SyncResult(source_id=source_id, status=status)


def test_completed_job_records_every_source(session, monkeypatch):
    _seed_sources(session, "S1", "S2")
    monkeypatch.setattr(gs, "sync_source", lambda *a, **k: _result(a[1]))

    job_id = str(uuid.uuid4())
    gs.sync_all_sources(session, trigger_type="MANUAL", sync_job_id=job_id)

    repo = SyncJobRepo(session)
    job = repo.get(job_id)
    assert job.status == "COMPLETED"
    assert job.total_sources == 2
    assert job.processed_sources == 2
    assert job.unchanged_sources == 2
    assert {row.status for row in job.sources} == {"UNCHANGED"}


def test_keyboard_interrupt_is_persisted_and_resume_skips_finished_sources(
    session, monkeypatch,
):
    _seed_sources(session, "S1", "S2")
    calls = []

    def _fake(session_, source_id, **kwargs):
        calls.append(source_id)
        if source_id == "S2":
            raise KeyboardInterrupt()
        return _result(source_id, "changed")

    monkeypatch.setattr(gs, "sync_source", _fake)
    job_id = str(uuid.uuid4())

    with pytest.raises(KeyboardInterrupt):
        gs.sync_all_sources(session, trigger_type="MANUAL", sync_job_id=job_id)

    repo = SyncJobRepo(session)
    job = repo.get(job_id)
    assert job.status == "INTERRUPTED"
    assert repo.get_source(job_id, "S1").status == "CHANGED"
    assert repo.get_source(job_id, "S2").status == "INTERRUPTED"

    calls.clear()

    def _resume_fake(session_, source_id, **kwargs):
        calls.append(source_id)
        return _result(source_id)

    monkeypatch.setattr(gs, "sync_source", _resume_fake)
    gs.sync_all_sources(session, trigger_type="MANUAL", sync_job_id=job_id)

    assert calls == ["S2"], "S1 already finished and must not be fetched twice"
    job = repo.get(job_id)
    assert job.status == "COMPLETED"
    assert repo.get_source(job_id, "S1").status == "CHANGED"
    assert repo.get_source(job_id, "S2").status == "UNCHANGED"


def test_host_cooldown_skip_is_persisted(session, monkeypatch):
    _seed_sources(session, "S1")
    monkeypatch.setattr(
        gs, "blocked_until",
        lambda *a, **k: datetime.now(timezone.utc) + timedelta(hours=1),
    )
    monkeypatch.setattr(
        gs, "sync_source",
        lambda *a, **k: pytest.fail("a cooling host must not be fetched"),
    )

    job_id = str(uuid.uuid4())
    results = gs.sync_all_sources(
        session, trigger_type="MANUAL", sync_job_id=job_id,
    )

    assert [r.status for r in results] == ["cooldown"]
    repo = SyncJobRepo(session)
    row = repo.get_source(job_id, "S1")
    assert row.status == "SKIPPED"
    assert row.reason == "HOST_COOLDOWN"
    assert repo.get(job_id).skipped_sources == 1


def test_partial_status_when_some_sources_error(session, monkeypatch):
    _seed_sources(session, "S1", "S2", "S3")

    def _fake(session_, source_id, **kwargs):
        if source_id == "S1":
            raise RuntimeError("navigation timed out")
        if source_id == "S3":
            return _result(source_id, "no_document")
        return _result(source_id)

    monkeypatch.setattr(gs, "sync_source", _fake)
    job_id = str(uuid.uuid4())

    gs.sync_all_sources(session, trigger_type="MANUAL", sync_job_id=job_id)

    repo = SyncJobRepo(session)
    job = repo.get(job_id)
    assert job.status == "PARTIAL"
    assert job.error_sources == 1
    assert job.skipped_sources == 1
    assert repo.get_source(job_id, "S1").status == "ERROR"
    assert repo.get_source(job_id, "S2").status == "UNCHANGED"
    assert repo.get_source(job_id, "S3").status == "SKIPPED"
    assert repo.get_source(job_id, "S3").reason == "NO_DOCUMENT"
