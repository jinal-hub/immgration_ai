"""Tests for the APScheduler-based government source sync scheduler.

Covers every operational invariant:
  1. Scheduler starts correctly
  2. Scheduler doesn't duplicate jobs
  3. crawl_frequency / interval is respected
  4. Failed job does not kill scheduler
  5. Retry works (next interval fires after failure)
  6. One source failure doesn't stop other sources
  7. Graceful shutdown works
  8. Timezone is explicit (UTC)
  9. Overlapping runs are prevented (lock + max_instances=1)
"""
from __future__ import annotations

import sys
import threading
import time
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from db.models import Base, GovernmentSource
from scheduler import (
    start_scheduler, shutdown_scheduler, get_scheduler,
    trigger_sync_now, _sync_lock, _acquired_sync_lock, _release_sync_lock,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture()
def db_engine():
    eng = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=eng)
    return eng


@pytest.fixture(autouse=True)
def _clean_scheduler():
    """Guarantee scheduler is torn down and lock released after every test."""
    yield
    shutdown_scheduler()
    if _sync_lock.locked():
        _sync_lock.release()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
_SOURCE_ROW = {
    "country_id": "AU",
    "program_id": "prog",
    "visa_category": "491",
    "authority": "Home Affairs",
    "source_type": "WEB",
    "priority": "HIGH",
    "monitoring_method": "WEB",
    "crawl_frequency": "DAILY",
    "status": "ACTIVE",
}


def _insert_sources(engine, source_ids: list[str]):
    """Insert government source rows with stable IDs."""
    with engine.begin() as conn:
        for sid in source_ids:
            row = {**_SOURCE_ROW, "source_id": sid, "url": f"https://{sid}.example.com"}
            conn.execute(GovernmentSource.__table__.insert(), row)


# ---------------------------------------------------------------------------
# 1. Scheduler starts correctly
# ---------------------------------------------------------------------------
class TestSchedulerStart:
    def test_start_creates_scheduler(self, db_engine):
        start_scheduler(db_engine, interval_minutes=5)
        sched = get_scheduler()
        assert sched is not None
        assert sched.running

    def test_daily_cron_reads_hour_env(self, db_engine, monkeypatch):
        """GOVERNMENT_SYNC_HOUR_UTC pins a fixed daily run time."""
        monkeypatch.setenv("GOVERNMENT_SYNC_HOUR_UTC", "7")
        monkeypatch.setenv("GOVERNMENT_SYNC_MINUTE_UTC", "30")
        start_scheduler(db_engine)
        job = get_scheduler().get_jobs()[0]
        from apscheduler.triggers.cron import CronTrigger
        assert isinstance(job.trigger, CronTrigger)
        assert "hour='7'" in str(job.trigger)
        assert "minute='30'" in str(job.trigger)

    def test_invalid_hour_raises(self, db_engine):
        import pytest as _pytest
        with _pytest.raises(ValueError):
            start_scheduler(db_engine, hour_utc=24)

    def test_default_is_daily_cron_at_fixed_time(self, db_engine, monkeypatch):
        """Default (no env vars) schedules ONE daily run at 03:00 UTC.

        Regression: the scheduler used to be an interval every 60 minutes from
        process start (drift). Requirement: daily scraping on a fixed clock time.
        """
        monkeypatch.delenv("GOVERNMENT_SYNC_INTERVAL_MINUTES", raising=False)
        monkeypatch.delenv("GOVERNMENT_SYNC_HOUR_UTC", raising=False)
        start_scheduler(db_engine)
        sched = get_scheduler()
        job = sched.get_jobs()[0]
        from apscheduler.triggers.cron import CronTrigger
        assert isinstance(job.trigger, CronTrigger)
        assert "hour='3'" in str(job.trigger)
        assert "minute='0'" in str(job.trigger)

    def test_custom_interval_minutes(self, db_engine):
        start_scheduler(db_engine, interval_minutes=15)
        sched = get_scheduler()
        jobs = sched.get_jobs()
        assert len(jobs) == 1
        assert jobs[0].id == "government_source_sync"
        assert jobs[0].name == "Government Source Sync"

    def test_scheduler_reads_env_var(self, db_engine, monkeypatch):
        monkeypatch.setenv("GOVERNMENT_SYNC_INTERVAL_MINUTES", "90")
        start_scheduler(db_engine)
        sched = get_scheduler()
        job = sched.get_jobs()[0]
        assert job.trigger.interval.total_seconds() == 90 * 60


# ---------------------------------------------------------------------------
# 2. Scheduler doesn't duplicate jobs
# ---------------------------------------------------------------------------
class TestNoDuplicateJobs:
    def test_double_start_is_safe(self, db_engine):
        start_scheduler(db_engine, interval_minutes=5)
        start_scheduler(db_engine, interval_minutes=5)
        assert len(get_scheduler().get_jobs()) == 1

    def test_replace_existing_prevents_duplicates(self, db_engine):
        start_scheduler(db_engine, interval_minutes=5)
        start_scheduler(db_engine, interval_minutes=10)
        assert len(get_scheduler().get_jobs()) == 1


# ---------------------------------------------------------------------------
# 3. crawl_frequency / interval is respected
# ---------------------------------------------------------------------------
class TestIntervalRespected:
    def test_job_runs_at_configured_interval(self, db_engine):
        start_scheduler(db_engine, interval_minutes=1)
        job = get_scheduler().get_jobs()[0]
        assert job.trigger.interval.total_seconds() == 60

    def test_engine_passed_to_job(self, db_engine):
        start_scheduler(db_engine, interval_minutes=5)
        job = get_scheduler().get_jobs()[0]
        assert job.args[0] == db_engine


# ---------------------------------------------------------------------------
# 4. Failed job does not kill scheduler
# ---------------------------------------------------------------------------
class TestFailureResilience:
    def test_scheduler_survives_sync_exception(self, db_engine):
        """If _sync_job raises, the scheduler keeps running."""
        call_count = {"n": 0}

        def exploding_sync(engine):
            call_count["n"] += 1
            raise RuntimeError("Simulated DB explosion")

        start_scheduler(db_engine, interval_minutes=5)
        sched = get_scheduler()

        # Replace the existing job with a fast-firing exploding one
        sched.remove_job("government_source_sync")
        sched.add_job(
            exploding_sync,
            "interval",
            seconds=0.05,
            args=[db_engine],
            id="government_source_sync",
            name="Government Source Sync",
        )

        time.sleep(0.3)

        assert sched.running
        assert call_count["n"] >= 2

    def test_exception_does_not_corrupt_scheduler_state(self, db_engine):
        """After failure, the job is still present and can fire again."""
        def failing(engine):
            raise ValueError("boom")

        start_scheduler(db_engine, interval_minutes=5)
        sched = get_scheduler()
        sched.remove_job("government_source_sync")
        sched.add_job(
            failing,
            "interval",
            seconds=0.05,
            args=[db_engine],
            id="government_source_sync",
            name="Government Source Sync",
        )

        time.sleep(0.3)
        assert sched.get_job("government_source_sync") is not None


# ---------------------------------------------------------------------------
# 5. Retry works (next interval fires after failure)
# ---------------------------------------------------------------------------
class TestRetryBehavior:
    def test_next_interval_fires_after_failure(self, db_engine):
        """After a failed scheduled run, the next interval should still fire."""
        fire_log = []

        def counting_sync(engine):
            fire_log.append("fired")

        start_scheduler(db_engine, interval_minutes=5)
        sched = get_scheduler()
        sched.remove_job("government_source_sync")
        sched.add_job(
            counting_sync,
            "interval",
            seconds=0.05,
            args=[db_engine],
            id="government_source_sync",
            name="Government Source Sync",
        )

        time.sleep(0.3)
        assert len(fire_log) >= 2


# ---------------------------------------------------------------------------
# 6. One source failure doesn't stop other sources
# ---------------------------------------------------------------------------
class TestSourceIsolation:
    def test_sync_all_sources_continues_after_one_failure(self, db_engine):
        """When one source raises, sync_all_sources still processes the others."""
        from orchestration.government_sync import sync_all_sources

        _insert_sources(db_engine, ["src_ok", "src_bad"])

        factory = sessionmaker(bind=db_engine)

        # Use a function that dispatches based on source_id argument
        def selective_fail(session, source_id, **kwargs):
            if source_id == "src_bad":
                raise RuntimeError("source down")
            # src_ok → return the real detect_change result (no doc = unchanged)
            return False, None, None

        with patch(
            "orchestration.government_sync.detect_change",
            side_effect=selective_fail,
        ):
            with factory() as session:
                results = sync_all_sources(session, dry_run=True, trigger_type="TEST")

        statuses = {r.source_id: r.status for r in results}
        assert statuses["src_bad"] == "error"
        assert statuses["src_ok"] == "unchanged"


# ---------------------------------------------------------------------------
# 7. Graceful shutdown works
# ---------------------------------------------------------------------------
class TestGracefulShutdown:
    def test_shutdown_stops_scheduler(self, db_engine):
        start_scheduler(db_engine, interval_minutes=5)
        assert get_scheduler().running
        shutdown_scheduler()
        assert get_scheduler() is None

    def test_shutdown_waits_for_inflight_job(self, db_engine):
        """shutdown(wait=True) should allow a running job to finish."""
        finished = threading.Event()

        def slow_sync(engine):
            time.sleep(0.05)
            finished.set()

        start_scheduler(db_engine, interval_minutes=5)
        sched = get_scheduler()
        sched.remove_job("government_source_sync")
        sched.add_job(
            slow_sync,
            "interval",
            seconds=0.05,
            args=[db_engine],
            id="government_source_sync",
            name="Government Source Sync",
        )

        # Let the job fire at least once
        time.sleep(0.15)
        shutdown_scheduler()
        assert get_scheduler() is None

    def test_double_shutdown_is_safe(self, db_engine):
        start_scheduler(db_engine, interval_minutes=5)
        shutdown_scheduler()
        shutdown_scheduler()  # should not raise


# ---------------------------------------------------------------------------
# 8. Timezone is explicit (UTC)
# ---------------------------------------------------------------------------
class TestExplicitTimezone:
    def test_scheduler_uses_utc(self, db_engine):
        start_scheduler(db_engine, interval_minutes=5)
        sched = get_scheduler()
        assert sched.timezone is not None
        # The timezone should be UTC
        from datetime import timezone
        assert str(sched.timezone) == "UTC"

    def test_trigger_sync_now_returns_utc_aware_fields(self, db_engine):
        result = trigger_sync_now(db_engine)
        assert "total_sources" in result


# ---------------------------------------------------------------------------
# 9. Overlapping runs are prevented (lock + max_instances=1)
# ---------------------------------------------------------------------------
class TestOverlapProtection:
    def test_max_instances_is_one(self, db_engine):
        start_scheduler(db_engine, interval_minutes=5)
        job = get_scheduler().get_jobs()[0]
        assert job.max_instances == 1

    def test_coalesce_is_true(self, db_engine):
        start_scheduler(db_engine, interval_minutes=5)
        job = get_scheduler().get_jobs()[0]
        assert job.coalesce is True

    def test_trigger_sync_now_rejects_concurrent_run(self, db_engine):
        """trigger_sync_now returns 'busy' when lock is already held."""
        _sync_lock.acquire()
        try:
            result = trigger_sync_now(db_engine)
            assert result["status"] == "busy"
            assert "already in progress" in result["detail"]
        finally:
            _sync_lock.release()

    def test_scheduled_job_skips_when_lock_held(self, db_engine):
        """_sync_job returns early (no crash) when the lock is held."""
        from scheduler import _sync_job

        _sync_lock.acquire()
        try:
            _sync_job(db_engine)  # should not raise
        finally:
            _sync_lock.release()

    def test_lock_is_released_after_busy_rejection(self, db_engine):
        """Even if trigger_sync_now returns busy, the lock state is unchanged."""
        _sync_lock.acquire()
        try:
            trigger_sync_now(db_engine)
            assert _sync_lock.locked()  # still held by us
        finally:
            _sync_lock.release()

    def test_lock_released_after_sync_failure(self, db_engine):
        """If sync_all_sources raises, the lock is still released."""
        assert _acquired_sync_lock()
        try:
            pass
        finally:
            _release_sync_lock()

        assert not _sync_lock.locked()

    def test_lock_acquire_release_cycle(self, db_engine):
        """Verify acquire/release round-trips cleanly."""
        assert _acquired_sync_lock()
        assert _sync_lock.locked()
        _release_sync_lock()
        assert not _sync_lock.locked()

    def test_thread_pool_max_workers_is_one(self, db_engine):
        """ThreadPoolExecutor(max_workers=1) prevents parallel threads."""
        start_scheduler(db_engine, interval_minutes=5)
        sched = get_scheduler()
        executor = sched._executors.get("default")
        # APScheduler wraps ThreadPoolExecutor; the inner pool has _max_workers
        assert executor._pool._max_workers == 1

    def test_double_start_preserves_lock(self, db_engine):
        """Double start should not corrupt the lock mechanism."""
        start_scheduler(db_engine, interval_minutes=5)
        start_scheduler(db_engine, interval_minutes=5)
        # Lock should still work
        assert _acquired_sync_lock()
        _release_sync_lock()
        shutdown_scheduler()


def _wait_for_lock_free(timeout: float = 5.0) -> None:
    """Block until the background sync thread has released _sync_lock."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not _sync_lock.locked():
            return
        time.sleep(0.01)
    raise AssertionError("background sync did not release _sync_lock in time")


# ---------------------------------------------------------------------------
# 10. Background sync jobs (POST /api/v1/admin/sync)
#
# Regression guards for the lock/terminal-state race. sync_all_sources() creates
# one PipelineRun per SOURCE, so a batch-level handle lives in the in-memory
# _sync_jobs registry keyed by its own uuid run_id.
# ---------------------------------------------------------------------------
class TestBackgroundSyncJob:
    @staticmethod
    def _stub_sync(monkeypatch, results=None, raises=None, gate=None):
        """Replace sync_all_sources so no DB/network work happens.

        `gate` holds the fake open until set, so a test can observe a
        genuinely in-flight sync instead of racing the background thread.
        """
        import types

        class _R:
            def __init__(self, source_id, status):
                self.source_id = source_id
                self.status = status

        def _fake(session, dry_run=False, trigger_type=None):
            if gate is not None:
                assert gate.wait(timeout=10), "gate never released"
            if raises is not None:
                raise raises
            if results is not None:
                return results
            return [_R("S1", "changed"), _R("S2", "unchanged")]

        mod = types.ModuleType("orchestration.government_sync")
        mod.sync_all_sources = _fake
        monkeypatch.setitem(sys.modules, "orchestration.government_sync", mod)

    def test_returns_immediately_with_run_id_and_started_status(self, db_engine, monkeypatch):
        self._stub_sync(monkeypatch)
        from scheduler import start_sync_background

        out = start_sync_background(db_engine, trigger_type="MANUAL")

        assert out["status"] == "STARTED"
        assert out["run_id"]
        _wait_for_lock_free()
        assert not _sync_lock.locked()

    def test_rejects_concurrent_run_same_as_trigger_sync_now(self, db_engine, monkeypatch):
        """A background sync in flight must reject a second request, exactly as
        trigger_sync_now does - 409 rather than a queued second sync."""
        gate = threading.Event()
        self._stub_sync(monkeypatch, raises=RuntimeError("slow"), gate=gate)
        from scheduler import start_sync_background, trigger_sync_now

        first = start_sync_background(db_engine, trigger_type="MANUAL")
        assert first["status"] == "STARTED"

        # The first sync is genuinely in flight (blocked inside the stub).
        second = start_sync_background(db_engine, trigger_type="MANUAL")
        assert second["status"] == "busy"

        sync_now_result = trigger_sync_now(db_engine)
        assert sync_now_result["status"] == "busy"

        gate.set()
        _wait_for_lock_free()

    def test_progress_readable_via_get_sync_job(self, db_engine, monkeypatch):
        self._stub_sync(monkeypatch)
        from scheduler import start_sync_background, get_sync_job

        run_id = start_sync_background(db_engine, trigger_type="MANUAL")["run_id"]
        _wait_for_lock_free()

        job = get_sync_job(run_id)
        assert job is not None
        assert job["run_id"] == run_id
        assert job["status"] == "COMPLETED"
        assert job["result"]["total_sources"] == 2
        assert job["result"]["changed"] == 1
        assert job["result"]["unchanged"] == 1
        assert job["result"]["cooldowns"] == 0
        assert job["result"]["results"] == [
            {"source_id": "S1", "status": "changed", "errors": []},
            {"source_id": "S2", "status": "unchanged", "errors": []},
        ]

    def test_background_result_preserves_source_errors(self, db_engine, monkeypatch):
        class ErrorResult:
            source_id = "SRC-FAILED"
            status = "error"
            errors = ["Playwright navigation timed out"]

        self._stub_sync(monkeypatch, results=[ErrorResult()])
        from scheduler import start_sync_background, get_sync_job

        run_id = start_sync_background(db_engine, trigger_type="MANUAL")["run_id"]
        _wait_for_lock_free()

        item = get_sync_job(run_id)["result"]["results"][0]
        assert item["source_id"] == "SRC-FAILED"
        assert item["status"] == "error"
        assert item["errors"] == ["Playwright navigation timed out"]

    def test_unknown_run_id_returns_none(self, db_engine):
        from scheduler import get_sync_job

        assert get_sync_job("not-a-real-run-id") is None

    def test_lock_is_released_once_background_job_completes(self, db_engine, monkeypatch):
        """A second release would raise "release unlocked lock" and leave the
        job unfinalised; the run must complete cleanly with the lock free."""
        self._stub_sync(monkeypatch)
        from scheduler import start_sync_background, get_sync_job

        run_id = start_sync_background(db_engine, trigger_type="MANUAL")["run_id"]
        _wait_for_lock_free()

        assert not _sync_lock.locked()
        assert get_sync_job(run_id)["status"] == "COMPLETED"

    def test_lock_released_even_if_sync_raises(self, db_engine, monkeypatch):
        """The finally-based release must run even when sync_all_sources throws."""
        self._stub_sync(monkeypatch, raises=RuntimeError("injected sync failure"))
        from scheduler import start_sync_background, get_sync_job

        run_id = start_sync_background(db_engine, trigger_type="MANUAL")["run_id"]
        _wait_for_lock_free()

        assert not _sync_lock.locked()
        job = get_sync_job(run_id)
        assert job["status"] == "FAILED"
        assert "injected sync failure" in job["error"]


# ---------------------------------------------------------------------------
# 11. Terminal status is never observable before the lock is released
# ---------------------------------------------------------------------------
class TestSyncLockTerminalStateOrdering:
    """Invariant: observing COMPLETED/FAILED implies the lock is already free.

    Otherwise a client that polls until it sees a terminal status and then
    immediately POSTs a new sync gets a spurious 409 for a run that has
    externally already finished.
    """

    @staticmethod
    def _stub(monkeypatch, error=None):
        import types

        class _R:
            source_id, status = "S1", "unchanged"

        def _fake(session, dry_run=False, trigger_type=None):
            if error is not None:
                raise error
            return [_R()]

        mod = types.ModuleType("orchestration.government_sync")
        mod.sync_all_sources = _fake
        monkeypatch.setitem(sys.modules, "orchestration.government_sync", mod)

    def _run_and_capture_order(self, db_engine, monkeypatch, error=None):
        """Drive _run_sync_job in-thread, recording lock state at the moment the
        terminal status is published."""
        from scheduler import _run_sync_job, _set_job

        seen = {}

        def _recording_set_job(run_id, **fields):
            if fields.get("status") in ("COMPLETED", "FAILED"):
                seen["lock_held_at_terminal"] = _sync_lock.locked()
                seen["terminal_status"] = fields["status"]
            _set_job(run_id, **fields)

        self._stub(monkeypatch, error=error)
        monkeypatch.setattr("scheduler._set_job", _recording_set_job)

        _run_sync_job("RUN-ORDER", db_engine, "MANUAL")
        return seen

    def test_completed_is_not_visible_before_lock_is_released(self, db_engine, monkeypatch):
        assert _acquired_sync_lock()
        seen = self._run_and_capture_order(db_engine, monkeypatch)
        assert seen["terminal_status"] == "COMPLETED"
        assert seen["lock_held_at_terminal"] is False
        assert not _sync_lock.locked()

    def test_failed_is_not_visible_before_lock_is_released(self, db_engine, monkeypatch):
        assert _acquired_sync_lock()
        seen = self._run_and_capture_order(db_engine, monkeypatch, error=RuntimeError("boom"))
        assert seen["terminal_status"] == "FAILED"
        assert seen["lock_held_at_terminal"] is False
        assert not _sync_lock.locked()

    def test_lock_released_exactly_once_when_result_recording_fails(self, db_engine, monkeypatch):
        """The release lives only in `finally`. If publishing the terminal state
        raises, that failure must NOT fall through into a second release."""
        from scheduler import _run_sync_job

        self._stub(monkeypatch)

        def _boom_set_job(run_id, **fields):
            # Only the terminal publish is broken; the initial RUNNING marker
            # must still succeed so the run reaches the interesting path.
            if fields.get("status") in ("COMPLETED", "FAILED"):
                raise RuntimeError("injected terminal-state recording failure")

        monkeypatch.setattr("scheduler._set_job", _boom_set_job)

        assert _acquired_sync_lock()
        # Must not raise: a double release of a free lock would raise
        # "release unlocked lock" out of _run_sync_job.
        _run_sync_job("RUN-ORDER-2", db_engine, "MANUAL")

        assert not _sync_lock.locked()

    def test_next_sync_is_not_blocked_once_terminal_status_is_observable(
        self, db_engine, monkeypatch
    ):
        """End-to-end version of the invariant: once a poller can see the
        terminal status, a follow-up sync must be accepted (not 409)."""
        from scheduler import start_sync_background, get_sync_job

        self._stub(monkeypatch)

        run_id = start_sync_background(db_engine, trigger_type="MANUAL")["run_id"]
        _wait_for_lock_free()

        assert get_sync_job(run_id)["status"] == "COMPLETED"
        # The observable terminal status must imply an acquirable lock.
        assert _acquired_sync_lock()
        _release_sync_lock()
