"""Global test fixtures — sets TESTING=true so lifespan skips DB + scheduler init.

Every test gets its own fresh SQLite in-memory engine.
"""
from __future__ import annotations

import os
import pytest

# ---------------------------------------------------------------------------
# Hermetic test environment.
# ---------------------------------------------------------------------------
# The requirement-extraction model is env-configurable (EXTRACTION_MODEL) so a
# developer's local .env can point it at a real (billed) provider model. Tests
# must never make real LLM calls UNLESS the developer explicitly opts in.
#
#   Default (offline) mode:  EXTRACTION_MODEL is pinned to Cohere and provider
#   keys are stripped, so a test fails fast if it tries to reach a real provider
#   — and the autouse fixture below
#   swaps in the deterministic MockLLMClient for every unit test.
#
#   Live mode:                set PYTEST_LLM_LIVE=1 and export a provider key.
#   Integration tests marked ``integration`` (e.g. TestOpenAIRealLLM) then run
#   against the configured live model and are skipped when the key is absent.
#
# These lines run at module load (before any app module is imported), so a local
# .env's EXTRACTION_MODEL / provider keys can never leak in accidentally.
# ---------------------------------------------------------------------------
os.environ.setdefault("TESTING", "true")
_LIVE = os.environ.get("PYTEST_LLM_LIVE") == "1"
if _LIVE:
    os.environ.setdefault("EXTRACTION_MODEL", "command-a-03-2025")
else:
    os.environ["EXTRACTION_MODEL"] = "command-a-03-2025"
    for _key in (
        "COHERE_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY",
        "GOOGLE_API_KEY", "ANTHROPIC_API_KEY",
    ):
        os.environ.pop(_key, None)


# ---------------------------------------------------------------------------
# Ensure lifespan never touches real DATABASE_URL during tests
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True, scope="session")
def _set_testing_env():
    os.environ["TESTING"] = "true"
    # POST /auth/register is gated behind ALLOW_PUBLIC_REGISTRATION in
    # production (default false). A large number of unrelated test files use
    # self-registration purely as a way to mint a test user/admin, so the
    # test session opts in globally here rather than requiring every one of
    # them to set the flag individually. The gate itself is still covered by
    # a dedicated test that explicitly overrides this back to false/unset.
    os.environ["ALLOW_PUBLIC_REGISTRATION"] = "true"
    # A developer's shell must not be able to point the suite's fetches at a
    # real egress proxy (FETCH_PROXY_URL). Remove it for the whole session; the
    # proxy tests set it back explicitly and per-test.
    os.environ.pop("FETCH_PROXY_URL", None)
    # Unit tests use reserved domains (example.gov.au) with fake contexts.
    # Production startup refuses this flag; the SSRF/DNS tests exercise the
    # resolver directly rather than relying on a live lookup.
    os.environ["FETCH_VALIDATE_DESTINATION_DNS"] = "false"
    # The historical suite registers users with 6-7 character passwords. Keep
    # that convenience local to tests; the production/API default remains 8 and
    # the policy itself is covered explicitly in test_auth.py.
    os.environ.setdefault("PASSWORD_MIN_LENGTH", "6")
    yield
    for _key in (
        "TESTING", "ALLOW_PUBLIC_REGISTRATION", "FETCH_VALIDATE_DESTINATION_DNS",
        "FETCH_ALLOW_PRIVATE_HOSTS", "PASSWORD_MIN_LENGTH",
    ):
        os.environ.pop(_key, None)


# ---------------------------------------------------------------------------
# Unit tests must never hit a real LLM: swap the provider factory out for
# the deterministic MockLLMClient wherever production code calls get_client.
# Explicitly marked ``integration`` tests skip this swap so they can call the
# real provider API (when PYTEST_LLM_LIVE=1 and the provider key is exported).
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _isolate_raw_snapshots(tmp_path, monkeypatch):
    """Never let a unit test publish into the developer's real raw/ archive."""
    monkeypatch.setenv("RAW_SNAPSHOT_DIR", str(tmp_path / "raw"))


@pytest.fixture(autouse=True)
def _reset_login_throttle():
    from services import login_throttle

    login_throttle.reset_all()
    yield
    login_throttle.reset_all()


@pytest.fixture(autouse=True)
def _patch_llm_client_for_unit_tests(request, monkeypatch):
    if request.node.get_closest_marker("integration"):
        yield
        return
    from extraction import llm_client
    from extraction.llm_client import MockLLMClient

    monkeypatch.setattr(llm_client, "get_client", MockLLMClient)
    # get_client is imported into these modules at their top level, so each
    # module attribute must be patched individually for the factory swap to
    # take effect at those call sites.
    import importlib
    for mod_name in ("utils.fetch_document", "source_discovery"):
        try:
            mod = importlib.import_module(mod_name)
            monkeypatch.setattr(mod, "get_client", MockLLMClient)
        except Exception:
            pass
    yield


# ---------------------------------------------------------------------------
# UUID validation helper (shared across all route test modules)
# ---------------------------------------------------------------------------
def parse_uuid_or_404(id_str: str):
    """Parse a string to UUID, raising ValueError if invalid."""
    from uuid import UUID
    try:
        return UUID(id_str)
    except ValueError:
        raise ValueError(f"Invalid UUID: {id_str}")