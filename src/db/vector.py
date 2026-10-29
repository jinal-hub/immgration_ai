"""A fixed-dimension vector column, without the pgvector Python package.

The `pgvector` package is the usual way to bind a PostgreSQL ``vector``
column from SQLAlchemy. This project does not otherwise need it, and the
type is small enough to own outright:

* On PostgreSQL the column is a real ``VECTOR(n)``. Values are written in
  pgvector's own text form (``[0.1,0.2]``) and read back the same way.
* On every other dialect - the SQLite the unit tests run on - the column
  degrades to text holding JSON, so one model definition works in both
  places and the test suite needs no special casing.

The dimension is fixed at the column level, and a value of the wrong
length is rejected on the way in rather than silently stored: a vector of
the wrong dimension cannot be compared to anything.
"""
from __future__ import annotations

import json
from typing import Any

from sqlalchemy import Text
from sqlalchemy.types import TypeDecorator, UserDefinedType


# The dimension of the stored embeddings. Cohere Embed v4 is configured to
# return this width by default.
# Changing it means a new column and a re-embed of every chunk, which is
# why it is a constant rather than a setting.
EMBEDDING_DIMENSIONS = 1536


class _PGVector(UserDefinedType):
    """The PostgreSQL ``VECTOR(n)`` type itself."""

    cache_ok = True

    def __init__(self, dim: int) -> None:
        self.dim = dim

    def get_col_spec(self, **kw) -> str:
        return f"VECTOR({self.dim})"


class Vector(TypeDecorator):
    """A fixed-dimension float vector column."""

    impl = Text
    cache_ok = True

    def __init__(self, dim: int) -> None:
        self.dim = dim
        super().__init__()

    def load_dialect_impl(self, dialect):
        if dialect.name == "postgresql":
            return dialect.type_descriptor(_PGVector(self.dim))
        return dialect.type_descriptor(Text())

    def bind_processor(self, dialect):
        def process(value: Any) -> str | None:
            if value is None:
                return None
            values = [float(v) for v in value]
            if len(values) != self.dim:
                raise ValueError(
                    f"vector must have exactly {self.dim} dimensions, got {len(values)}"
                )
            if dialect.name == "postgresql":
                # pgvector's text input form: [0.1,0.2]
                return "[" + ",".join(repr(v) for v in values) + "]"
            return json.dumps(values)
        return process

    def process_result_value(self, value: Any, dialect) -> list[float] | None:
        if value is None:
            return None
        if isinstance(value, (list, tuple)):
            return [float(v) for v in value]
        text = value if isinstance(value, str) else value.decode("utf-8")
        text = text.strip()
        if not text:
            return None
        try:
            return [float(v) for v in json.loads(text)]
        except (json.JSONDecodeError, TypeError, ValueError):
            # A vector that is not JSON is not a vector we wrote; refusing to
            # guess is better than returning a half-parsed list.
            raise ValueError(f"could not read vector value: {text!r}")
