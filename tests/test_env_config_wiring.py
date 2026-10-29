"""Tests for environment-driven LLM configuration wiring.

Covers:
* EXTRACTION_MODEL selects the requirement-extraction model (default is Cohere);
  the factory supports Cohere, OpenAI and Gemini models and rejects the
  test-only mock-llm-v1 and any other unsupported provider model.
  OpenAILLMClient itself only accepts OpenAI model names (OPENAI_API_KEY required).
* LLM_DAILY_BUDGET_USD / LLM_MONTHLY_BUDGET_USD are read into the discovery
  BudgetTracker, with the module fallback preserved when unset (including 0.0
  as a legitimate "block all spend" value).
* The seed registry exposes WORK/STUDENT/VISITOR immigration_program rows so
  discovered sources in those categories have a program anchor to attach to.
All tests are offline: no real network or billed LLM call is made.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from source_discovery import _discovery_budget  # noqa: E402
from source_discovery import _resolve_category  # noqa: E402
from utils.fetch_document import _extraction_model  # noqa: E402
from extraction.llm_client import OpenAILLMClient  # noqa: E402


# ---------------------------------------------------------------------------
# EXTRACTION_MODEL wiring
# ---------------------------------------------------------------------------
class TestExtractionModelEnv:
    def test_defaults_to_cohere_when_unset(self, monkeypatch):
        monkeypatch.delenv("EXTRACTION_MODEL", raising=False)
        assert _extraction_model() == "command-a-03-2025"

    def test_reads_env_when_set(self, monkeypatch):
        monkeypatch.setenv("EXTRACTION_MODEL", "gpt-4o")
        assert _extraction_model() == "gpt-4o"

    def test_model_routes_to_openai_client(self, monkeypatch):
        """An OpenAI model name constructs an OpenAILLMClient (not MockLLMClient)."""
        from extraction.llm_client import OpenAILLMClient, MockLLMClient
        client = OpenAILLMClient(api_key="dummy", model="gpt-4o-mini")
        assert isinstance(client, OpenAILLMClient)
        assert not isinstance(client, MockLLMClient)

    def test_default_is_openai_model(self):
        from extraction.llm_client import DEFAULT_OPENAI_MODEL
        assert DEFAULT_OPENAI_MODEL == "gpt-4o-mini"
        assert OpenAILLMClient._is_openai_model(DEFAULT_OPENAI_MODEL)

    def test_mock_model_is_rejected(self):
        from extraction.llm_client import OpenAILLMClient
        assert not OpenAILLMClient._is_openai_model("mock-llm-v1")
        with pytest.raises(ValueError, match="Unsupported extraction model"):
            OpenAILLMClient(api_key="dummy", model="mock-llm-v1")

    def test_non_openai_model_is_rejected(self):
        from extraction.llm_client import OpenAILLMClient
        for bad in ("gemini-flash-lite-latest", "claude-sonnet-4-6"):
            assert not OpenAILLMClient._is_openai_model(bad)
            with pytest.raises(ValueError, match="Unsupported extraction model"):
                OpenAILLMClient(api_key="dummy", model=bad)

    def test_missing_api_key_fails_fast(self):
        from extraction.llm_client import OpenAILLMClient
        with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
            OpenAILLMClient(api_key=None, model="gpt-4o-mini")

    def test_factory_routes_gemini_model_to_gemini_client(self, monkeypatch):
        # create_llm_client is a separate module attribute aliased to the real
        # get_client function; the autouse unit-test fixture only swaps the
        # get_client attribute itself, so this reaches the real factory logic.
        from extraction.llm_client import create_llm_client, GeminiLLMClient
        monkeypatch.setenv("GEMINI_API_KEY", "dummy-gemini-key")
        client = create_llm_client("gemini-flash-lite-latest", visa_category="SKILLED")
        assert isinstance(client, GeminiLLMClient)
        # Governance is wired the same way as OpenAILLMClient (budget/rate-limit/cache).
        assert client.governance is not None

    def test_default_routes_to_cohere_client(self, monkeypatch):
        from extraction.llm_client import (
            CohereLLMClient,
            DEFAULT_EXTRACTION_MODEL,
            create_llm_client,
        )
        monkeypatch.delenv("EXTRACTION_MODEL", raising=False)
        monkeypatch.setenv("COHERE_API_KEY", "dummy-cohere-key")
        client = create_llm_client()
        assert DEFAULT_EXTRACTION_MODEL == "command-a-03-2025"
        assert isinstance(client, CohereLLMClient)
        assert client.governance is not None

    def test_cohere_missing_api_key_fails_fast(self, monkeypatch):
        from extraction.llm_client import CohereLLMClient
        monkeypatch.delenv("COHERE_API_KEY", raising=False)
        with pytest.raises(RuntimeError, match="COHERE_API_KEY"):
            CohereLLMClient(api_key=None, model="command-a-03-2025")

    def test_cohere_response_is_decoded_without_network(self, monkeypatch):
        import requests
        from extraction.llm_client import CohereLLMClient

        class FakeResponse:
            status_code = 200
            headers = {}

            def raise_for_status(self):
                pass

            def json(self):
                return {
                    "message": {"content": [{"type": "text", "text": '{"requirements": []}'}]},
                    "usage": {"tokens": {"input_tokens": 12, "output_tokens": 4}},
                }

        calls = []

        def fake_post(*args, **kwargs):
            calls.append(kwargs)
            return FakeResponse()

        monkeypatch.setattr(requests, "post", fake_post)
        client = CohereLLMClient(api_key="test-key", model="command-a-03-2025")
        content, tokens_in, tokens_out, retries = client._call_cohere({"source": "fixture"})

        assert content == '{"requirements": []}'
        assert (tokens_in, tokens_out, retries) == (12, 4, 0)
        assert calls[0]["json"]["response_format"] == {"type": "json_object"}
        assert calls[0]["headers"]["Authorization"] == "Bearer test-key"

    def test_factory_still_rejects_anthropic_model(self):
        from extraction.llm_client import create_llm_client
        with pytest.raises(ValueError, match="Unsupported extraction model"):
            create_llm_client("claude-sonnet-4-6")

    def test_gemini_missing_api_key_fails_fast(self, monkeypatch):
        from extraction.llm_client import GeminiLLMClient
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
        with pytest.raises(RuntimeError, match="GEMINI_API_KEY"):
            GeminiLLMClient(api_key=None, model="gemini-flash-lite-latest")


# ---------------------------------------------------------------------------
# Discovery budget wiring
# ---------------------------------------------------------------------------
class TestDiscoveryBudgetEnv:
    def test_uses_env_values(self, monkeypatch):
        monkeypatch.setenv("LLM_DAILY_BUDGET_USD", "2.50")
        monkeypatch.setenv("LLM_MONTHLY_BUDGET_USD", "75.0")
        bt = _discovery_budget()
        assert bt.daily_limit == 2.5
        assert bt.monthly_limit == 75.0

    def test_falls_back_when_unset(self, monkeypatch):
        monkeypatch.delenv("LLM_DAILY_BUDGET_USD", raising=False)
        monkeypatch.delenv("LLM_MONTHLY_BUDGET_USD", raising=False)
        bt = _discovery_budget()
        assert bt.daily_limit == 10.0
        assert bt.monthly_limit == 200.0

    def test_zero_env_is_respected_not_overridden(self, monkeypatch):
        monkeypatch.setenv("LLM_DAILY_BUDGET_USD", "0.0")
        monkeypatch.setenv("LLM_MONTHLY_BUDGET_USD", "0.0")
        bt = _discovery_budget()
        assert bt.daily_limit == 0.0
        assert bt.monthly_limit == 0.0

    def test_invalid_env_falls_back(self, monkeypatch):
        monkeypatch.setenv("LLM_DAILY_BUDGET_USD", "not-a-number")
        monkeypatch.setenv("LLM_MONTHLY_BUDGET_USD", "also-bad")
        bt = _discovery_budget()
        assert bt.daily_limit == 10.0
        assert bt.monthly_limit == 200.0


# ---------------------------------------------------------------------------
# WORK/STUDENT/VISITOR category routing used during discovery
# ---------------------------------------------------------------------------
class TestCategoryRouting:
    def test_work_student_visitor_categories_resolve(self):
        for kw, cat in [("TSS 482 temporary skill shortage", "WORK"),
                        ("student visa 500", "STUDENT"),
                        ("visitor visa 600 tourist", "VISITOR")]:
            assert _resolve_category(kw, None) == cat

    def test_ambiguous_ties_stay_unresolved(self):
        assert _resolve_category("visa visa", "VISITOR") is None


# ---------------------------------------------------------------------------
# Seed registry exposes WORK/STUDENT/VISITOR program anchors
# ---------------------------------------------------------------------------
class TestSeedRegistryProgramAnchors:
    def test_seed_contains_work_student_visitor_programs(self):
        from seed_registry import load_seed
        data = load_seed()
        by_id = {p["program_id"]: p for p in data["immigration_programs"]}
        for pid, cat, subclass in [
            ("WORK_482", "WORK", "482"),
            ("STUDENT_500", "STUDENT", "500"),
            ("VISITOR_600", "VISITOR", "600"),
        ]:
            assert pid in by_id, f"missing program row {pid}"
            assert by_id[pid]["visa_category"] == cat
            assert by_id[pid]["subclass_code"] == subclass

    def test_upsert_programs_loads_work_student_visitor(self):
        from sqlalchemy import create_engine
        from sqlalchemy.pool import StaticPool

        from db.models import Base, ImmigrationProgram
        from seed_registry import _SQLAlchemyAdapter, load_seed, upsert_programs

        engine = create_engine(
            "sqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(bind=engine)
        conn = engine.connect()
        adapter = _SQLAlchemyAdapter(conn)

        data = load_seed()
        programs = [
            p for p in data["immigration_programs"]
            if p["program_id"] in {"WORK_482", "STUDENT_500", "VISITOR_600"}
        ]
        count = upsert_programs(adapter, programs)
        from sqlalchemy import text
        user_conn = conn.execute(
            text("SELECT program_id, visa_category, subclass_code "
                 "FROM immigration_programs ORDER BY program_id")
        )
        rows = {(r[0], r[1], r[2]) for r in user_conn.fetchall()}
        assert len(rows) == count == 3
        assert ("WORK_482", "WORK", "482") in rows
        assert ("STUDENT_500", "STUDENT", "500") in rows
        assert ("VISITOR_600", "VISITOR", "600") in rows
        conn.close()
