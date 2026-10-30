"""Source metadata, official-allowlist and SSRF destination validation."""
from __future__ import annotations

import socket
import threading

import pytest

from fetch import source_validation as sv
from fetch.source_validation import (
    SourceValidationError,
    assert_fetch_destination,
    resolve_public_addresses,
    resolve_redirect,
    validate_http_url,
    validate_source_fields,
    validate_source_id,
)


@pytest.fixture(autouse=True)
def _clear_dns_cache():
    with sv._RESOLVE_CACHE_LOCK:
        sv._RESOLVE_CACHE.clear()
    yield
    with sv._RESOLVE_CACHE_LOCK:
        sv._RESOLVE_CACHE.clear()


def _fake_dns(monkeypatch, *addresses):
    calls = []

    def _getaddrinfo(host, port, type=None):
        calls.append((host, port))
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port))
            for address in addresses
        ]

    monkeypatch.setattr(sv.socket, "getaddrinfo", _getaddrinfo)
    return calls


class TestMetadata:
    def test_source_id_accepts_the_shipped_shape(self):
        assert validate_source_id("SRC-AU-MIGRATION-REGS-1994") == "SRC-AU-MIGRATION-REGS-1994"

    @pytest.mark.parametrize(
        "value",
        [
            "src-au-lower", "SRC-au-189", "SRC--AU", "SRC-AU-", "SRC-AU-189/../x",
            "SRC-AU-189\\x", "SRC-AU-189;DROP", "../SRC-AU-189", "SRC-" + "A" * 40,
        ],
    )
    def test_source_id_rejects_traversal_and_injection_shapes(self, value):
        with pytest.raises(SourceValidationError):
            validate_source_id(value)

    def test_fields_are_canonicalised_not_trusted(self):
        result = validate_source_fields(
            {
                "source_id": "SRC-AU-189-MAIN",
                "country_id": "au",
                "program_id": "skilled_189",
                "visa_category": "skilled",
                "source_type": "html",
                "priority": "primary",
                "monitoring_method": "web",
                "crawl_frequency": "daily",
                "url": "HTTPS://Example.GOV.AU/visas/189?x=1",
            }
        )
        assert result["country_id"] == "AU"
        assert result["program_id"] == "SKILLED_189"
        assert result["source_type"] == "HTML"
        assert result["url"] == "https://example.gov.au/visas/189?x=1"

    @pytest.mark.parametrize("field,value", [
        ("country_id", "AUS"), ("program_id", "189"), ("visa_category", ""),
        ("source_type", "RSS"), ("priority", "HIGH"),
        ("monitoring_method", "SCRAPE"), ("crawl_frequency", "HOURLY"),
        ("content_kind", "PDF"),
    ])
    def test_invalid_enumerations_are_rejected(self, field, value):
        fields = {"source_id": "SRC-AU-189-MAIN", field: value}
        with pytest.raises(SourceValidationError):
            validate_source_fields(fields)


class TestUrlValidation:
    def test_valid_official_url_is_normalised(self):
        assert validate_http_url(" HTTPS://Example.GOV.AU/visas/189 ") == (
            "https://example.gov.au/visas/189"
        )

    def test_encoded_path_is_checked_but_preserved(self):
        assert validate_http_url("https://example.gov.au/a%20b") == (
            "https://example.gov.au/a%20b"
        )

    @pytest.mark.parametrize(
        "url",
        [
            "ftp://example.gov.au/file",
            "javascript:alert(1)",
            "https://user:pass@example.gov.au/x",
            "https://example.gov.au/x#fragment",
            "https://example.gov.au:8443/x",
            "https://example.gov.au:80/x",
            "https://127.0.0.1/x",
            "https://[::1]/x",
            "https://localhost/x",
            "https://metadata.google.internal/x",
            "https://evil.example.com/x",
            "https://example.gov.au.evil.com/x",
            "https://example.gov.au/%2e%2e/secret",
            "https://example.gov.au/..%2fsecret",
            "https://example.gov.au/a\\b",
            "https://example.gov.au/" + "a" * 2100,
            "https://example.gov.au/\x00",
        ],
    )
    def test_unsafe_urls_are_rejected(self, url):
        with pytest.raises(SourceValidationError):
            validate_http_url(url)

    def test_allowed_suffixes_are_configurable(self, monkeypatch):
        monkeypatch.setenv("SOURCE_ALLOWED_HOST_SUFFIXES", ".govt.nz")
        assert validate_http_url("https://www.immigration.govt.nz/x").startswith(
            "https://www.immigration.govt.nz"
        )
        with pytest.raises(SourceValidationError):
            validate_http_url("https://example.gov.au/x")

    @pytest.mark.parametrize(
        "url",
        [
            "https://www.vetassess.com.au/",
            "https://www.acs.org.au/",
            "https://www.engineersaustralia.org.au/",
            "https://www.anmac.org.au/",
            "https://www.aitsl.edu.au/",
            "https://www.cpaaustralia.com.au/",
            "https://www.publicaccountants.org.au/",
            "https://www.aim.com.au/",
            "https://theterritory.com.au/migrate",
        ],
    )
    def test_documented_designated_authorities_are_allowed(self, url):
        assert validate_http_url(url).startswith("https://")

    def test_unlisted_com_au_hosts_are_not_allowed(self):
        with pytest.raises(SourceValidationError):
            validate_http_url("https://unlisted.example.com.au/")


class TestDestinationResolution:
    def test_public_address_is_allowed(self, monkeypatch):
        _fake_dns(monkeypatch, "93.184.216.34")
        assert resolve_public_addresses("example.gov.au") == ["93.184.216.34"]

    @pytest.mark.parametrize(
        "address",
        ["127.0.0.1", "10.0.0.5", "192.168.1.4", "169.254.169.254", "::1", "fc00::1", "fe80::1"],
    )
    def test_private_and_metadata_addresses_are_refused(self, monkeypatch, address):
        _fake_dns(monkeypatch, address)
        with pytest.raises(SourceValidationError):
            resolve_public_addresses("example.gov.au")

    def test_mixed_public_private_answers_are_refused(self, monkeypatch):
        _fake_dns(monkeypatch, "93.184.216.34", "127.0.0.1")
        with pytest.raises(SourceValidationError):
            resolve_public_addresses("example.gov.au")

    def test_resolution_failure_is_fail_closed(self, monkeypatch):
        def _boom(host, port, type=None):
            raise OSError("no DNS")

        monkeypatch.setattr(sv.socket, "getaddrinfo", _boom)
        with pytest.raises(SourceValidationError):
            resolve_public_addresses("example.gov.au")

    def test_positive_resolution_is_cached(self, monkeypatch):
        calls = _fake_dns(monkeypatch, "93.184.216.34")
        resolve_public_addresses("example.gov.au")
        resolve_public_addresses("example.gov.au")
        assert len(calls) == 1

    def test_fetch_destination_checks_scheme_and_credentials(self, monkeypatch):
        monkeypatch.delenv("FETCH_VALIDATE_DESTINATION_DNS", raising=False)
        _fake_dns(monkeypatch, "93.184.216.34")
        assert assert_fetch_destination("https://example.gov.au/x")
        with pytest.raises(SourceValidationError):
            assert_fetch_destination("file:///etc/passwd")
        with pytest.raises(SourceValidationError):
            assert_fetch_destination("https://u:p@example.gov.au/x")

    def test_redirect_resolution_applies_the_allowlist(self, monkeypatch):
        monkeypatch.delenv("FETCH_VALIDATE_DESTINATION_DNS", raising=False)
        _fake_dns(monkeypatch, "93.184.216.34")
        assert resolve_redirect(
            "https://example.gov.au/a", "/b"
        ) == "https://example.gov.au/b"
        with pytest.raises(SourceValidationError):
            resolve_redirect("https://example.gov.au/a", "http://127.0.0.1/secret")
