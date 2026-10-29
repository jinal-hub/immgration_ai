"""Host cooldowns — stop hitting a host after it refuses us (Part B).

A refusal is 403 or 429. It pauses *the host* for FETCH_COOLDOWN_HOURS
(default 0.25 hours / 15 minutes); every source on that host is skipped until
then, a single-source sync answers 409, and there is deliberately no override.

The safety rule these tests also pin down: a refusal may only DELAY us. The
next request we make is byte-for-byte the same request we would have made
otherwise — same headers, same User-Agent, no proxy, same fetch method.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import requests
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import fetch.fetcher as fetcher
from db.connection import get_session_factory
from db.models import (
    Base, Country, FetchLog, GovernmentSource, HostCooldown, ImmigrationProgram,
    PipelineRun,
)
from fetch.politeness import HostLimiter, reset_limiter, set_limiter
from fetch.fetcher import FetchResult, FetchStatus
from orchestration.government_sync import sync_all_sources, sync_source
from orchestration.host_cooldown import (
    DEFAULT_COOLDOWN_HOURS,
    active_cooldown,
    blocked_until,
    blocked_until_map,
    clear_cooldown,
    cooldown_hours,
    is_cooling,
    is_refusal,
    start_cooldown,
)
from utils.fetch_document import (
    FetchStoreOutcome,
    _record_fetch_failure,
    fetch_document_and_store,
)


REFUSING = "https://refusing.example.invalid/189"
CALM = "https://calm.example.invalid/189"


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
def session(engine):
    s = get_session_factory(engine)()
    yield s
    s.close()


def _seed(session, source_id="SRC-AU-189", url=REFUSING):
    session.merge(Country(country_id="AU", name="Australia", iso_code="AUS"))
    session.merge(ImmigrationProgram(
        program_id="SKILLED_189", country_id="AU", visa_category="SKILLED",
        subclass_code="189", name="Skilled Independent", status="ACTIVE",
    ))
    session.merge(GovernmentSource(
        source_id=source_id, country_id="AU", program_id="SKILLED_189",
        visa_category="SKILLED", authority="Home Affairs",
        url=url, source_type="HTML", priority="PRIMARY",
        monitoring_method="WEB", crawl_frequency="DAILY", status="ACTIVE",
    ))
    session.commit()
    return source_id


def _seed_two_hosts(session):
    _seed(session, "SRC-AU-REFUSING", REFUSING)
    _seed(session, "SRC-AU-CALM", CALM)


def _blocked(url, status=403):
    return FetchResult(
        url=url, status=FetchStatus.ACCESS_BLOCKED, http_status=status,
        content_type=None, raw_bytes=None, duration_ms=5,
        error=f"HTTP {status}",
    )


def _fault(url, status=500):
    return FetchResult(
        url=url, status=FetchStatus.FETCH_FAILED, http_status=status,
        content_type=None, raw_bytes=None, duration_ms=5,
        error=f"HTTP {status}",
    )


def _ok(url="https://ok.example.invalid/page"):
    return FetchResult(
        url=url, status=FetchStatus.OK, http_status=200,
        content_type="text/html", raw_bytes=b"<html><body>hello</body></html>",
        duration_ms=5, error=None,
    )


# ===========================================================================
# Configuration
# ===========================================================================
class TestConfiguration:
    def test_default_is_fifteen_minutes(self):
        assert DEFAULT_COOLDOWN_HOURS == 0.25

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("FETCH_COOLDOWN_HOURS", "1.5")
        assert cooldown_hours() == 1.5

    def test_garbage_falls_back_to_the_default(self, monkeypatch):
        monkeypatch.setenv("FETCH_COOLDOWN_HOURS", "six hours")
        assert cooldown_hours() == DEFAULT_COOLDOWN_HOURS

    def test_negative_means_never_pause(self, monkeypatch):
        monkeypatch.setenv("FETCH_COOLDOWN_HOURS", "-3")
        assert cooldown_hours() == 0.0


class TestWhatCountsAsARefusal:
    @pytest.mark.parametrize("status", [403, 429, "403", "429"])
    def test_refused(self, status):
        assert is_refusal(status) is True

    @pytest.mark.parametrize("status", [200, 204, 301, 404, 500, 503, None, "", "OK"])
    def test_merely_failed_or_succeeded(self, status):
        assert is_refusal(status) is False


# ===========================================================================
# The cooldown window itself
# ===========================================================================
class TestCooldownWindow:
    def test_start_sets_the_window_from_now(self, session):
        _seed(session)
        now = datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc)
        until = start_cooldown(session, REFUSING, 403,
                               source_id="SRC-AU-189", now=now)
        session.commit()
        assert until == now + timedelta(minutes=15)
        assert blocked_until(session, REFUSING, now) == until

    def test_it_is_a_host_pause_not_a_url_pause(self, session):
        _seed(session)
        start_cooldown(session, REFUSING, 403, source_id="SRC-AU-189")
        session.commit()
        # A completely different path on the same host is paused too...
        assert blocked_until(session, "https://refusing.example.invalid/other") is not None
        # ...but an unrelated host is untouched.
        assert blocked_until(session, CALM) is None

    def test_an_expired_window_reads_as_reachable(self, session):
        _seed(session)
        now = datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc)
        start_cooldown(session, REFUSING, 403, now=now)
        session.commit()
        after = now + timedelta(minutes=15, seconds=1)
        assert blocked_until(session, REFUSING, after) is None
        assert is_cooling(session, REFUSING, after) is False

    def test_repeat_refusal_extends_the_window_and_counts_hits(self, session):
        _seed(session)
        now = datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc)
        start_cooldown(session, REFUSING, 403, now=now)
        session.commit()
        start_cooldown(session, REFUSING, 429, now=now + timedelta(minutes=5))
        session.commit()

        row = session.get(HostCooldown, "refusing.example.invalid")
        assert row.hits == 2
        assert row.http_status == 429
        assert row.cooldown_until.replace(tzinfo=timezone.utc) == (
            now + timedelta(minutes=20)
        )

    def test_a_refusal_after_the_window_starts_fresh(self, session):
        _seed(session)
        now = datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc)
        start_cooldown(session, REFUSING, 403, now=now)
        session.commit()
        later = now + timedelta(minutes=16)
        start_cooldown(session, REFUSING, 403, now=later)
        session.commit()

        row = session.get(HostCooldown, "refusing.example.invalid")
        assert row.hits == 1, "the count is per window, not forever"

    def test_clearing_lets_the_host_be_asked_again(self, session):
        _seed(session)
        start_cooldown(session, REFUSING, 403, source_id="SRC-AU-189")
        session.commit()
        assert is_cooling(session, REFUSING)
        assert clear_cooldown(session, REFUSING) is True
        session.commit()
        assert blocked_until(session, REFUSING) is None

    def test_clearing_a_host_that_was_never_cooling_is_a_no_op(self, session):
        _seed(session)
        assert clear_cooldown(session, CALM) is False

    def test_blank_url_is_never_cooling(self, session):
        assert blocked_until(session, "") is None
        assert active_cooldown(session, "") is None

    def test_blocked_until_map_answers_a_whole_page_in_one_go(self, session):
        _seed_two_hosts(session)
        now = datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc)
        start_cooldown(session, REFUSING, 403, now=now)
        session.commit()

        result = blocked_until_map(session, [REFUSING, CALM], now)
        assert set(result) == {REFUSING, CALM}
        assert result[REFUSING] is not None
        assert result[CALM] is None


# ===========================================================================
# Recording — a refusal pauses the host, a fault does not
# ===========================================================================
class TestRecording:
    def test_403_pauses_the_host(self, session):
        _seed(session)
        source = session.get(GovernmentSource, "SRC-AU-189")
        _record_fetch_failure(session, source, _blocked(source.url),
                              datetime.now(timezone.utc))
        session.commit()
        assert is_cooling(session, REFUSING)

    def test_429_pauses_the_host(self, session):
        _seed(session)
        source = session.get(GovernmentSource, "SRC-AU-189")
        _record_fetch_failure(session, source, _blocked(source.url, 429),
                              datetime.now(timezone.utc))
        session.commit()
        assert is_cooling(session, REFUSING)

    @pytest.mark.parametrize("make", [_fault, lambda url: _fault(url, 503)])
    def test_an_ordinary_fault_never_pauses_anything(self, session, make):
        """A timeout or 5xx says nothing about our standing — we keep trying."""
        _seed(session)
        source = session.get(GovernmentSource, "SRC-AU-189")
        _record_fetch_failure(session, source, make(source.url),
                              datetime.now(timezone.utc))
        session.commit()
        assert blocked_until(session, REFUSING) is None

    def test_refusal_records_who_triggered_it(self, session):
        _seed(session)
        source = session.get(GovernmentSource, "SRC-AU-189")
        _record_fetch_failure(session, source, _blocked(source.url),
                              datetime.now(timezone.utc))
        session.commit()
        row = session.get(HostCooldown, "refusing.example.invalid")
        assert row.source_id == "SRC-AU-189"
        assert row.http_status == 403
        assert row.hits == 1

    def test_a_refusal_during_sync_stops_the_next_source_on_that_host(
        self, session, monkeypatch,
    ):
        """End to end: one host refuses, and the rest of that run leaves it alone.

        Three sources share the refusing host and one independent source sits
        on a calm host. The calm host only ever returns an ordinary fault
        (500), which must keep the whole run going without pausing anything.
        """
        for letter in "abc":
            _seed(session, f"SRC-AU-REFUSING-{letter.upper()}",
                  f"https://refusing.example.invalid/{letter}")
        _seed(session, "SRC-AU-CALM", CALM)
        asked_for = []

        def fake_fetch(url, source_type="HTML", **kw):
            asked_for.append(url)
            if "refusing.example.invalid" in url:
                return _blocked(url)
            return _fault(url)

        monkeypatch.setattr("fetch.backend.fetch", fake_fetch)

        sync_all_sources(session, trigger_type="MANUAL")
        session.commit()

        # Set-based even though retrieval is single-path and so never produces
        # a second request for the same URL - the dedup still makes the point
        # robustly: what matters is how many *sources* on that host were
        # contacted at all.
        refusing_asked = {u for u in asked_for if "refusing.example.invalid" in u}
        assert len(refusing_asked) == 1, (
            "the first 403 must stop the other two sources on that host "
            f"before they are asked — asked: {sorted(set(asked_for))}"
        )
        assert CALM in asked_for, "an unrelated host is never collateral damage"

        # The pause survives the run: the next run contacts the calm host only.
        asked_for.clear()
        sync_all_sources(session, trigger_type="MANUAL")
        session.commit()
        assert asked_for == [CALM]


class TestSuccessfulFetchClearsThePause:
    def test_a_working_host_is_asked_again_immediately(self, session):
        _seed(session)
        start_cooldown(session, REFUSING, 403, source_id="SRC-AU-189")
        session.commit()

        # Pretend a fetch from that host succeeded while the pause was stale.
        clear_cooldown(session, REFUSING)
        session.commit()
        assert blocked_until(session, REFUSING) is None
        assert session.query(FetchLog).count() == 0, "we only cleared a row"

    def test_a_real_successful_fetch_lifts_its_own_hosts_pause(
        self, session, monkeypatch
    ):
        # A pause left behind by an earlier 403 must not outlive the evidence
        # that contradicts it. The clear happens on the production path, not
        # just in a helper someone has to remember to call.
        _seed(session)
        start_cooldown(session, REFUSING, 403, source_id="SRC-AU-189")
        session.commit()
        assert blocked_until(session, REFUSING) is not None

        source = session.query(GovernmentSource).filter_by(source_id="SRC-AU-189").one()
        monkeypatch.setattr("fetch.backend.fetch", lambda url, **kw: _ok(url))

        outcome = fetch_document_and_store(session, source)
        session.commit()

        assert outcome.status in ("OK", "UNCHANGED")
        assert blocked_until(session, REFUSING) is None, (
            "a real response must lift the pause its host left behind"
        )
        assert not session.query(HostCooldown).filter_by(
            host="refusing.example.invalid"
        ).count()

    def test_a_failed_fetch_leaves_the_pause_in_place(self, session, monkeypatch):
        # Only success clears it. Another failure — refusal or not — has proved
        # nothing about the host's willingness to serve us.
        _seed(session)
        start_cooldown(session, REFUSING, 403, source_id="SRC-AU-189")
        session.commit()
        original = blocked_until(session, REFUSING)

        source = session.query(GovernmentSource).filter_by(source_id="SRC-AU-189").one()
        monkeypatch.setattr("fetch.backend.fetch", lambda url, **kw: _fault(url, 500))

        outcome = fetch_document_and_store(session, source)
        session.commit()

        assert outcome.status != "OK"
        assert blocked_until(session, REFUSING) == original


# ===========================================================================
# Sync behaviour
# ===========================================================================
class TestSyncBehaviour:
    def test_sync_source_refuses_before_opening_a_run(self, session):
        _seed(session)
        start_cooldown(session, REFUSING, 403, source_id="SRC-AU-189")
        session.commit()

        result = sync_source(session, "SRC-AU-189", trigger_type="MANUAL")
        assert result.status == "cooldown"
        assert result.errors and "refused" in result.errors[0].lower()
        assert session.query(PipelineRun).count() == 0, (
            "no run should be opened for a request we did not make"
        )

    def test_sync_source_still_runs_once_the_window_has_passed(self, session):
        _seed(session)
        now = datetime.now(timezone.utc)
        start_cooldown(session, REFUSING, 403, now=now - timedelta(hours=7))
        session.commit()
        assert blocked_until(session, REFUSING) is None

        result = sync_source(session, "SRC-AU-189", dry_run=True,
                             trigger_type="MANUAL")
        assert result.status != "cooldown"

    def test_a_missing_source_keeps_reported_as_not_found(self, session):
        """The cooldown guard must not swallow the existing not-found error."""
        result = sync_source(session, "SRC-NOPE", trigger_type="MANUAL")
        assert result.status == "error"
        assert any("not found" in e for e in result.errors)

    def test_dry_run_is_never_blocked_by_a_cooldown(self, session):
        _seed(session)
        start_cooldown(session, REFUSING, 403, source_id="SRC-AU-189")
        session.commit()
        # dry_run makes no network request, so there is nothing to refuse.
        result = sync_source(session, "SRC-AU-189", dry_run=True,
                             trigger_type="MANUAL")
        assert result.status != "cooldown"

    def test_sync_all_skips_the_whole_host_but_keeps_the_rest(
        self, session, monkeypatch,
    ):
        _seed_two_hosts(session)
        fetched = []

        def fake_fetch_document(sess, source, **kw):
            fetched.append(source.source_id)
            return FetchStoreOutcome(source_id=source.source_id,
                                     status="FETCH_FAILED",
                                     error="simulated", error_type="connect_timeout")

        import orchestration.government_sync as gov
        monkeypatch.setattr(gov, "fetch_document_and_store", fake_fetch_document)

        start_cooldown(session, REFUSING, 403, source_id="SRC-AU-REFUSING")
        session.commit()

        results = sync_all_sources(session, trigger_type="MANUAL")

        assert fetched == ["SRC-AU-CALM"], "the paused host is never contacted"
        by_source = {result.source_id: result for result in results}
        assert by_source["SRC-AU-CALM"].status != "cooldown"
        assert by_source["SRC-AU-REFUSING"].status == "cooldown"
        assert "skipped until" in by_source["SRC-AU-REFUSING"].errors[0]

    def test_sync_all_contacts_every_source_when_nothing_is_cooling(
        self, session, monkeypatch,
    ):
        _seed_two_hosts(session)
        fetched = []

        def fake_fetch_document(sess, source, **kw):
            fetched.append(source.source_id)
            return FetchStoreOutcome(source_id=source.source_id,
                                     status="FETCH_FAILED",
                                     error="simulated", error_type="connect_timeout")

        import orchestration.government_sync as gov
        monkeypatch.setattr(gov, "fetch_document_and_store", fake_fetch_document)

        sync_all_sources(session, trigger_type="MANUAL")
        assert sorted(fetched) == ["SRC-AU-CALM", "SRC-AU-REFUSING"]


# ===========================================================================
# Admin API
# ===========================================================================
@pytest.fixture()
def client(engine):
    from fastapi.testclient import TestClient
    from api.main import app

    app.state.db_engine = engine
    with TestClient(app) as c:
        yield c


@pytest.fixture(autouse=True)
def _free_sync_lock():
    from scheduler import _sync_lock
    if _sync_lock.locked():
        _sync_lock.release()
    yield
    if _sync_lock.locked():
        _sync_lock.release()


@pytest.fixture()
def admin_headers(client):
    client.post("/api/v1/auth/register",
                json={"email": "cooldown-admin@test.com", "password": "admin123"})
    with get_session_factory(client.app.state.db_engine)() as s:
        from db.models import ApiUser
        user = s.query(ApiUser).filter(ApiUser.email == "cooldown-admin@test.com").first()
        user.role = "admin"
        s.commit()
    resp = client.post("/api/v1/auth/login",
                       json={"email": "cooldown-admin@test.com", "password": "admin123"})
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


@pytest.fixture()
def seeded_for_api(engine):
    with get_session_factory(engine)() as s:
        _seed(s, "SRC-AU-REFUSING", REFUSING)
    return "SRC-AU-REFUSING"


class TestSyncEndpoint:
    def test_409_while_the_host_is_refusing(self, client, admin_headers,
                                            seeded_for_api, engine):
        with get_session_factory(engine)() as s:
            start_cooldown(s, REFUSING, 403, source_id=seeded_for_api)
            s.commit()

        resp = client.post(f"/api/v1/admin/sources/{seeded_for_api}/sync",
                           headers=admin_headers)
        assert resp.status_code == 409
        detail = resp.json()["detail"]
        assert detail["code"] == "host_cooldown"
        assert detail["host"] == "refusing.example.invalid"
        assert detail["blocked_until"].startswith("2026"), (
            "the admin is told when to try again"
        )
        assert "refusing.example.invalid" in detail["message"]

    def test_a_busy_409_stays_a_plain_string_so_the_two_are_tellable_apart(
        self, client, admin_headers, seeded_for_api,
    ):
        from scheduler import _sync_lock
        _sync_lock.acquire()
        try:
            resp = client.post(f"/api/v1/admin/sources/{seeded_for_api}/sync",
                               headers=admin_headers)
        finally:
            _sync_lock.release()
        assert resp.status_code == 409
        assert isinstance(resp.json()["detail"], str)

    def test_no_override_flag_will_force_it(self, client, admin_headers,
                                            seeded_for_api, engine):
        """B4: there is deliberately no way to force a paused host."""
        with get_session_factory(engine)() as s:
            start_cooldown(s, REFUSING, 403, source_id=seeded_for_api)
            s.commit()

        for suffix in ("?force=true", "?override=true", "?skip_cooldown=1"):
            resp = client.post(f"/api/v1/admin/sources/{seeded_for_api}/sync{suffix}",
                               headers=admin_headers)
            assert resp.status_code == 409, f"{suffix} must not force a request"

    def test_202_once_the_host_is_reachable_again(self, client, admin_headers,
                                                  seeded_for_api, engine, monkeypatch):
        import orchestration.government_sync as gov
        from orchestration.government_sync import SyncResult
        monkeypatch.setattr(
            gov, "sync_source",
            lambda sess, sid, dry_run=False, trigger_type="ON_DEMAND":
                SyncResult(source_id=sid, status="unchanged"),
        )
        resp = client.post(f"/api/v1/admin/sources/{seeded_for_api}/sync",
                           headers=admin_headers)
        assert resp.status_code == 202

    def test_404_still_wins_over_409(self, client, admin_headers):
        resp = client.post("/api/v1/admin/sources/SRC-MISSING/sync",
                           headers=admin_headers)
        assert resp.status_code == 404


class TestBlockedUntilInResponses:
    @staticmethod
    def _seed_and_pause(engine, pause: bool):
        with get_session_factory(engine)() as s:
            _seed(s, "SRC-AU-REFUSING", REFUSING)
            _seed(s, "SRC-AU-CALM", CALM)
            if pause:
                start_cooldown(s, REFUSING, 403, source_id="SRC-AU-REFUSING")
                s.commit()

    def test_list_exposes_when_the_host_will_be_reachable(self, client,
                                                         admin_headers, engine):
        self._seed_and_pause(engine, pause=True)

        rows = {r["source_id"]: r for r in
                client.get("/api/v1/admin/sources", headers=admin_headers).json()}

        assert rows["SRC-AU-REFUSING"]["blocked_until"] is not None
        assert rows["SRC-AU-CALM"]["blocked_until"] is None

    def test_single_source_view_agrees_with_the_list(self, client, admin_headers,
                                                     engine):
        self._seed_and_pause(engine, pause=True)

        body = client.get("/api/v1/admin/sources/SRC-AU-REFUSING",
                          headers=admin_headers).json()
        assert body["blocked_until"] is not None

    def test_null_when_the_host_has_never_refused(self, client, admin_headers,
                                                  engine):
        self._seed_and_pause(engine, pause=False)
        body = client.get("/api/v1/admin/sources/SRC-AU-CALM",
                          headers=admin_headers).json()
        assert body["blocked_until"] is None

    def test_an_expired_pause_reads_as_reachable(self, client, admin_headers, engine):
        with get_session_factory(engine)() as s:
            _seed(s, "SRC-AU-REFUSING", REFUSING)
            start_cooldown(s, REFUSING, 403, source_id="SRC-AU-REFUSING",
                           now=datetime.now(timezone.utc) - timedelta(hours=7))
            s.commit()

        body = client.get("/api/v1/admin/sources/SRC-AU-REFUSING",
                          headers=admin_headers).json()
        assert body["blocked_until"] is None


# ===========================================================================
# B4 — a refusal may only DELAY us
# ===========================================================================
def _fresh_zero_limiter():
    set_limiter(HostLimiter(min_interval=0.0, jitter=0.0, sleep=lambda s: None))


def _capture_headers(monkeypatch, url):
    """Everything fetcher.fetch would actually put on the wire for ``url``.

    A fresh limiter each call, so both the robots.txt probe and the page request
    are captured identically every time.
    """
    _fresh_zero_limiter()
    captured = []

    def fake_get(request_url, **kwargs):
        captured.append((request_url, kwargs.get("headers"), kwargs.get("proxies")))
        resp = requests.models.Response()
        resp.status_code = 200
        resp.headers["Content-Type"] = "text/html"
        resp._content = b"<html><body>same as always</body></html>"
        resp.url = request_url
        return resp

    monkeypatch.setattr(requests, "get", fake_get)
    fetcher.fetch(url)
    return captured


class TestRefusalNeverChangesHowWeFetch:
    def test_the_next_request_is_identical_to_the_last(self, monkeypatch, session):
        """B4: no new IP, no new User-Agent, no rotating proxy, no method switch."""
        _seed(session)
        url = "https://polite.example.invalid/page"

        before = _capture_headers(monkeypatch, url)

        start_cooldown(session, url, 403, source_id="SRC-AU-189")
        session.commit()
        assert blocked_until(session, url) is not None
        # ...and once the pause lapses, we go back to the host as before.
        clear_cooldown(session, url)
        session.commit()

        after = _capture_headers(monkeypatch, url)

        assert before == after, "a refusal must not change a single header"
        assert all(proxies is None for _, _, proxies in after), "no proxy appears"
        assert all(h and h.get("User-Agent") == fetcher.USER_AGENT
                   for _, h, _ in after)

    def test_the_user_agent_is_one_fixed_string(self):
        """Part of the standing no-evasion policy — a refusal must not vary it."""
        assert fetcher.USER_AGENT == "AU-Immigration-Source-Monitor/0.1"

    def test_starting_a_cooldown_reaches_into_no_fetch_configuration(self, session):
        """The pause lives in the database, never in the fetch layer."""
        _seed(session)
        import fetch.fetcher as f
        before = dict(vars(f))

        start_cooldown(session, REFUSING, 403, source_id="SRC-AU-189")
        session.commit()

        after = dict(vars(f))
        assert after == before, "the cooldown was written into the fetch layer"
        assert f.USER_AGENT == "AU-Immigration-Source-Monitor/0.1"
        # A refusal must not introduce any *rotation* capability. A single
        # static egress proxy helper (fetch/proxy.py) is allowed and unrelated —
        # what the no-evasion policy forbids is cycling identity, so the guard
        # is on rotation, not on the word "proxy".
        rotation = [
            name for name in after
            if "rotate" in name.lower() or "rotation" in name.lower()
        ]
        assert not rotation, f"a rotation helper exists: {rotation}"
