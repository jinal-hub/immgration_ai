"""The embedding client (Phase 3b).

The rules this file pins down:

* Embeddings are off unless the operator turns them on.
* There is no silent fallback to the mock. A missing API key fails.
* The offline mock is deterministic and gives similar vectors to texts
  that share vocabulary - that is what makes the search path testable
  without a network call.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from db.vector import EMBEDDING_DIMENSIONS  # noqa: E402
from extraction.embeddings import (  # noqa: E402
    DEFAULT_EMBEDDING_MODEL,
    MOCK_EMBEDDING_MODEL,
    CohereEmbeddingClient,
    EmbeddingError,
    MockEmbeddingClient,
    cosine_similarity,
    embedding_model,
    embeddings_enabled,
    get_embedding_client,
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("EMBEDDINGS_ENABLED", raising=False)
    monkeypatch.delenv("EMBEDDING_MODEL", raising=False)
    monkeypatch.delenv("COHERE_API_KEY", raising=False)


class TestTheOffSwitch:
    def test_embeddings_are_off_by_default(self):
        assert embeddings_enabled() is False

    def test_they_can_be_turned_on(self, monkeypatch):
        monkeypatch.setenv("EMBEDDINGS_ENABLED", "true")
        assert embeddings_enabled() is True

    @pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "True"])
    def test_the_usual_spellings_all_count(self, monkeypatch, value):
        monkeypatch.setenv("EMBEDDINGS_ENABLED", value)
        assert embeddings_enabled() is True

    @pytest.mark.parametrize("value", ["false", "0", "no", "", "maybe"])
    def test_anything_else_stays_off(self, monkeypatch, value):
        monkeypatch.setenv("EMBEDDINGS_ENABLED", value)
        assert embeddings_enabled() is False

    def test_the_factory_refuses_while_off(self):
        with pytest.raises(EmbeddingError, match="disabled"):
            get_embedding_client()


class TestTheMock:
    def test_it_is_asked_for_by_name(self, monkeypatch):
        monkeypatch.setenv("EMBEDDINGS_ENABLED", "true")
        monkeypatch.setenv("EMBEDDING_MODEL", MOCK_EMBEDDING_MODEL)
        client = get_embedding_client()
        assert isinstance(client, MockEmbeddingClient)

    def test_it_returns_one_vector_per_text(self):
        client = MockEmbeddingClient()
        vectors = client.embed(["one", "two", "three"])
        assert len(vectors) == 3
        for vector in vectors:
            assert len(vector) == EMBEDDING_DIMENSIONS

    def test_it_is_deterministic(self):
        client = MockEmbeddingClient()
        first = client.embed_query("you must be under 45 years of age")
        second = client.embed_query("you must be under 45 years of age")
        assert first == second

    def test_texts_that_share_vocabulary_are_similar(self):
        client = MockEmbeddingClient()
        a = client.embed_query("you must be under 45 years of age")
        b = client.embed_query("the applicant must be under 45 years old")
        c = client.embed_query("genuine temporary entrant requirements apply")
        assert cosine_similarity(a, b) > cosine_similarity(a, c)

    def test_unrelated_texts_are_not_similar(self):
        client = MockEmbeddingClient()
        a = client.embed_query("you must be under 45 years of age")
        b = client.embed_query("genuine temporary entrant requirements apply")
        assert cosine_similarity(a, b) < 0.2

    def test_an_empty_text_does_not_explode(self):
        client = MockEmbeddingClient()
        vector = client.embed_query("")
        assert len(vector) == EMBEDDING_DIMENSIONS

    def test_a_batch_keeps_its_order(self):
        client = MockEmbeddingClient()
        vectors = client.embed(["age limit", "points test"])
        assert vectors[0] == client.embed_query("age limit")
        assert vectors[1] == client.embed_query("points test")


class TestTheRealClient:
    def test_factory_uses_cohere_when_enabled(self, monkeypatch):
        monkeypatch.setenv("EMBEDDINGS_ENABLED", "true")
        monkeypatch.setenv("COHERE_API_KEY", "test-key")
        assert isinstance(get_embedding_client(), CohereEmbeddingClient)

    def test_a_missing_api_key_fails_loudly(self, monkeypatch):
        monkeypatch.setenv("EMBEDDINGS_ENABLED", "true")
        with pytest.raises(EmbeddingError, match="COHERE_API_KEY"):
            get_embedding_client()

    def test_it_never_falls_back_to_the_mock(self, monkeypatch):
        monkeypatch.setenv("EMBEDDINGS_ENABLED", "true")
        with pytest.raises(EmbeddingError):
            get_embedding_client()

    def test_an_unsupported_model_is_refused(self, monkeypatch):
        monkeypatch.setenv("COHERE_API_KEY", "test-key")
        with pytest.raises(EmbeddingError, match="Unsupported embedding model"):
            CohereEmbeddingClient(model="command-a-03-2025")

    def test_the_default_model_fits_the_column(self):
        assert DEFAULT_EMBEDDING_MODEL == "embed-v4.0"
        client = CohereEmbeddingClient(api_key="test-key")
        assert client.model_name == DEFAULT_EMBEDDING_MODEL

    def test_document_and_query_embeddings_use_cohere_input_types(self, monkeypatch):
        import requests

        calls = []

        class FakeResponse:
            status_code = 200
            text = ""

            def json(self):
                return {"embeddings": {"float": [[0.1] * EMBEDDING_DIMENSIONS]}}

        def fake_post(*args, **kwargs):
            calls.append(kwargs)
            return FakeResponse()

        monkeypatch.setattr(requests, "post", fake_post)
        client = CohereEmbeddingClient(api_key="test-key")
        client.embed(["document chunk"])
        client.embed_query("search query")

        assert calls[0]["json"]["input_type"] == "search_document"
        assert calls[1]["json"]["input_type"] == "search_query"
        assert calls[0]["json"]["output_dimension"] == EMBEDDING_DIMENSIONS
        assert calls[0]["headers"]["Authorization"] == "Bearer test-key"


class TestTheModelSetting:
    def test_the_default_model_is_the_one_that_fits(self, monkeypatch):
        monkeypatch.delenv("EMBEDDING_MODEL", raising=False)
        assert embedding_model() == DEFAULT_EMBEDDING_MODEL

    def test_the_model_can_be_overridden(self, monkeypatch):
        monkeypatch.setenv("EMBEDDING_MODEL", MOCK_EMBEDDING_MODEL)
        assert embedding_model() == MOCK_EMBEDDING_MODEL
