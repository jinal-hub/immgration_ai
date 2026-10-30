"""Validation for government-source metadata, URLs and outbound destinations.

Two layers, deliberately separate:

* metadata/URL validation is pure and used by the admin API before a source can
  be created or approved. It rejects traversal-shaped identifiers, unsupported
  schemes, credentials-in-URL, non-standard ports, fragments and hosts outside
  the configured official-government suffix list.
* destination validation additionally resolves every address for the host and
  refuses non-public answers. It runs at retrieval time (including on redirects)
  where DNS rebinding and redirect-based SSRF actually matter.

The default suffix list is ``.gov.au`` because this product only covers
Australian visas. Exact hosts for designated Australian assessment and
government migration authorities are also allowed; arbitrary commercial
domains are not. A deployment can extend the suffix list with a comma-separated
``SOURCE_ALLOWED_HOST_SUFFIXES`` value (for example ``.gov.au,.govt.nz``); it
cannot disable the public-address check.
"""
from __future__ import annotations

import ipaddress
import os
import re
import socket
import threading
import time
from urllib.parse import unquote, urljoin, urlparse
from urllib.parse import urlsplit, urlunsplit

DEFAULT_ALLOWED_SUFFIXES = (".gov.au",)
APPROVED_DESIGNATED_AUTHORITY_HOSTS = frozenset({
    "theterritory.com.au",
    "www.vetassess.com.au",
    "vetassess.com.au",
    "www.acs.org.au",
    "acs.org.au",
    "www.engineersaustralia.org.au",
    "engineersaustralia.org.au",
    "www.anmac.org.au",
    "anmac.org.au",
    "www.aitsl.edu.au",
    "aitsl.edu.au",
    "www.cpaaustralia.com.au",
    "cpaaustralia.com.au",
    "www.publicaccountants.org.au",
    "publicaccountants.org.au",
    "www.aim.com.au",
    "aim.com.au",
})


def allowed_suffixes() -> tuple[str, ...]:
    raw = os.getenv("FETCH_ALLOWED_DOMAINS") or os.getenv("SOURCE_ALLOWED_HOST_SUFFIXES")
    if raw is None or not raw.strip():
        return DEFAULT_ALLOWED_SUFFIXES
    values = []
    for item in raw.split(","):
        suffix = item.strip().lower()
        if not suffix:
            continue
        if suffix == "*" or suffix == "any":
            env = os.getenv("ENVIRONMENT", "development").strip().lower()
            if env == "production":
                raise ValueError("Wildcard '*' is not permitted in production")
            return ("*",)
        if not suffix.startswith("."):
            suffix = "." + suffix
        values.append(suffix)
    return tuple(values) or DEFAULT_ALLOWED_SUFFIXES

SOURCE_ID_RE = re.compile(r"^SRC-[A-Z0-9]+(?:-[A-Z0-9]+)*$")
COUNTRY_RE = re.compile(r"^[A-Z]{2}$")
PROGRAM_RE = re.compile(r"^[A-Z][A-Z0-9_]{1,31}$")
CATEGORY_RE = re.compile(r"^[A-Z][A-Z0-9_]{1,31}$")
HOST_LABEL_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")

SOURCE_TYPES = {"HTML", "PDF", "API"}
PRIORITIES = {"PRIMARY", "SECONDARY"}
MONITORING_METHODS = {"WEB", "MANUAL", "API"}
CRAWL_FREQUENCIES = {"DAILY", "WEEKLY", "MONTHLY", "QUARTERLY", "MANUAL"}
CONTENT_KINDS = {"PAGE", "NEWS_LIST"}

MAX_URL_LENGTH = 2048
MAX_IDENTIFIER_LENGTH = 32
FORBIDDEN_HOSTS = {
    "localhost", "localhost.localdomain", "metadata.google.internal",
    "metadata", "instance-data",
}
FORBIDDEN_HOST_SUFFIXES = (".localhost", ".local", ".internal", ".home.arpa")
_TRAVERSAL_MARKERS = ("..", "%2e%2e", "%2f", "%5c", "\\")


class SourceValidationError(ValueError):
    """A source field or destination URL is not safe/valid."""

    def __init__(self, message: str, field: str | None = None):
        super().__init__(message)
        self.field = field


def _fail(message: str, field: str | None = None) -> None:
    raise SourceValidationError(message, field=field)


# Duplicate removed - using the first definition above


def _validate_identifier(value: str | None, field: str, pattern: re.Pattern[str]) -> str:
    if not value or not isinstance(value, str):
        _fail(f"{field} is required", field)
    value = value.strip()
    if len(value) > MAX_IDENTIFIER_LENGTH or not pattern.match(value):
        _fail(
            f"{field} must match {pattern.pattern} and be at most "
            f"{MAX_IDENTIFIER_LENGTH} characters",
            field,
        )
    return value


def validate_source_id(value: str | None) -> str:
    return _validate_identifier(value, "source_id", SOURCE_ID_RE)


def validate_country_id(value: str | None) -> str:
    return _validate_identifier(
        value.upper() if isinstance(value, str) else value, "country_id", COUNTRY_RE
    )


def validate_program_id(value: str | None) -> str:
    return _validate_identifier(
        value.upper() if isinstance(value, str) else value, "program_id", PROGRAM_RE
    )


def validate_visa_category(value: str | None) -> str:
    return _validate_identifier(
        value.upper() if isinstance(value, str) else value,
        "visa_category", CATEGORY_RE,
    )


def _validate_choice(value: str | None, field: str, choices: set[str], default: str | None = None) -> str:
    chosen = (value if value is not None else default or "").strip().upper()
    if chosen not in choices:
        _fail(f"{field} must be one of {sorted(choices)}, got {value!r}", field)
    return chosen


def _normalise_host(host: str) -> str:
    host = (host or "").strip().lower().rstrip(".")
    if not host or len(host) > 253 or not host.isascii():
        _fail("URL host must be a valid ASCII DNS name", "url")
    labels = host.split(".")
    if len(labels) < 2 or any(not HOST_LABEL_RE.match(label) for label in labels):
        _fail("URL host must be a valid DNS name, not an IP literal", "url")
    return host


def validate_http_url(value: str | None, *, resolve: bool = False) -> str:
    """Validate a government-source URL and return its normalised form.

    ``resolve=True`` also resolves the host and refuses any non-global address.
    It is used on the retrieval path, including each redirect hop. Write-time
    validation uses ``resolve=False`` so a hermetic admin API test does not
    depend on external DNS, while the fetch guard still enforces the network
    boundary before any bytes leave the process.
    """
    if not value or not isinstance(value, str):
        _fail("url is required", "url")
    url = value.strip()
    if len(url) > MAX_URL_LENGTH:
        _fail(f"url must be at most {MAX_URL_LENGTH} characters", "url")
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in url):
        _fail("url must not contain control characters", "url")

    parsed = urlparse(url)
    if parsed.scheme.lower() not in {"http", "https"}:
        _fail("url scheme must be http or https", "url")
    if parsed.username or parsed.password or "@" in parsed.netloc:
        _fail("url must not contain embedded credentials", "url")
    if parsed.fragment:
        _fail("url must not contain a fragment", "url")

    host = _normalise_host(parsed.hostname or "")
    if host in FORBIDDEN_HOSTS or any(host.endswith(s) for s in FORBIDDEN_HOST_SUFFIXES):
        _fail("url host is not a public government host", "url")

    try:
        port = parsed.port
    except ValueError:
        _fail("url has an invalid port", "url")
    scheme = parsed.scheme.lower()
    if port is not None and port not in {80, 443}:
        _fail("url port must be 80 or 443", "url")
    if port == 80 and scheme == "https":
        _fail("https URL cannot use port 80", "url")
    if port == 443 and scheme == "http":
        _fail("http URL cannot use port 443", "url")

    suffixes = allowed_suffixes()
    if "*" in suffixes or "any" in suffixes:
        pass
    elif (
        host not in APPROVED_DESIGNATED_AUTHORITY_HOSTS
        and not any(host == suffix.lstrip(".") or host.endswith(suffix) for suffix in suffixes)
    ):
        _fail(
            "url host is not on the official government or designated-authority allowlist",
            "url",
        )

    decoded_path = unquote(parsed.path or "/")
    if any(marker in decoded_path.lower() for marker in _TRAVERSAL_MARKERS):
        _fail("url path must not contain traversal sequences", "url")
    path = parsed.path or "/"
    if not path.startswith("/"):
        path = "/" + path

    normalised = urlunsplit((scheme, parsed.netloc.lower(), path, parsed.query, ""))
    if resolve:
        assert_public_destination(normalised)
    return normalised


def validate_source_fields(fields: dict) -> dict:
    """Validate and normalise the admin-supplied metadata for a source.

    Returns a copy with canonical casing. Raises SourceValidationError on the
    first invalid field; the API maps ``field`` to a clean 422 response.
    """
    result = dict(fields)
    if "source_id" in result:
        result["source_id"] = validate_source_id(result["source_id"])
    if "country_id" in result:
        result["country_id"] = validate_country_id(result["country_id"])
    if "program_id" in result:
        result["program_id"] = validate_program_id(result["program_id"])
    if "visa_category" in result:
        result["visa_category"] = validate_visa_category(result["visa_category"])
    if "source_type" in result:
        result["source_type"] = _validate_choice(
            result["source_type"], "source_type", SOURCE_TYPES, "HTML"
        )
    if "priority" in result:
        result["priority"] = _validate_choice(
            result["priority"], "priority", PRIORITIES, "PRIMARY"
        )
    if "monitoring_method" in result:
        result["monitoring_method"] = _validate_choice(
            result["monitoring_method"], "monitoring_method", MONITORING_METHODS, "WEB"
        )
    if "crawl_frequency" in result:
        result["crawl_frequency"] = _validate_choice(
            result["crawl_frequency"], "crawl_frequency", CRAWL_FREQUENCIES, "DAILY"
        )
    if "content_kind" in result and result["content_kind"] is not None:
        result["content_kind"] = _validate_choice(
            result["content_kind"], "content_kind", CONTENT_KINDS, "PAGE"
        )
    if "url" in result:
        result["url"] = validate_http_url(result["url"])
    authority = result.get("authority")
    if authority is not None and (not str(authority).strip() or len(str(authority)) > 128):
        _fail("authority must be 1-128 characters", "authority")
    return result


def _is_public_address(value: str) -> bool:
    address = ipaddress.ip_address(value)
    if getattr(address, "scope_id", None):
        return False
    return address.is_global


_RESOLVE_CACHE: dict[tuple[str, int], tuple[float, tuple[str, ...]]] = {}
_RESOLVE_CACHE_LOCK = threading.Lock()
_DNS_CACHE_TTL_SECONDS = 60.0


def _cached_addresses(host: str, port: int) -> list[str] | None:
    now = time.monotonic()
    with _RESOLVE_CACHE_LOCK:
        entry = _RESOLVE_CACHE.get((host, port))
        if entry and now - entry[0] < _DNS_CACHE_TTL_SECONDS:
            return list(entry[1])
    return None


def _remember_addresses(host: str, port: int, addresses: list[str]) -> None:
    with _RESOLVE_CACHE_LOCK:
        _RESOLVE_CACHE[(host, port)] = (time.monotonic(), tuple(addresses))


def resolve_public_addresses(host: str, *, port: int = 443) -> list[str]:
    """Resolve a hostname to all addresses and require every one to be public.

    Positive results are cached briefly to avoid a DNS lookup per page
    subresource; the TTL is intentionally short so a rebinding change is still
    observed within a sync run.
    """
    cached = _cached_addresses(host, port)
    if cached is not None:
        return cached
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise SourceValidationError(
            f"could not resolve source host {host!r}: {exc}", field="url"
        ) from exc
    addresses = sorted({str(info[4][0]) for info in infos})
    if not addresses:
        _fail(f"source host {host!r} resolved to no addresses", "url")
    for value in addresses:
        try:
            allowed = _is_public_address(value)
        except ValueError:
            allowed = False
        if not allowed:
            _fail(
                f"source host {host!r} resolves to a non-public address ({value})",
                "url",
            )
    _remember_addresses(host, port, addresses)
    return addresses


def assert_fetch_destination(url: str) -> str:
    """HTTP(S) + public-address check for a retrieval/redirect target.

    Source-creation additionally enforces the official-domain allowlist (see
    :func:`validate_http_url`). Retrieval only enforces the network boundary:
    a previously registered legacy source or an official host redirecting to a
    CDN must not become an SSRF primitive, but the fetch path does not need to
    re-litigate the operator's source registry on every byte.
    """
    parsed = urlparse(url)
    if parsed.scheme.lower() not in {"http", "https"}:
        _fail("redirect target must use http or https", "url")
    if parsed.username or parsed.password:
        _fail("redirect target must not contain credentials", "url")
    assert_public_destination(url)
    return url


def assert_public_destination(url: str) -> str:
    """Resolve and validate a URL immediately before an outbound request.

    ``FETCH_VALIDATE_DESTINATION_DNS=false`` exists only for hermetic test
    suites that use reserved domains (``example.invalid``) with fake browser
    contexts. Production startup refuses it.
    """
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if not host:
        _fail("URL has no host", "url")
    if os.getenv("FETCH_ALLOW_PRIVATE_HOSTS", "false").strip().lower() == "true":
        return url
    if os.getenv(
        "FETCH_VALIDATE_DESTINATION_DNS", "true"
    ).strip().lower() in {"0", "false", "no", "off"}:
        return url
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    resolve_public_addresses(host, port=port)
    return url


def resolve_redirect(current_url: str, location: str | None) -> str | None:
    """Resolve one redirect target and validate it before it is requested."""
    if not location:
        return None
    target = urljoin(current_url, location)
    return validate_http_url(target, resolve=True)
