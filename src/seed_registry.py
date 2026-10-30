"""Load sources/source_registry.seed.json into countries / immigration_programs / government_sources.

Idempotent: re-running upserts rows by primary key rather than duplicating them.

Backend selection (single source of truth = src/config.py):
  * DATABASE_URL set (e.g. postgresql://...) -> seed PostgreSQL.
  * DATABASE_URL absent -> legacy SQLite mode (immigration.db), used by the
    standalone offline pipeline scripts. When run via the CLI entrypoint this
    mode is refused with a clear error, so a human never silently seeds SQLite.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import config
from db import ROOT, get_connection, init_schema

SEED_PATH = ROOT / "sources" / "source_registry.seed.json"


def load_seed(path: Path = SEED_PATH) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def upsert_countries(conn, countries: list[dict]) -> int:
    n = 0
    for c in countries:
        conn.execute(
            """
            INSERT INTO countries (country_id, name, iso_code)
            VALUES (:country_id, :name, :iso_code)
            ON CONFLICT(country_id) DO UPDATE SET
                name = excluded.name,
                iso_code = excluded.iso_code;
            """,
            c,
        )
        n += 1
    return n


def upsert_programs(conn, programs: list[dict]) -> int:
    n = 0
    for p in programs:
        conn.execute(
            """
            INSERT INTO immigration_programs
                (program_id, country_id, visa_category, subclass_code, name, status)
            VALUES
                (:program_id, :country_id, :visa_category, :subclass_code, :name, COALESCE(:status,'ACTIVE'))
            ON CONFLICT(program_id) DO UPDATE SET
                country_id = excluded.country_id,
                visa_category = excluded.visa_category,
                subclass_code = excluded.subclass_code,
                name = excluded.name,
                status = excluded.status;
            """,
            {**p, "status": p.get("status", "ACTIVE")},
        )
        n += 1
    return n


def upsert_sources(conn, sources: list[dict]) -> int:
    n = 0
    ts = _supports_timestamps(conn)
    for s in sources:
        row = {
            "source_id": s["source_id"],
            "country_id": s["country_id"],
            "program_id": s["program_id"],
            "visa_category": s["visa_category"],
            "authority": s["authority"],
            "url": s["url"],
            "source_type": s["source_type"],
            "source_subtype": s.get("source_subtype"),
            "priority": s["priority"],
            "monitoring_method": s.get("monitoring_method", "WEB"),
            "crawl_frequency": s.get("crawl_frequency", "DAILY"),
            "extract_requirements": s.get("extract_requirements", True),
            "content_kind": s.get("content_kind", "PAGE"),
            "notes": s.get("notes"),
            "created_at": None,
            "updated_at": None,
        }
        if ts:
            from datetime import datetime, timezone
            row["created_at"] = datetime.now(timezone.utc)
            row["updated_at"] = datetime.now(timezone.utc)
            sql = """
            INSERT INTO government_sources
                (source_id, country_id, program_id, visa_category, authority, url,
                 source_type, source_subtype, priority, monitoring_method, crawl_frequency,
                 extract_requirements, content_kind, status, notes, created_at, updated_at)
            VALUES
                (:source_id, :country_id, :program_id, :visa_category, :authority, :url,
                 :source_type, :source_subtype, :priority, :monitoring_method, :crawl_frequency,
                 :extract_requirements, :content_kind, 'ACTIVE', :notes, :created_at, :updated_at)
            ON CONFLICT(source_id) DO UPDATE SET
                program_id = excluded.program_id,
                visa_category = excluded.visa_category,
                authority = excluded.authority,
                url = excluded.url,
                source_type = excluded.source_type,
                source_subtype = excluded.source_subtype,
                priority = excluded.priority,
                monitoring_method = excluded.monitoring_method,
                crawl_frequency = excluded.crawl_frequency,
                extract_requirements = excluded.extract_requirements,
                content_kind = excluded.content_kind,
                notes = excluded.notes;
            """
        else:
            sql = """
            INSERT INTO government_sources
                (source_id, country_id, program_id, visa_category, authority, url,
                 source_type, source_subtype, priority, monitoring_method, crawl_frequency,
                 extract_requirements, content_kind, status, notes)
            VALUES
                (:source_id, :country_id, :program_id, :visa_category, :authority, :url,
                 :source_type, :source_subtype, :priority, :monitoring_method, :crawl_frequency,
                 :extract_requirements, :content_kind, 'ACTIVE', :notes)
            ON CONFLICT(source_id) DO UPDATE SET
                program_id = excluded.program_id,
                visa_category = excluded.visa_category,
                authority = excluded.authority,
                url = excluded.url,
                source_type = excluded.source_type,
                source_subtype = excluded.source_subtype,
                priority = excluded.priority,
                monitoring_method = excluded.monitoring_method,
                crawl_frequency = excluded.crawl_frequency,
                extract_requirements = excluded.extract_requirements,
                content_kind = excluded.content_kind,
                notes = excluded.notes;
            """
            row.pop("created_at")
            row.pop("updated_at")
        conn.execute(sql, row)
        n += 1
    return n


class _SQLAlchemyAdapter:
    """Make a SQLAlchemy Connection look like the legacy sqlite3 connection the
    upsert helpers expect, so the same named-parameter SQL runs on PostgreSQL."""

    supports_timestamps = True

    def __init__(self, conn):
        self._conn = conn

    def execute(self, sql, params=None):
        from sqlalchemy import text
        return self._conn.execute(text(sql), params or {})

    def commit(self):
        self._conn.commit()

    def close(self):
        self._conn.close()


def _supports_timestamps(conn) -> bool:
    return bool(getattr(conn, "supports_timestamps", False))


def _make_connection(require_postgres: bool = True):
    """Return a connection targeting the intended backend.

    Backend resolution (single source of truth = src/config.py):
      1. SEED_BACKEND=x explicitly overrides (x in {"postgres"[ql], "sqlite"}).
         Used by the legacy standalone pipeline scripts to force SQLite even when
         .env defines DATABASE_URL.
      2. Otherwise, DATABASE_URL set -> PostgreSQL via SQLAlchemy (deployment).
      3. Otherwise:
         - require_postgres=True  (CLI) -> raise, never silently seed SQLite.
         - require_postgres=False (library/legacy) -> explicit visible fallback to
           SQLite immigration.db.
    """
    override = (config.getenv("SEED_BACKEND") or "").strip().lower()
    if override:
        if override in ("postgres", "postgresql"):
            if not config.getenv("DATABASE_URL"):
                raise RuntimeError(
                    "SEED_BACKEND=postgres but DATABASE_URL is not set. "
                    "Copy .env.example to .env and configure it."
                )
            from db.connection import get_engine
            return _SQLAlchemyAdapter(get_engine().connect())
        if override == "sqlite":
            sys.stderr.write(
                "SEED_BACKEND=sqlite: forcing legacy SQLite seed mode "
                f"({ROOT / 'immigration.db'}) even though DATABASE_URL may be set.\n"
            )
            return init_schema()
        raise RuntimeError(f"Unknown SEED_BACKEND={override!r} (expected 'postgres' or 'sqlite').")

    if config.getenv("DATABASE_URL"):
        from db.connection import get_engine
        return _SQLAlchemyAdapter(get_engine().connect())
    if require_postgres:
        raise RuntimeError(
            "DATABASE_URL not set. Copy .env.example to .env and configure it "
            "(e.g. postgresql://user:pass@localhost:5432/immigration) so the "
            "seed targets the real deployment database instead of accidentally "
            "writing to a legacy SQLite file. Refusing to seed in PostgreSQL mode."
        )
    sys.stderr.write(
        "DATABASE_URL not configured; using legacy SQLite seed mode "
        f"({ROOT / 'immigration.db'}).\n"
    )
    return init_schema()


def main() -> None:
    """Seed the registry. Preserves the legacy SQLite fallback so the standalone
    offline pipeline scripts (which call this function directly) still work.

    Prefer the CLI (``python src/seed_registry.py``) for real deployments, which
    requires PostgreSQL and refuses to silently write to SQLite.
    """
    _seed_database(require_postgres=False)


def _seed_database(require_postgres: bool) -> dict:
    conn = _make_connection(require_postgres=require_postgres)
    data = load_seed()
    try:
        n_countries = upsert_countries(conn, data["countries"])
        n_programs = upsert_programs(conn, data["immigration_programs"])
        n_sources = upsert_sources(conn, data["government_sources"])
        conn.commit()
    except Exception:
        conn.close()
        raise

    override = (config.getenv("SEED_BACKEND") or "").strip().lower()
    actual_backend = (
        "sqlite" if override == "sqlite" or (not override and config.database_query() == "sqlite")
        else "postgresql"
    )
    if actual_backend == "postgresql":
        info = config.database_info()
        print("Seeding database: PostgreSQL")
        print(f"Database: {info['database']}")
        print(f"Host: {info['host']}")
    else:
        print(f"Seeding database: SQLite ({ROOT / 'immigration.db'})")
    print(f"Seeded: {n_countries} countries, {n_programs} programs, {n_sources} sources.")
    conn.close()
    return {"countries": n_countries, "programs": n_programs, "sources": n_sources}


if __name__ == "__main__":
    # CLI entrypoint: a human running the seed command expects the real database.
    # Never silently fall back to SQLite here.
    _seed_database(require_postgres=True)
