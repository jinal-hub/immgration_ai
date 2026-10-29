"""Embeddings for semantic search.

Semantic search needs the chunk text as a vector, and a vector needs a
model. This module is the only place that talks to an embedding provider.

Two rules shape everything here:

* **Embeddings are off by default.** ``EMBEDDINGS_ENABLED`` has to be set
  explicitly. Nothing in the pipeline embeds a chunk unless the operator
  asked for it, and the chunk's embedding column stays NULL until then.
* **There is no silent fallback.** A missing API key fails loudly instead
  of quietly producing fake vectors. The offline mock exists, but it has
  to be asked for by name (``EMBEDDING_MODEL=mock-embedding-v1``) - the
  same rule the extraction client follows.

The mock is not a stub. It is a hashed bag of words, so two texts that
share vocabulary end up with similar vectors. That is enough to exercise
the whole search path - the database column, the cosine comparison, the
ranking - offline and deterministically, and it makes the search feature
demonstrable without a network call.
"""
from __future__ import annotations

import hashlib
import math
import os
import re
from abc import ABC, abstractmethod

from db.vector import EMBEDDING_DIMENSIONS
from logging_config import get_logger

logger = get_logger(__name__)


DEFAULT_EMBEDDING_MODEL = "embed-v4.0"
MOCK_EMBEDDING_MODEL = "mock-embedding-v1"

# Cohere Embed v4 supports a 1536-dimensional output matching the stored column.
SUPPORTED_EMBEDDING_MODELS = {
    "embed-v4.0": EMBEDDING_DIMENSIONS,
    "mock-embedding-v1": EMBEDDING_DIMENSIONS,
}

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def embeddings_enabled() -> bool:
    """Whether the operator has turned embeddings on."""
    return os.getenv("EMBEDDINGS_ENABLED", "false").strip().lower() in ("1", "true", "yes")


def embedding_model() -> str:
    return os.getenv("EMBEDDING_MODEL", DEFAULT_EMBEDDING_MODEL).strip()


class EmbeddingError(RuntimeError):
    """Anything that stops an embedding from being produced."""


class EmbeddingClient(ABC):
    """Turns text into vectors."""

    model_name: str = ""

    @abstractmethod
    def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed a batch of texts. Returns one vector per input, in order."""

    def embed_query(self, text: str) -> list[float]:
        vectors = self.embed([text])
        if not vectors:
            raise EmbeddingError("embedding returned no vectors")
        return vectors[0]


class MockEmbeddingClient(EmbeddingClient):
    """A deterministic, offline, hash-based embedding.

    Each token is hashed to a coordinate with a sign (the signed hashing
    trick), so two texts sharing vocabulary land near each other and the
    cosine similarity between them is meaningful. It is not a language
    model and does not pretend to be one - it is the offline stand-in that
    lets the search path be tested and demonstrated without a network call.
    """

    def __init__(self, model: str = MOCK_EMBEDDING_MODEL, dim: int = EMBEDDING_DIMENSIONS):
        if dim <= 0:
            raise ValueError("embedding dimension must be positive")
        self.model_name = model
        self.dim = dim

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._vector(text) for text in texts]

    def _vector(self, text: str) -> list[float]:
        vector = [0.0] * self.dim
        for token in _TOKEN_RE.findall((text or "").lower()):
            digest = hashlib.sha256(token.encode("utf-8")).digest()
            index = int.from_bytes(digest[:4], "big") % self.dim
            sign = 1.0 if digest[4] % 2 == 0 else -1.0
            vector[index] += sign
        norm = math.sqrt(sum(v * v for v in vector))
        if norm == 0.0:
            return vector
        return [v / norm for v in vector]


class CohereEmbeddingClient(EmbeddingClient):
    """Cohere Embed v4 vectors for extracted document chunks and search queries."""

    ENDPOINT = "https://api.cohere.com/v2/embed"

    def __init__(self, api_key: str | None = None, model: str | None = None,
                 timeout: int = 120):
        self.api_key = api_key or os.environ.get("COHERE_API_KEY")
        self.model_name = model or embedding_model()
        expected = SUPPORTED_EMBEDDING_MODELS.get(self.model_name)
        if expected is None:
            raise EmbeddingError(
                f"Unsupported embedding model {self.model_name!r}. "
                f"Supported: {', '.join(sorted(SUPPORTED_EMBEDDING_MODELS))}."
            )
        if expected != EMBEDDING_DIMENSIONS:
            raise EmbeddingError(
                f"Embedding model {self.model_name!r} produces {expected} dimensions, "
                f"but the chunk embedding column stores {EMBEDDING_DIMENSIONS}. "
                f"Use a model of the same width, or change the column."
            )
        if not self.api_key:
            raise EmbeddingError(
                "COHERE_API_KEY is required for real embeddings. Set it in the "
                "environment; never hardcode it. To work offline, ask for the "
                f"mock explicitly with EMBEDDING_MODEL={MOCK_EMBEDDING_MODEL}."
            )
        self.timeout = timeout

    def embed(self, texts: list[str]) -> list[list[float]]:
        return self._embed(texts, input_type="search_document")

    def embed_query(self, text: str) -> list[float]:
        vectors = self._embed([text], input_type="search_query")
        if not vectors:
            raise EmbeddingError("embedding returned no vectors")
        return vectors[0]

    def _embed(self, texts: list[str], *, input_type: str) -> list[list[float]]:
        if not texts:
            return []
        import requests  # local import: only needed when this client is used

        response = requests.post(
            self.ENDPOINT,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": self.model_name,
                "texts": texts,
                "input_type": input_type,
                "embedding_types": ["float"],
                "output_dimension": EMBEDDING_DIMENSIONS,
            },
            timeout=self.timeout,
        )
        if response.status_code != 200:
            raise EmbeddingError(
                f"Embedding request failed with HTTP {response.status_code}: "
                f"{response.text[:200]}"
            )
        payload = response.json()
        data = (payload.get("embeddings") or {}).get("float") or []
        if len(data) != len(texts):
            raise EmbeddingError(
                f"Cohere returned {len(data)} vectors for {len(texts)} inputs"
            )
        vectors = []
        for item in data:
            vector = [float(v) for v in item]
            if len(vector) != EMBEDDING_DIMENSIONS:
                raise EmbeddingError(
                    f"Cohere returned a {len(vector)}-dimension vector, "
                    f"expected {EMBEDDING_DIMENSIONS}"
                )
            vectors.append(vector)
        return vectors


def get_embedding_client() -> EmbeddingClient:
    """The client for the current configuration.

    Fails loudly when embeddings are disabled or the provider is
    misconfigured. It never quietly substitutes the mock.
    """
    if not embeddings_enabled():
        raise EmbeddingError(
            "Embeddings are disabled. Set EMBEDDINGS_ENABLED=true to turn them on "
            "(this embeds every chunk through a paid API - it is off by default)."
        )
    model = embedding_model()
    if model == MOCK_EMBEDDING_MODEL:
        return MockEmbeddingClient(model=model)
    return CohereEmbeddingClient(model=model)


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """Cosine similarity of two equal-length vectors."""
    if len(a) != len(b):
        raise ValueError("vectors must have the same length")
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


# ---------------------------------------------------------------------------
# Backfill: give the stored chunks an embedding
# ---------------------------------------------------------------------------
def backfill_embeddings(
    session,
    client: EmbeddingClient,
    *,
    batch_size: int = 32,
    limit: int | None = None,
    document_id: str | None = None,
    program_id: str | None = None,
) -> dict:
    """Embed the chunks that do not have an embedding yet.

    Chunks that already carry one are left alone: an embedding is a
    function of the text, and the text of an existing chunk has not
    changed, so re-embedding it would cost money to produce the same
    numbers.

    Returns a summary: how many chunks were embedded, how many were
    skipped, and how many were left over.
    """
    from sqlalchemy import select, text as sql_text

    from db.models import DocumentChunk, GovernmentDocument

    clauses = [DocumentChunk.embedding.is_(None)]
    query = select(DocumentChunk).where(*clauses)
    if document_id:
        query = query.where(DocumentChunk.document_id == document_id)
    if program_id:
        query = query.where(
            DocumentChunk.document_id.in_(
                select(GovernmentDocument.document_id).where(
                    GovernmentDocument.program_id == program_id
                )
            )
        )
    query = query.order_by(DocumentChunk.chunk_id)
    if limit:
        query = query.limit(limit)

    chunks = list(session.execute(query).scalars())
    if not chunks:
        return {"embedded": 0, "skipped": 0, "remaining": 0, "total_chunks": 0}

    embedded = 0
    for start in range(0, len(chunks), batch_size):
        batch = chunks[start:start + batch_size]
        vectors = client.embed([chunk.raw_text for chunk in batch])
        for chunk, vector in zip(batch, vectors):
            chunk.embedding = vector
            embedded += 1
        session.flush()

    remaining_row = session.execute(sql_text(
        "SELECT count(*) FROM document_chunks WHERE embedding IS NULL"
    )).first()

    return {
        "embedded": embedded,
        "skipped": 0,
        "remaining": int(remaining_row[0]) if remaining_row else 0,
        "total_chunks": embedded,
    }


def main(argv: list[str] | None = None) -> int:
    """CLI: embed the chunks that have no embedding yet.

    Usage:
        python -m src.extraction.embeddings --backfill [--limit N] [--document ID]
    """
    import argparse

    parser = argparse.ArgumentParser(
        description="Embed the document chunks that have no embedding yet."
    )
    parser.add_argument("--backfill", action="store_true",
                        help="embed every chunk that has no embedding")
    parser.add_argument("--limit", type=int, default=None,
                        help="stop after this many chunks")
    parser.add_argument("--document", default=None,
                        help="only embed chunks of this document")
    parser.add_argument("--program", default=None,
                        help="only embed chunks of this program")
    args = parser.parse_args(argv)

    if not args.backfill:
        parser.print_help()
        return 1

    from db.connection import get_engine, get_session, get_database_url

    url = get_database_url()
    if not url:
        print("DATABASE_URL is not set - nothing to embed.", flush=True)
        return 1

    engine = get_engine(url)
    client = get_embedding_client()
    with get_session(engine) as session:
        summary = backfill_embeddings(
            session, client,
            limit=args.limit,
            document_id=args.document,
            program_id=args.program,
        )
        session.commit()
    print(summary, flush=True)
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
