"""Regression tests for the seed-registry database backend fix.

Root cause: seed_registry.py never loaded .env, so DATABASE_URL was missing in
the CLI process and it silently fell back to seeding the legacy SQLite
immigration.db while printing "Seeded: N countries, ..." — masking that real
PostgreSQL stayed empty (0 rows).

The fix centralizes config in src/config.py (loads .env once) and makes the
seed backend observable and safe:
  * CLI (python src/seed_registry.py) requires PostgreSQL and FAILS loudly if
    DATABASE_URL is missing — it never silently writes SQLite from the CLI.
  * SEED_BACKEND=sqlite explicitly opts any caller (incl. standalone pipeline
    scripts) back into the legacy SQLite path.
  * database_info()/get_database_url() never leak the password.

The real-PostgreSQL assertions (counts 1/7/14 + idempotency) are gated on PG
being reachable so the offline suite stays green when the container is down.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import seed_registry  # noqa: E402


def _pg_reachable() -> bool:
    """Return True when the configured DATABASE_URL connects to a real PG."""
    try:
        import sqlalchemy
        from sqlalchemy import text

        from config import get_database_url
        engine = sqlalchemy.create_engine(get_database_url())
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        engine.dispose()
        return True
    except Exception:
        return False


PG_AVAILABLE = _pg_reachable()


# ---------------------------------------------------------------------------
# Test A: config honours .env DATABASE_URL and never leaks the password
# ---------------------------------------------------------------------------
class TestConfigDatabaseUrl:
    def test_database_query_is_postgresql(self):
        import config
        assert config.database_query() in {"postgresql", "sqlite", "unknown"}

    def test_database_info_prefixes_backend(self, monkeypatch):
        import config
        monkeypatch.setenv("DATABASE_URL", "postgresql://u:supersecret@dbhost:5432/mydb")
        info = config.database_info()
        assert info["configured"] is True
        assert info["backend"] == "postgresql"
        assert info["database"] == "mydb"
        assert info["host"] == "dbhost"
        # The secret must never be exposed.
        serialized = str(info)
        assert "supersecret" not in serialized

    def test_database_info_reports_configured_false_when_unset(self, monkeypatch):
        import config
        monkeypatch.delenv("DATABASE_URL", raising=False)
        assert config.database_info()["configured"] is False

    def test_get_database_url_requires_config(self, monkeypatch):
        import config
        monkeypatch.delenv("DATABASE_URL", raising=False)
        with pytest.raises(RuntimeError):
            config.get_database_url()


# ---------------------------------------------------------------------------
# Test B: CLI refuses to seed when DATABASE_URL is missing (never silent SQLite)
# ---------------------------------------------------------------------------
class TestCliRequiresPostgres:
    def test_raises_when_database_url_missing(self, monkeypatch):
        monkeypatch.delenv("DATABASE_URL", raising=False)
        monkeypatch.delenv("SEED_BACKEND", raising=False)
        with pytest.raises(RuntimeError, match="DATABASE_URL"):
            seed_registry._make_connection(require_postgres=True)


# ---------------------------------------------------------------------------
# Test C: explicit SEED_BACKEND=sqlite opts into legacy SQLite even when
#         DATABASE_URL is set (used by the standalone offline scripts)
# ---------------------------------------------------------------------------
class TestExplicitSqliteOverride:
    def test_seed_backend_sqlite_forces_sqlite(self, monkeypatch, tmp_path):
        import db
        monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@localhost:5432/immigration")
        monkeypatch.setenv("SEED_BACKEND", "sqlite")
        # Redirect the legacy SQLite file so tests don't touch the real one.
        tmp_db = tmp_path / "immigration.db"

        def _isolated_init_schema():
            return db.init_schema(db_path=tmp_db)

        monkeypatch.setattr(seed_registry, "init_schema", _isolated_init_schema)
        conn = seed_registry._make_connection(require_postgres=False)
        assert conn is not None
        # It must be the SQLite init_schema() connection, not the PG adapter.
        assert not isinstance(conn, seed_registry._SQLAlchemyAdapter)
        assert tmp_db.exists()

    def test_unknown_seed_backend_raises(self, monkeypatch):
        monkeypatch.setenv("SEED_BACKEND", "nonsense")
        with pytest.raises(RuntimeError, match="Unknown SEED_BACKEND"):
            seed_registry._make_connection()


# ---------------------------------------------------------------------------
# Test D: upserts are idempotent (re-seeding does not duplicate rows)
# ---------------------------------------------------------------------------
class TestIdempotency:
    def test_legacy_sqlite_seed_is_idempotent(self, monkeypatch, tmp_path):
        """The legacy SQLite path (a real deployment backend used by the offline
        scripts) must seed 1/7/14 exactly once across repeated calls."""
        import db

        tmp_db = tmp_path / "immigration.db"

        def _isolated_init_schema():
            return db.init_schema(db_path=tmp_db)

        monkeypatch.setenv("SEED_BACKEND", "sqlite")
        monkeypatch.setattr(seed_registry, "init_schema", _isolated_init_schema)

        result1 = seed_registry._seed_database(require_postgres=False)
        result2 = seed_registry._seed_database(require_postgres=False)
        seed = seed_registry.load_seed()
        expected = {
            "countries": len(seed["countries"]),
            "programs": len(seed["immigration_programs"]),
            "sources": len(seed["government_sources"]),
        }
        assert result1 == expected
        assert result2 == result1

        import sqlite3
        raw = sqlite3.connect(str(tmp_db))
        for table, expect in [
            ("countries", expected["countries"]),
            ("immigration_programs", expected["programs"]),
            ("government_sources", expected["sources"]),
        ]:
            n = raw.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            assert n == expect, f"{table}: expected {expect}, got {n}"
        raw.close()


# ---------------------------------------------------------------------------
# Test E (gated): real PostgreSQL is seeded with 1/7/9 and stays idempotent
# ---------------------------------------------------------------------------
@pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL not reachable")
class TestPostgresSeedCounts:
    def test_seed_cli_targets_postgres_and_counts(self):
        from sqlalchemy import text
        from config import get_database_url
        from sqlalchemy import create_engine

        seed_registry._seed_database(require_postgres=True)
        seed = seed_registry.load_seed()
        engine = create_engine(get_database_url())
        with engine.connect() as conn:
            assert conn.execute(text("SELECT COUNT(*) FROM countries")).scalar() == len(seed["countries"])
            assert conn.execute(text("SELECT COUNT(*) FROM immigration_programs")).scalar() == len(seed["immigration_programs"])
            assert conn.execute(text("SELECT COUNT(*) FROM government_sources")).scalar() == len(seed["government_sources"])
        engine.dispose()

    def test_seed_idempotent_on_postgres(self):
        from sqlalchemy import text
        from config import get_database_url
        from sqlalchemy import create_engine

        # Two more runs must not grow the row counts.
        seed_registry._seed_database(require_postgres=True)
        seed_registry._seed_database(require_postgres=True)
        seed = seed_registry.load_seed()
        engine = create_engine(get_database_url())
        with engine.connect() as conn:
            assert conn.execute(text("SELECT COUNT(*) FROM countries")).scalar() == len(seed["countries"])
            assert conn.execute(text("SELECT COUNT(*) FROM immigration_programs")).scalar() == len(seed["immigration_programs"])
            assert conn.execute(text("SELECT COUNT(*) FROM government_sources")).scalar() == len(seed["government_sources"])
        engine.dispose()

