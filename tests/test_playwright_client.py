"""Fast unit tests for fetch/playwright_client.py - no real browser.

The real-browser proof lives in tests/integration/test_playwright_fixture_server.py
(marker ``browser_live``). These run in the default suite on every commit and
cover the decision logic around the driver: classification, which mechanism is
chosen for which source type, conditional-GET short-circuiting, proxy
fail-closed, session reuse, and the honesty rules (one UA, no stealth args).
"""
from __future__ import annotations

import sys
from contextlib import contextmanager
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import fetch.playwright_client as pw  # noqa: E402
from fetch.fetcher import FetchStatus  # noqa: E402
from fetch.politeness import HostLimiter  # noqa: E402

URL = "https://example.gov.au/page"
PDF_URL = "https://example.gov.au/doc.pdf"
HTML = "<html><body><main><h1>Eligibility</h1><p>" + ("x" * 80) + "</p></main></body></html>"


# ---------------------------------------------------------------------------
# Fakes mirroring exactly the Playwright surface playwright_client.py touches
# ---------------------------------------------------------------------------
class FakeAPIResponse:
    def __init__(self, status=200, headers=None, body=b"", url=None):
        self.status = status
        self.headers = headers or {}
        self._body = body
        self.url = url

    def body(self):
        return self._body

    def text(self):
        return self._body.decode("utf-8", errors="replace")


class FakePage:
    def __init__(self, owner):
        self._owner = owner
        self.closed = False
        self.selector_waits = []
        self.timeout_ms = None
        self.content_capture_calls = []

    def set_default_timeout(self, ms):
        self.timeout_ms = ms

    def goto(self, url, wait_until=None):
        self._owner.goto_calls.append((url, wait_until))
        if self._owner.goto_exc is not None:
            raise self._owner.goto_exc
        return FakeAPIResponse(
            status=self._owner.page_status,
            headers=self._owner.page_headers,
            url=url,
        )

    def wait_for_selector(self, selector, timeout=None):
        self.selector_waits.append((selector, timeout))
        if self._owner.selector_exc is not None:
            raise self._owner.selector_exc

    def locator(self, selector):
        assert selector == "html"
        return FakeLocator(self)

    def close(self):
        self.closed = True


class FakeLocator:
    def __init__(self, page):
        self._page = page

    def evaluate(self, expression, arg=None, timeout=None):
        self._page.content_capture_calls.append((expression, timeout))
        if self._page._owner.content_exc is not None:
            raise self._page._owner.content_exc
        return self._page._owner.page_html


class FakeContext:
    class _Request:
        def __init__(self, owner):
            self._owner = owner

        def _respond(self, url, headers=None, method="get"):
            self._owner.request_calls.append((url, dict(headers or {})))
            self._owner.request_methods.append((method, url))
            if self._owner.request_exc is not None:
                raise self._owner.request_exc
            if url.endswith("/robots.txt"):
                return FakeAPIResponse(
                    status=self._owner.robots_status,
                    headers={"content-type": "text/plain"},
                    body=self._owner.robots_body,
                    url=url,
                )
            return FakeAPIResponse(
                status=self._owner.request_status,
                headers=self._owner.request_headers,
                body=self._owner.request_body,
                url=url,
            )

        def get(self, url, headers=None, timeout=None, max_redirects=None):
            return self._respond(url, headers, "get")

        def head(self, url, headers=None, timeout=None, max_redirects=None):
            return self._respond(url, headers, "head")

    def __init__(self, owner, kwargs):
        self._owner = owner
        self.kwargs = kwargs
        self.request = FakeContext._Request(owner)
        self.pages = []
        self.closed = False
        self.route_calls = []
        self.routes = []

    def route(self, pattern, handler):
        self.route_calls.append(pattern)
        self.routes.append(handler)

    def new_page(self):
        page = FakePage(self._owner)
        self.pages.append(page)
        return page

    def close(self):
        self.closed = True


class FakeBrowser:
    def __init__(self, owner):
        self._owner = owner
        self.closed = False

    def new_context(self, **kwargs):
        self._owner.context_kwargs.append(kwargs)
        context = FakeContext(self._owner, kwargs)
        self._owner.contexts.append(context)
        return context

    def close(self):
        self.closed = True


class FakePlaywright:
    """One configurable fake standing in for the whole sync_playwright() tree."""

    def __init__(self, **overrides):
        self.page_status = 200
        self.page_headers = {"content-type": "text/html"}
        self.page_html = HTML
        self.goto_exc = None
        self.selector_exc = None
        self.content_exc = None
        self.request_status = 200
        self.request_headers = {"content-type": "application/pdf"}
        self.request_body = b"%PDF-1.4 " + b"a" * 100
        self.request_exc = None
        self.robots_status = 404
        self.robots_body = b""
        self.launch_exc = None
        for key, value in overrides.items():
            setattr(self, key, value)

        self.launch_calls = []
        self.goto_calls = []
        self.request_calls = []
        self.request_methods = []
        self.context_kwargs = []
        self.contexts = []
        self.browsers = []
        self.chromium = self._Chromium(self)

    class _Chromium:
        def __init__(self, owner):
            self._owner = owner

        def launch(self, **kwargs):
            self._owner.launch_calls.append(kwargs)
            if self._owner.launch_exc is not None:
                raise self._owner.launch_exc
            browser = FakeBrowser(self._owner)
            self._owner.browsers.append(browser)
            return browser

    def as_launcher(self):
        owner = self

        @contextmanager
        def _launcher():
            yield owner

        return _launcher


@pytest.fixture()
def limiter():
    return HostLimiter(min_interval=0, jitter=0, sleep=lambda _s: None)


def _session(fake):
    return pw.PlaywrightSession(launcher=fake.as_launcher())


# ---------------------------------------------------------------------------
# Honesty rules
# ---------------------------------------------------------------------------
class TestHonestyRules:
    def test_launch_args_are_exactly_the_container_flags(self):
        assert pw.LAUNCH_ARGS == ["--no-sandbox", "--disable-dev-shm-usage"]

    def test_default_user_agent_is_the_declared_one(self, monkeypatch):
        monkeypatch.delenv("FETCH_USER_AGENT", raising=False)
        assert pw.user_agent() == "AU-Immigration-Source-Monitor/0.1"

    def test_user_agent_is_operator_overridable(self, monkeypatch):
        monkeypatch.setenv("FETCH_USER_AGENT", "AU-Monitor/2.0 (ops@example.gov.au)")
        assert pw.user_agent() == "AU-Monitor/2.0 (ops@example.gov.au)"

    def test_blank_override_falls_back_rather_than_sending_nothing(self, monkeypatch):
        monkeypatch.setenv("FETCH_USER_AGENT", "   ")
        assert pw.user_agent() == "AU-Immigration-Source-Monitor/0.1"

    def test_context_is_created_with_the_honest_ua_and_locale(self, limiter):
        fake = FakePlaywright()
        with _session(fake) as session:
            pw.fetch(URL, session=session, limiter=limiter)
        kwargs = fake.context_kwargs[0]
        assert kwargs["user_agent"] == pw.user_agent()
        assert kwargs["locale"] == "en-AU"

    def test_launch_is_headless_with_only_the_two_flags(self, limiter):
        fake = FakePlaywright()
        with _session(fake) as session:
            pw.fetch(URL, session=session, limiter=limiter)
        launch = fake.launch_calls[0]
        assert launch["headless"] is True
        assert launch["args"] == ["--no-sandbox", "--disable-dev-shm-usage"]

    def test_no_init_script_or_stealth_hook_is_registered(self, limiter):
        """A stealth implementation would need add_init_script / route
        interception on the context. The fake has neither, so if the client
        ever starts calling them this test fails with AttributeError."""
        fake = FakePlaywright()
        with _session(fake) as session:
            result = pw.fetch(URL, session=session, limiter=limiter)
        assert result.status == FetchStatus.OK


# ---------------------------------------------------------------------------
# Mechanism selection
# ---------------------------------------------------------------------------
class TestMechanismSelection:
    def test_html_goes_through_a_real_navigation(self, limiter):
        fake = FakePlaywright()
        with _session(fake) as session:
            result = pw.fetch(URL, source_type="HTML", session=session, limiter=limiter)
        assert result.acquisition == pw.ACQUISITION_PAGE
        assert fake.goto_calls == [(URL, "domcontentloaded")]
        # The only non-navigation request is the once-per-host robots.txt probe;
        # the body itself came from the navigation, not a second HTTP GET.
        assert [c[0] for c in fake.request_calls] == ["https://example.gov.au/robots.txt"]

    def test_pdf_goes_through_the_request_api_never_a_navigation(self, limiter):
        fake = FakePlaywright()
        with _session(fake) as session:
            result = pw.fetch(PDF_URL, source_type="PDF", session=session, limiter=limiter)
        assert result.acquisition == pw.ACQUISITION_REQUEST
        assert result.raw_bytes.startswith(b"%PDF")
        assert fake.goto_calls == [], "a PDF must never be navigated to"

    def test_pdf_served_as_html_is_requires_manual_download(self, limiter):
        fake = FakePlaywright(
            request_headers={"content-type": "text/html"},
            request_body=b"<html>" + b"x" * 100 + b"</html>",
        )
        with _session(fake) as session:
            result = pw.fetch(PDF_URL, source_type="PDF", session=session, limiter=limiter)
        assert result.status == FetchStatus.REQUIRES_MANUAL_DOWNLOAD


# ---------------------------------------------------------------------------
# Classification parity with the legacy decision table
# ---------------------------------------------------------------------------
class TestClassification:
    def test_200_with_real_content_is_ok(self, limiter):
        fake = FakePlaywright()
        with _session(fake) as session:
            result = pw.fetch(URL, session=session, limiter=limiter)
        assert result.status == FetchStatus.OK
        assert result.http_status == 200

    @pytest.mark.parametrize("status", [401, 403, 429])
    def test_refusals_are_access_blocked(self, limiter, status):
        fake = FakePlaywright(page_status=status, page_html="<html>Forbidden</html>")
        with _session(fake) as session:
            result = pw.fetch(URL, session=session, limiter=limiter)
        assert result.status == FetchStatus.ACCESS_BLOCKED
        assert result.error == f"HTTP {status}"

    def test_challenge_body_is_requires_browser(self, limiter):
        fake = FakePlaywright(
            page_status=403,
            page_html="<html>Checking your browser. Please enable JavaScript.</html>",
        )
        with _session(fake) as session:
            result = pw.fetch(URL, session=session, limiter=limiter)
        assert result.status == FetchStatus.REQUIRES_BROWSER

    def test_egress_proxy_denial_is_network_restricted_not_a_host_block(self, limiter):
        fake = FakePlaywright(
            page_status=403,
            page_headers={"content-type": "text/html", "x-deny-reason": "egress blocked"},
            page_html="<html>denied by local policy</html>",
        )
        with _session(fake) as session:
            result = pw.fetch(URL, session=session, limiter=limiter)
        assert result.status == FetchStatus.NETWORK_RESTRICTED

    def test_other_non_200_is_fetch_failed(self, limiter):
        fake = FakePlaywright(page_status=500, page_html="<html>oops</html>")
        with _session(fake) as session:
            result = pw.fetch(URL, session=session, limiter=limiter)
        assert result.status == FetchStatus.FETCH_FAILED

    def test_tiny_body_is_invalid_content(self, limiter):
        fake = FakePlaywright(page_html="<p>hi</p>")
        with _session(fake) as session:
            result = pw.fetch(URL, session=session, limiter=limiter)
        assert result.status == FetchStatus.INVALID_CONTENT


# ---------------------------------------------------------------------------
# The content-ready wait
# ---------------------------------------------------------------------------
class TestContentReadyWait:
    def test_wait_runs_on_a_normal_page(self, limiter):
        fake = FakePlaywright()
        with _session(fake) as session:
            result = pw.fetch(URL, session=session, limiter=limiter)
        assert result.selector_timed_out is False
        assert result.selector_wait_ms is not None
        assert fake.contexts[0].pages[0].selector_waits[0][0] == pw.CONTENT_READY_SELECTOR

    def test_wait_is_skipped_on_a_block_page(self, limiter):
        fake = FakePlaywright(page_status=403, page_html="<html>no</html>")
        with _session(fake) as session:
            result = pw.fetch(URL, session=session, limiter=limiter)
        assert result.selector_wait_ms is None
        assert result.selector_timed_out is None
        assert fake.contexts[0].pages[0].selector_waits == []

    def test_a_timed_out_wait_still_captures_the_page(self, limiter):
        fake = FakePlaywright(selector_exc=TimeoutError("no structural content"))
        with _session(fake) as session:
            result = pw.fetch(URL, session=session, limiter=limiter)
        assert result.status == FetchStatus.OK
        assert result.selector_timed_out is True

    def test_dom_capture_uses_the_fetch_timeout(self, limiter):
        fake = FakePlaywright()
        with _session(fake) as session:
            result = pw.fetch(URL, timeout=9, session=session, limiter=limiter)
        assert result.status == FetchStatus.OK
        page = fake.contexts[0].pages[0]
        assert page.content_capture_calls == [
            ("(root) => root.outerHTML", 9000),
        ]

    def test_dom_capture_timeout_is_reported_as_a_fetch_failure(self, limiter):
        fake = FakePlaywright(content_exc=TimeoutError("DOM capture timed out"))
        with _session(fake) as session:
            result = pw.fetch(URL, session=session, limiter=limiter)
        assert result.status == FetchStatus.FETCH_FAILED
        assert result.error_type == "browser_timeout"
        assert fake.contexts[0].pages[0].closed is True
        assert fake.contexts[0].closed is True

    def test_the_page_is_always_closed(self, limiter):
        fake = FakePlaywright(selector_exc=TimeoutError("x"))
        with _session(fake) as session:
            pw.fetch(URL, session=session, limiter=limiter)
        assert fake.contexts[0].pages[0].closed is True

    def test_the_context_is_always_closed_even_on_a_navigation_error(self, limiter):
        fake = FakePlaywright(goto_exc=RuntimeError("net::ERR_CONNECTION_REFUSED"))
        with _session(fake) as session:
            result = pw.fetch(URL, session=session, limiter=limiter)
        assert result.status == FetchStatus.FETCH_FAILED
        assert result.error_type == "connection_error"
        assert fake.contexts[0].closed is True


# ---------------------------------------------------------------------------
# Conditional GET
# ---------------------------------------------------------------------------
class TestConditionalGet:
    def test_no_validators_means_no_pre_check_request(self, limiter):
        fake = FakePlaywright()
        with _session(fake) as session:
            pw.fetch(URL, session=session, limiter=limiter)
        assert [c for c in fake.request_calls if not c[0].endswith("robots.txt")] == []

    def test_validators_trigger_a_pre_check_before_navigating(self, limiter):
        fake = FakePlaywright(request_status=304, request_headers={"etag": '"v1"'})
        with _session(fake) as session:
            result = pw.fetch(
                URL, session=session, limiter=limiter,
                etag='"v1"', last_modified="Wed, 01 Jan 2026 00:00:00 GMT",
            )
        probe = [c for c in fake.request_calls if not c[0].endswith("robots.txt")][0]
        assert probe[1]["If-None-Match"] == '"v1"'
        assert probe[1]["If-Modified-Since"] == "Wed, 01 Jan 2026 00:00:00 GMT"
        assert ("get", URL) in fake.request_methods
        assert not any(method == "head" and target == URL
                       for method, target in fake.request_methods)
        assert result.not_modified is True
        assert result.raw_bytes is None
        assert result.ok is True
        assert fake.goto_calls == [], "a 304 must not cost a navigation"

    def test_non_304_pre_check_falls_through_to_navigation(self, limiter):
        fake = FakePlaywright(request_status=200, request_headers={"content-type": "text/html"})
        with _session(fake) as session:
            result = pw.fetch(URL, session=session, limiter=limiter, etag='"stale"')
        assert result.not_modified is False
        assert fake.goto_calls, "a changed document must still be rendered"
        assert result.acquisition == pw.ACQUISITION_PAGE

    def test_a_failed_pre_check_does_not_fail_the_fetch(self, limiter):
        fake = FakePlaywright(request_exc=RuntimeError("probe blew up"))
        with _session(fake) as session:
            result = pw.fetch(URL, session=session, limiter=limiter, etag='"v1"')
        assert result.status == FetchStatus.OK
        assert fake.goto_calls, "we fall back to a normal navigation"

    def test_pdf_conditional_304_short_circuits(self, limiter):
        fake = FakePlaywright(request_status=304, request_headers={"etag": '"p1"'})
        with _session(fake) as session:
            result = pw.fetch(
                PDF_URL, source_type="PDF", session=session, limiter=limiter, etag='"p1"',
            )
        assert result.not_modified is True
        assert result.http_status == 304
        assert result.etag == '"p1"'

    def test_validators_are_captured_from_a_200_page(self, limiter):
        fake = FakePlaywright(page_headers={
            "content-type": "text/html", "etag": '"fresh"',
            "last-modified": "Thu, 02 Jan 2026 00:00:00 GMT",
        })
        with _session(fake) as session:
            result = pw.fetch(URL, session=session, limiter=limiter)
        assert result.etag == '"fresh"'
        assert result.last_modified == "Thu, 02 Jan 2026 00:00:00 GMT"


# ---------------------------------------------------------------------------
# Politeness, robots, cooldown inputs
# ---------------------------------------------------------------------------
class TestPolitenessIntegration:
    def test_limiter_wait_is_recorded(self, limiter):
        fake = FakePlaywright()
        with _session(fake) as session:
            result = pw.fetch(URL, session=session, limiter=limiter)
        assert result.limiter_wait_ms is not None

    def test_robots_crawl_delay_is_registered(self, limiter):
        fake = FakePlaywright(
            robots_status=200, robots_body=b"User-agent: *\nCrawl-delay: 5\n",
        )
        with _session(fake) as session:
            pw.fetch(URL, session=session, limiter=limiter)
        assert limiter.crawl_delay_for(URL) == 5.0

    def test_robots_failure_is_not_a_fetch_failure(self, limiter):
        fake = FakePlaywright(robots_status=500)
        with _session(fake) as session:
            result = pw.fetch(URL, session=session, limiter=limiter)
        assert result.status == FetchStatus.OK

    def test_retry_after_on_429_pauses_the_host(self, limiter):
        fake = FakePlaywright(
            page_status=429,
            page_headers={"content-type": "text/plain", "retry-after": "11"},
            page_html="too many requests",
        )
        with _session(fake) as session:
            pw.fetch(URL, session=session, limiter=limiter)
        assert limiter.next_allowed_at(URL) is not None

    def test_retry_after_http_date_is_ignored_not_guessed(self, limiter):
        fake = FakePlaywright(
            page_status=429,
            page_headers={"content-type": "text/plain",
                           "retry-after": "Wed, 21 Oct 2026 07:28:00 GMT"},
            page_html="too many requests",
        )
        with _session(fake) as session:
            result = pw.fetch(URL, session=session, limiter=limiter)
        assert result.status == FetchStatus.ACCESS_BLOCKED  # still recorded honestly


# ---------------------------------------------------------------------------
# Session lifecycle and proxy
# ---------------------------------------------------------------------------
class TestSessionLifecycle:
    def test_one_launch_serves_many_fetches(self, limiter):
        fake = FakePlaywright()
        with _session(fake) as session:
            first = pw.fetch(URL, session=session, limiter=limiter)
            second = pw.fetch(URL + "2", session=session, limiter=limiter)
        assert len(fake.launch_calls) == 1
        assert first.browser_launch_ms is not None
        assert second.browser_launch_ms is None

    def test_each_fetch_gets_its_own_context(self, limiter):
        fake = FakePlaywright()
        with _session(fake) as session:
            pw.fetch(URL, session=session, limiter=limiter)
            pw.fetch(URL + "2", session=session, limiter=limiter)
        assert len(fake.contexts) == 2
        assert all(c.closed for c in fake.contexts)

    def test_closing_the_session_closes_the_browser(self, limiter):
        fake = FakePlaywright()
        session = _session(fake)
        pw.fetch(URL, session=session, limiter=limiter)
        session.close()
        assert fake.browsers[0].closed is True

    def test_close_is_idempotent(self):
        fake = FakePlaywright()
        session = _session(fake)
        session.close()
        session.close()  # must not raise

    def test_an_unused_session_never_launches(self):
        fake = FakePlaywright()
        session = _session(fake)
        session.close()
        assert fake.launch_calls == []

    def test_launch_failure_is_classified_not_raised(self, limiter):
        fake = FakePlaywright(launch_exc=RuntimeError("no browser binary"))
        with _session(fake) as session:
            result = pw.fetch(URL, session=session, limiter=limiter)
        assert result.status == FetchStatus.FETCH_FAILED
        assert result.error_type in ("browser_launch_failed", "browser_navigation_failed")


class TestProxyFailsClosed:
    def test_malformed_proxy_refuses_to_fetch_direct(self, limiter, monkeypatch):
        monkeypatch.setenv("FETCH_PROXY_URL", "not-a-url")
        fake = FakePlaywright()
        with _session(fake) as session:
            result = pw.fetch(URL, session=session, limiter=limiter)
        assert result.status == FetchStatus.NETWORK_RESTRICTED
        assert result.error_type == "proxy_config_error"
        assert fake.launch_calls == [], "a bad proxy must not reach the network at all"

    def test_valid_proxy_is_passed_to_the_context(self, limiter, monkeypatch):
        monkeypatch.setenv("FETCH_PROXY_URL", "http://user:secret@proxy.invalid:8080")
        fake = FakePlaywright()
        with _session(fake) as session:
            pw.fetch(URL, session=session, limiter=limiter)
        proxy = fake.context_kwargs[0]["proxy"]
        assert proxy["server"] == "http://proxy.invalid:8080"
        assert proxy["username"] == "user"

    def test_proxy_credentials_never_appear_in_an_error(self, limiter, monkeypatch):
        monkeypatch.setenv("FETCH_PROXY_URL", "ftp://user:secret@proxy.invalid:8080")
        fake = FakePlaywright()
        with _session(fake) as session:
            result = pw.fetch(URL, session=session, limiter=limiter)
        assert result.status == FetchStatus.NETWORK_RESTRICTED
        assert "secret" not in (result.error or "")


class TestExceptionClassification:
    @pytest.mark.parametrize("message,expected", [
        ("net::ERR_NAME_NOT_RESOLVED at https://x", "dns_resolution_failure"),
        ("net::ERR_CONNECTION_REFUSED", "connection_error"),
        ("net::ERR_CERT_AUTHORITY_INVALID", "tls_error"),
        ("net::ERR_PROXY_CONNECTION_FAILED", "proxy_error"),
        ("something else entirely", "browser_navigation_failed"),
    ])
    def test_network_errors_are_classified(self, message, expected):
        assert pw.classify_exception(RuntimeError(message)) == expected

    def test_timeout_is_classified_by_type_name(self):
        class TimeoutError_(Exception):
            pass

        TimeoutError_.__name__ = "TimeoutError"
        assert pw.classify_exception(TimeoutError_("x")) == "browser_timeout"
