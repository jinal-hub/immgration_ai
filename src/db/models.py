"""SQLAlchemy ORM models for the immigration knowledge platform.

Tables cover:
  Phase 1: countries, immigration_programs, government_sources, government_documents,
           document_versions, document_chunks, fetch_log
  Phase 2: extraction_runs, requirements, requirement_evidence, requirement_versions,
           requirement_review, requirement_references
  Phase 3: rules, rule_versions, assessments, assessment_rule_results
  Phase 3C: applicant_profiles, visa_selections, gaps
  Production: api_users, user_sessions, audit_log, llm_usage, llm_budget_log
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    UUID,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import DeclarativeBase, relationship
from sqlalchemy.types import JSON

from db.vector import EMBEDDING_DIMENSIONS, Vector


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _short_uuid() -> str:
    return uuid.uuid4().hex[:12]


def _uuid_str() -> str:
    return str(uuid.uuid4())


# Native PostgreSQL UUID columns (as_uuid=False keeps string binding so existing
# string-based code, e.g. JWT user_id handling, keeps working unchanged). On SQLite
# (legacy tests) this renders as CHAR(36), also string-compatible. This must stay in
# sync with migrations/versions/49b9088f3a03 which created these columns as
# sa.UUID(as_uuid=False).
def UUIDColumn(**kwargs):
    return Column(UUID(as_uuid=False), default=_uuid_str, **kwargs)


class Base(DeclarativeBase):
    pass


# ---------------------------------------------------------------------------
# Helper mixin: every table gets created_at + updated_at
# ---------------------------------------------------------------------------
class TimestampMixin:
    created_at = Column(DateTime(timezone=True), default=_utcnow, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False)


# ===========================================================================
#  PHASE 1 — Government Source Registry + Document Metadata
# ===========================================================================

class Country(Base):
    __tablename__ = "countries"
    country_id = Column(String(8), primary_key=True)
    name = Column(String(128), nullable=False)
    iso_code = Column(String(8), nullable=False)


class ImmigrationProgram(Base):
    __tablename__ = "immigration_programs"
    program_id = Column(String(32), primary_key=True)
    country_id = Column(String(8), ForeignKey("countries.country_id"), nullable=False)
    visa_category = Column(String(32), nullable=False)
    subclass_code = Column(String(8))
    name = Column(String(256), nullable=False)
    status = Column(String(16), nullable=False, default="ACTIVE")

    __table_args__ = (
        # composite key used as the FK target from requirements/government_sources/
        # government_documents so a row's program_id+country_id pairing is always the
        # program's true country (a row can never claim AU with a CA program)
        UniqueConstraint("program_id", "country_id", name="uq_program_country"),
    )


class GovernmentSource(Base, TimestampMixin):
    __tablename__ = "government_sources"
    source_id = Column(String(32), primary_key=True)
    country_id = Column(String(8), ForeignKey("countries.country_id"), nullable=False)
    program_id = Column(String(32), ForeignKey("immigration_programs.program_id"), nullable=False)
    visa_category = Column(String(32), nullable=False)
    authority = Column(String(128), nullable=False)
    url = Column(Text, nullable=False, unique=True)
    source_type = Column(String(8), nullable=False)
    source_subtype = Column(String(64))
    priority = Column(String(16), nullable=False)
    monitoring_method = Column(String(16), nullable=False, default="WEB")
    crawl_frequency = Column(String(16), nullable=False, default="DAILY")
    last_checked_at = Column(DateTime(timezone=True))
    last_successful_fetch_at = Column(DateTime(timezone=True))
    last_hash = Column(String(64))
    status = Column(String(32), nullable=False, default="ACTIVE")
    notes = Column(Text)

    # Scope flag: a source can be fetched/hashed/diffed and used as evidence
    # (document_versions/document_chunks) without ever running requirement
    # extraction against it - for a reference-only source whose content is
    # out of product scope (e.g. the whole Migration Regulations covering
    # subclasses nothing here supports yet). Deleting no rows: this only
    # gates the extraction step in fetch_document_and_store, never the fetch/
    # store step itself. Defaults true (opt-out) so every existing/new source
    # keeps today's behavior unless explicitly scoped out.
    extract_requirements = Column(Boolean, nullable=False, default=True)

    # What kind of page this URL serves, which decides how it is chunked.
    # PAGE (default): prose with headings - the ordinary block walk.
    # NEWS_LIST: a dated "latest updates" listing - one chunk per
    # announcement, each with its own date and link, so a change to one item
    # is not indistinguishable from a change to the whole page. Kept separate
    # from source_type (HTML/PDF, which decides how the bytes are fetched)
    # because it is about structure, not transport.
    content_kind = Column(String(16), nullable=False, default="PAGE")

    # Conditional GET (item 5): the validators the origin sent with the last
    # successful fetch, so the next request can ask "has this changed since
    # then?" instead of always re-downloading the full body.
    etag = Column(String(256), nullable=True)
    last_modified = Column(String(64), nullable=True)

    # Fallback strategy fields
    fallback_source_id = Column(String(32), ForeignKey("government_sources.source_id"), nullable=True)
    fetch_status = Column(String(32), nullable=True)
    http_status = Column(Integer, nullable=True)
    last_error = Column(Text, nullable=True)
    last_error_type = Column(String(64), nullable=True)  # dns_resolution_failure / connect_timeout / ...
    consecutive_failures = Column(Integer, nullable=False, default=0)

    __table_args__ = (
        Index("idx_sources_program", "program_id"),
        ForeignKeyConstraint(
            ["program_id", "country_id"],
            ["immigration_programs.program_id", "immigration_programs.country_id"],
            name="fk_src_program_country",
        ),
        CheckConstraint(
            "status IN ('ACTIVE','DISABLED','ACCESS_BLOCKED','NETWORK_RESTRICTED',"
            "'REQUIRES_BROWSER','REQUIRES_MANUAL_DOWNLOAD','BROKEN')",
            name="ck_source_status",
        ),
        CheckConstraint(
            # The FetchStatus values, plus OK_EMPTY_CONTENT: retrieval and
            # parsing both worked but there was nothing to extract (a scanned
            # PDF, or a page that renders no structural content). That is
            # neither a failure nor an ordinary success, and calling it "OK"
            # hid sources that were quietly contributing no knowledge.
            "fetch_status IS NULL OR fetch_status IN ('OK','OK_EMPTY_CONTENT',"
            "'FETCH_FAILED','ACCESS_BLOCKED','NETWORK_RESTRICTED',"
            "'REQUIRES_BROWSER','REQUIRES_MANUAL_DOWNLOAD','INVALID_CONTENT')",
            name="ck_source_fetch_status",
        ),
    )


# ===========================================================================
#  HOST COOLDOWN — one pause per hostname after it refused us (HTTP 403/429).
# ===========================================================================

class HostCooldown(Base, TimestampMixin):
    """A temporary pause on fetching from one hostname after it refused us.

    Keyed by host rather than by source: if a host refuses one of our URLs it
    will refuse the others too, so every source on that host is skipped until
    ``cooldown_until`` passes. This table is the *only* reaction to a refusal —
    it never changes the IP address, user agent or fetch method we use, and
    there is deliberately no override flag to skip it.
    """
    __tablename__ = "host_cooldowns"

    host = Column(String(255), primary_key=True)
    cooldown_until = Column(DateTime(timezone=True), nullable=False)
    http_status = Column(Integer, nullable=True)
    # How many times this host has refused us inside the current pause window.
    # Refreshing a cooldown leaves an existing count intact; a new refusal
    # window starts it again at 1.
    hits = Column(Integer, nullable=False, default=1)
    # The source whose fetch provoked the pause (diagnostic only).
    source_id = Column(
        String(32),
        ForeignKey("government_sources.source_id", ondelete="CASCADE"),
        nullable=True,
    )

    __table_args__ = (
        Index("idx_host_cooldown_until", "cooldown_until"),
    )


# ===========================================================================
#  GOVERNMENT SOURCE AUTO-DISCOVERY — Candidates proposed upstream of
#  government_sources (never inserted into that table without admin approval).
# ===========================================================================

class DiscoveredSource(Base, TimestampMixin):
    """A candidate URL surfaced by source auto-discovery, awaiting admin review.

    Onboarding/suggestion only: discovery fabricates *candidates*; only an admin
    approve action (via DiscoveredSourceRepo.approve) promotes a candidate into a
    real government_sources row. Nothing in discovery ever triggers extraction,
    rule compilation, or activation on its own.
    """
    __tablename__ = "discovered_sources"

    discovered_source_id = Column(String(32), primary_key=True)
    seed_url = Column(Text, nullable=False)
    candidate_url = Column(Text, nullable=False, unique=True)
    page_title = Column(String(512))
    matched_keywords = Column(JSON, default=list, nullable=False)
    confidence_score = Column(Integer, nullable=False, default=0)
    suggested_visa_category = Column(String(32))
    suggested_country_id = Column(String(8), ForeignKey("countries.country_id"), nullable=False, default="AU")
    status = Column(String(24), nullable=False, default="PENDING_REVIEW")
    discovered_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    discovery_run_id = Column(String(36))
    reviewed_by = Column(String(36))
    reviewed_at = Column(DateTime(timezone=True))
    rejection_reason = Column(Text)

    __table_args__ = (
        Index("idx_discovered_status", "status"),
        Index("idx_discovered_category", "suggested_visa_category"),
        Index("idx_discovered_run", "discovery_run_id"),
        CheckConstraint(
            "status IN ('PENDING_REVIEW','APPROVED','REJECTED')",
            name="ck_discovered_status",
        ),
    )


class GovernmentDocument(Base, TimestampMixin):
    __tablename__ = "government_documents"
    document_id = Column(String(32), primary_key=True)
    source_id = Column(String(32), ForeignKey("government_sources.source_id"), nullable=False)
    country_id = Column(String(8), ForeignKey("countries.country_id"), nullable=False)
    program_id = Column(String(32), ForeignKey("immigration_programs.program_id"), nullable=False)
    visa_category = Column(String(32), nullable=False)
    document_type = Column(String(64), nullable=False)
    url = Column(Text, nullable=False)
    title = Column(String(512))
    published_date = Column(String(32))
    effective_date = Column(String(32))
    retrieved_at = Column(DateTime(timezone=True), nullable=False)
    content_hash = Column(String(64), nullable=False)
    current_version = Column(Integer, nullable=False, default=1)
    language = Column(String(8), nullable=False, default="en")
    raw_path = Column(Text, nullable=False)
    extracted_path = Column(Text)
    status = Column(String(32), nullable=False, default="CURRENT")

    __table_args__ = (
        Index("idx_documents_source", "source_id"),
        ForeignKeyConstraint(
            ["program_id", "country_id"],
            ["immigration_programs.program_id", "immigration_programs.country_id"],
            name="fk_doc_program_country",
        ),
    )


class DocumentVersion(Base, TimestampMixin):
    __tablename__ = "document_versions"
    version_id = Column(Integer, primary_key=True, autoincrement=True)
    document_id = Column(String(32), ForeignKey("government_documents.document_id"), nullable=False)
    version_number = Column(Integer, nullable=False)
    content_hash = Column(String(64), nullable=False)
    retrieved_at = Column(DateTime(timezone=True), nullable=False)
    raw_path = Column(Text, nullable=False)
    extracted_path = Column(Text)
    change_type = Column(String(16), nullable=False)
    diff_summary = Column(Text)

    __table_args__ = (
        UniqueConstraint("document_id", "version_number", name="uq_doc_version"),
        # idempotency: a fetch that produces identical raw bytes never re-opens a version
        UniqueConstraint("document_id", "content_hash", name="uq_doc_version_hash"),
        # alternate key referenced by the document_chunks/extraction FKs
        UniqueConstraint("document_id", "version_id", name="uq_doc_version_identity"),
        CheckConstraint("version_number > 0", name="ck_doc_version_positive"),
        CheckConstraint(
            "change_type IN ('INITIAL','CHANGED','UNCHANGED','MANUAL_CAPTURE')",
            name="ck_doc_change_type",
        ),
    )


class DocumentChunk(Base, TimestampMixin):
    __tablename__ = "document_chunks"
    chunk_id = Column(Integer, primary_key=True, autoincrement=True)
    document_id = Column(String(32), ForeignKey("government_documents.document_id"), nullable=False)
    version_id = Column(Integer, ForeignKey("document_versions.version_id"), nullable=False)
    chunk_type = Column(String(16), nullable=False)
    heading_path = Column(String(512))
    order_index = Column(Integer, nullable=False)
    raw_text = Column(Text, nullable=False)
    page_number = Column(Integer)
    # The chunk's embedding, when embeddings are enabled. NULL means "not
    # embedded yet" - never "embedded as the zero vector", which would be
    # indistinguishable from a real match at the origin.
    embedding = Column(Vector(EMBEDDING_DIMENSIONS), nullable=True)

    __table_args__ = (
        Index("idx_chunks_document", "document_id", "version_id"),
        # a chunk is anchored to one exact immutable document version
        ForeignKeyConstraint(
            ["document_id", "version_id"],
            ["document_versions.document_id", "document_versions.version_id"],
            name="fk_chunks_doc_version_pair",
        ),
        UniqueConstraint("version_id", "order_index", name="uq_chunk_version_order"),
        # alternate key referenced by requirement_evidence lineage FK
        UniqueConstraint("chunk_id", "document_id", "version_id", name="uq_chunk_lineage"),
        CheckConstraint("order_index >= 0", name="ck_chunk_order_nonneg"),
    )


# ===========================================================================
#  SYNC JOB TRACKING - one durable row per "sync all sources" execution and
#  one durable row per source inside it. PipelineRun remains the per-source
#  audit trail; these rows make batch progress survive a process restart and
#  make a stalled/interrupted run resumable instead of silently starting over.
# ===========================================================================

class SyncJob(Base, TimestampMixin):
    """Durable progress record for one all-sources sync execution.

    Previously the only batch-level handle was an in-memory dict in
    ``scheduler._sync_jobs``. A process restart (or the host killing a run that
    takes hours because of per-host politeness spacing) lost the handle and all
    knowledge of which sources had already been processed, so the next run
    began again at source #1 and the observed "8 of 96 checked" state never
    advanced. This row is created before the first fetch and committed with
    per-source progress, so a restart leaves an honest INTERRUPTED record that
    can be resumed.
    """
    __tablename__ = "sync_jobs"
    job_id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    trigger_type = Column(String(16), nullable=False)
    status = Column(String(16), nullable=False, default="PENDING")
    total_sources = Column(Integer, nullable=False, default=0)
    processed_sources = Column(Integer, nullable=False, default=0)
    changed_sources = Column(Integer, nullable=False, default=0)
    unchanged_sources = Column(Integer, nullable=False, default=0)
    error_sources = Column(Integer, nullable=False, default=0)
    skipped_sources = Column(Integer, nullable=False, default=0)
    started_at = Column(DateTime(timezone=True))
    completed_at = Column(DateTime(timezone=True))
    heartbeat_at = Column(DateTime(timezone=True))
    last_error = Column(Text)
    metadata_json = Column(JSON, default=dict)

    sources = relationship(
        "SyncJobSource", back_populates="job", cascade="all, delete-orphan",
    )

    __table_args__ = (
        Index("idx_sync_job_status", "status"),
        Index("idx_sync_job_started", "started_at"),
        CheckConstraint(
            "status IN ('PENDING','RUNNING','COMPLETED','PARTIAL','FAILED','INTERRUPTED')",
            name="ck_sync_job_status",
        ),
    )


class SyncJobSource(Base, TimestampMixin):
    """Durable per-source outcome inside a SyncJob.

    Every source selected for the batch gets a row before the run starts -
    including sources later skipped because their host is in a refusal cooldown
    or because a scheduled run finds them not due. Those skips used to persist
    nothing at all, which is why 88 sources looked "never attempted" even
    though the run had reached them. ``reason`` makes the distinction explicit.
    """
    __tablename__ = "sync_job_sources"
    job_id = Column(
        String(36), ForeignKey("sync_jobs.job_id", ondelete="CASCADE"),
        primary_key=True,
    )
    source_id = Column(String(32), primary_key=True)
    status = Column(String(16), nullable=False, default="PENDING")
    reason = Column(String(64))
    fetch_status = Column(String(32))
    http_status = Column(Integer)
    error = Column(Text)
    # Plain string (not an FK): a PipelineRun can be rolled back/reinserted by
    # the failure handler, and the job row must remain writable regardless.
    pipeline_run_id = Column(String(36))
    attempt = Column(Integer, nullable=False, default=0)
    started_at = Column(DateTime(timezone=True))
    completed_at = Column(DateTime(timezone=True))

    job = relationship("SyncJob", back_populates="sources")

    __table_args__ = (
        Index("idx_sync_job_source_status", "job_id", "status"),
        Index("idx_sync_job_source_source", "source_id"),
        CheckConstraint(
            "status IN ('PENDING','RUNNING','CHANGED','UNCHANGED','ERROR','SKIPPED','INTERRUPTED')",
            name="ck_sync_job_source_status",
        ),
    )


class FetchLog(Base):
    __tablename__ = "fetch_log"
    log_id = Column(Integer, primary_key=True, autoincrement=True)
    source_id = Column(String(32), ForeignKey("government_sources.source_id"), nullable=False)
    fetched_at = Column(DateTime(timezone=True), nullable=False)
    http_status = Column(Integer)
    status = Column(String(32), nullable=False, default="OK")
    content_hash = Column(String(64))
    changed = Column(SmallInteger, nullable=False, default=0)
    duration_ms = Column(Integer)
    error = Column(Text)
    error_type = Column(String(64))  # machine-readable failure class from fetcher
    # Retrieval event detail: one row is one acquisition attempt, and these
    # fields make the attempt reproducible/auditable without the sidecar
    # manifest (which lives next to the immutable bytes).
    retrieval_id = Column(String(36), unique=True, nullable=True)
    acquisition = Column(String(32))  # playwright_page | playwright_request | legacy_*
    final_url = Column(String(2048))
    content_type = Column(String(128))
    limiter_wait_ms = Column(Integer)
    browser_launch_ms = Column(Integer)
    goto_ms = Column(Integer)
    selector_wait_ms = Column(Integer)
    selector_timed_out = Column(Boolean)

    __table_args__ = (
        Index("idx_fetchlog_source", "source_id", "fetched_at"),
        CheckConstraint("changed IN (0,1)", name="ck_fetch_changed"),
    )


# ===========================================================================
#  PHASE 2 — Requirement Knowledge Model
# ===========================================================================

class ExtractionRun(Base, TimestampMixin):
    __tablename__ = "extraction_runs"
    run_id = Column(String(24), primary_key=True)
    document_id = Column(String(32), ForeignKey("government_documents.document_id"), nullable=False)
    document_version = Column(Integer, nullable=False)
    # canonical audit lineage: the exact immutable source snapshot this run consumed
    document_version_id = Column(Integer, ForeignKey("document_versions.version_id"))
    model = Column(String(64), nullable=False)
    prompt_version = Column(String(64), nullable=False)
    chunks_seen = Column(Integer, nullable=False, default=0)
    candidate_chunks = Column(Integer, nullable=False, default=0)
    llm_calls = Column(Integer, nullable=False, default=0)
    # Item 6: requirements_extracted counts ATTEMPTS (every accepted/needs-
    # review candidate persist_requirement() was called for), not stored
    # rows - a dedup/conflict/evidence-merge can turn several attempts into
    # zero new rows. stored_new/merged_into_existing split that apart; use
    # stored_new, never requirements_extracted, for "how much new knowledge
    # did this run add".
    requirements_extracted = Column(Integer, nullable=False, default=0)
    stored_new = Column(Integer, nullable=False, default=0)
    merged_into_existing = Column(Integer, nullable=False, default=0)
    validated = Column(Integer, nullable=False, default=0)
    needs_review = Column(Integer, nullable=False, default=0)
    rejected = Column(Integer, nullable=False, default=0)
    evidence_failures = Column(Integer, nullable=False, default=0)
    schema_failures = Column(Integer, nullable=False, default=0)
    # Chunks whose LLM call hit a persistent provider quota/rate-limit
    # exhaustion and were skipped rather than extracted - a later sync
    # should retry them, not treat them as "the model found nothing".
    deferred_chunks = Column(Integer, nullable=False, default=0)
    status = Column(String(16), nullable=False, default="RUNNING")
    dry_run = Column(SmallInteger, nullable=False, default=0)
    error = Column(Text)
    completed_at = Column(DateTime(timezone=True))

    __table_args__ = (
        Index("idx_extraction_runs_document", "document_id", "document_version"),
        # (document_id, document_version) must resolve to one real document_versions row
        ForeignKeyConstraint(
            ["document_id", "document_version"],
            ["document_versions.document_id", "document_versions.version_number"],
            name="fk_extract_run_doc_version_pair",
        ),
        CheckConstraint("document_version > 0", name="ck_extract_version_positive"),
        CheckConstraint("dry_run IN (0,1)", name="ck_extract_dry_run"),
        CheckConstraint(
            # PARTIAL: completed, but at least one chunk was deferred due to
            # provider quota exhaustion and needs a retry on a later sync.
            "status IN ('RUNNING','COMPLETED','FAILED','DRY_RUN','PARTIAL')",
            name="ck_extract_status",
        ),
    )


class ExtractionCache(Base, TimestampMixin):
    """Persistent chunk-level LLM extraction cache (item 5).

    Keyed by (chunk content hash, prompt_version, model): a chunk whose exact
    text was already sent through this exact prompt/model combination never
    reaches the LLM again, even across document versions and sync runs -
    every new DocumentVersion gets fresh DocumentChunk rows even for
    byte-identical text (see fetch_document_and_store), so the chunk_id alone
    can never be the cache key. A new prompt_version is a different primary
    key entirely, so it naturally never reuses a stale cache entry.
    """
    __tablename__ = "extraction_cache"
    chunk_content_hash = Column(String(64), primary_key=True)
    prompt_version = Column(String(64), primary_key=True)
    model = Column(String(64), primary_key=True)
    # The chunk_id that produced this cached response - evidence[].chunk_id
    # references in raw_output are rewritten to the requesting call's own
    # chunk_id on every cache hit (see llm_client.CachingLLMClient).
    source_chunk_id = Column(Integer, nullable=False)
    raw_output = Column(Text, nullable=False)


class Requirement(Base, TimestampMixin):
    __tablename__ = "requirements"
    # 96, not 48: "REQ-{program_id}-{sem_key}-{uuid6}" (the CONFLICT-path format)
    # can exceed 48 once program_id is long (e.g. "LEGISLATION_MIGRATION_REGS") —
    # confirmed in production as a PostgreSQL StringDataRightTruncation that
    # silently dropped a correctly-extracted requirement. See migration
    # a3c5e7f9b1d3 and the MAX_REQUIREMENT_ID_LENGTH guard in
    # extraction/requirement_repository.py.
    requirement_id = Column(String(96), primary_key=True)
    semantic_key = Column(String(256), nullable=False)
    program_id = Column(String(32), ForeignKey("immigration_programs.program_id"), nullable=False)
    country_id = Column(String(8), ForeignKey("countries.country_id"), nullable=False)
    # canonical audit lineage: the source snapshot this requirement was extracted from
    document_version_id = Column(Integer, ForeignKey("document_versions.version_id"))
    category = Column(String(64), nullable=False)
    name = Column(String(256), nullable=False)
    description = Column(Text)
    field = Column(String(128))
    # Closed field vocabulary (v3): free-text label for the real legal/policy
    # concept when field="NOT_MODELED" - preserves the extractor's intent
    # instead of dropping it. Null when field is a real evaluable name.
    concept = Column(String(256))
    operator = Column(String(16), nullable=False, default="UNKNOWN")
    value_json = Column(Text)
    unit = Column(String(32))
    mandatory = Column(SmallInteger)
    condition_json = Column(Text)
    exception_json = Column(Text)
    applies_to = Column(Text)
    content_kind = Column(String(32), nullable=False, default="REQUIREMENT")
    confidence = Column(Float)
    criticality = Column(String(32), nullable=False, default="NORMAL")
    status = Column(String(32), nullable=False, default="DRAFT")
    current_version = Column(Integer, nullable=False, default=1)
    extraction_run_id = Column(String(24), ForeignKey("extraction_runs.run_id"), nullable=False)

    __table_args__ = (
        Index("idx_requirements_program", "program_id"),
        Index("idx_requirements_semantic_key", "semantic_key"),
        Index("idx_requirements_status", "status"),
        Index("idx_requirements_category", "category"),
        # one live (non-conflicted, non-rejected) row per semantic key + program
        Index(
            "uq_req_active_semantic", "program_id", "semantic_key", unique=True,
            sqlite_where=text("status NOT IN ('CONFLICT','REJECTED')"),
            postgresql_where=text("status NOT IN ('CONFLICT','REJECTED')"),
        ),
        # program/country pairing must match the program's true country
        ForeignKeyConstraint(
            ["program_id", "country_id"],
            ["immigration_programs.program_id", "immigration_programs.country_id"],
            name="fk_req_program_country",
        ),
        # alternate key referenced by the rules (requirement_id, program_id) FK
        UniqueConstraint("requirement_id", "program_id", name="uq_req_program_pair"),
        CheckConstraint(
            "status IN ('DRAFT','VALIDATED','NEEDS_REVIEW','APPROVED','REJECTED',"
            "'SUPERSEDED','CONFLICT')",
            name="ck_req_status",
        ),
        CheckConstraint("current_version > 0", name="ck_req_current_version"),
        CheckConstraint("mandatory IN (0,1)", name="ck_req_mandatory"),
    )


class RequirementEvidence(Base, TimestampMixin):
    __tablename__ = "requirement_evidence"
    evidence_id = Column(Integer, primary_key=True, autoincrement=True)
    requirement_id = Column(String(96), ForeignKey("requirements.requirement_id"), nullable=False)
    document_id = Column(String(32), ForeignKey("government_documents.document_id"), nullable=False)
    document_version = Column(Integer, nullable=False)
    # canonical audit lineage: chunk -> owner document version (proves the evidence
    # really came from the chunk/version it claims)
    document_version_id = Column(Integer, ForeignKey("document_versions.version_id"))
    chunk_id = Column(Integer, ForeignKey("document_chunks.chunk_id"), nullable=False)
    evidence_text = Column(Text, nullable=False)
    evidence_type = Column(String(32), nullable=False)
    verified = Column(SmallInteger, nullable=False, default=0)
    verification_note = Column(Text)
    extraction_method = Column(String(32), nullable=False, default="LLM_EXTRACTION")
    extracted_at = Column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        Index("idx_evidence_requirement", "requirement_id"),
        Index("idx_evidence_chunk", "chunk_id"),
        # the evidence's chunk/document/document_version triple must be self-consistent
        ForeignKeyConstraint(
            ["chunk_id", "document_id", "document_version_id"],
            ["document_chunks.chunk_id", "document_chunks.document_id", "document_chunks.version_id"],
            name="fk_evidence_chunk_lineage",
        ),
        # (document_id, document_version) must resolve to one real document_versions row
        ForeignKeyConstraint(
            ["document_id", "document_version"],
            ["document_versions.document_id", "document_versions.version_number"],
            name="fk_evidence_doc_version_pair",
        ),
        CheckConstraint("document_version > 0", name="ck_evidence_version_positive"),
        CheckConstraint("verified IN (0,1)", name="ck_evidence_verified"),
    )


class RequirementVersion(Base, TimestampMixin):
    __tablename__ = "requirement_versions"
    version_id = Column(Integer, primary_key=True, autoincrement=True)
    requirement_id = Column(String(96), ForeignKey("requirements.requirement_id"), nullable=False)
    version_number = Column(Integer, nullable=False)
    snapshot_json = Column(Text, nullable=False)
    document_version = Column(Integer, nullable=False)
    document_version_id = Column(Integer, ForeignKey("document_versions.version_id"))
    change_type = Column(String(16), nullable=False)
    created_by = Column(String(64), nullable=False, default="system")

    __table_args__ = (
        UniqueConstraint("requirement_id", "version_number", name="uq_req_version"),
        Index("idx_req_versions_requirement", "requirement_id"),
        CheckConstraint("version_number > 0", name="ck_reqver_version_number"),
        # 0 is legal only for legacy human edits with no source attachment
        CheckConstraint("document_version >= 0", name="ck_reqver_docver"),
    )


class RequirementReview(Base, TimestampMixin):
    __tablename__ = "requirement_review"
    review_id = Column(Integer, primary_key=True, autoincrement=True)
    requirement_id = Column(String(96), ForeignKey("requirements.requirement_id"), nullable=False)
    reviewer = Column(String(64), nullable=False)
    action = Column(String(32), nullable=False)
    old_state_json = Column(Text)
    new_state_json = Column(Text)
    notes = Column(Text)

    __table_args__ = (Index("idx_review_requirement", "requirement_id"),)


class RequirementReference(Base, TimestampMixin):
    __tablename__ = "requirement_references"
    reference_id = Column(Integer, primary_key=True, autoincrement=True)
    requirement_id = Column(String(96), ForeignKey("requirements.requirement_id"), nullable=False)
    reference_type = Column(String(32), nullable=False, default="LEGAL_CROSS_REFERENCE")
    target_text = Column(Text, nullable=False)
    resolved_chunk_id = Column(Integer, ForeignKey("document_chunks.chunk_id"))
    status = Column(String(32), nullable=False, default="UNRESOLVED")

    __table_args__ = (Index("idx_references_requirement", "requirement_id"),)


# ===========================================================================
#  PHASE 3 — Deterministic Rule Engine
# ===========================================================================

class Rule(Base, TimestampMixin):
    __tablename__ = "rules"
    # 96, matching requirement_id: requirement_to_rule.py builds
    # rule_id = f"RULE-{requirement_id}" directly from it, so rule_id must be at
    # least as wide as "RULE-" + the widened requirement_id or the same
    # truncation bug just resurfaces one stage later, at rule compilation.
    rule_id = Column(String(96), primary_key=True)
    requirement_id = Column(String(96), ForeignKey("requirements.requirement_id"), nullable=False)
    program_id = Column(String(32), ForeignKey("immigration_programs.program_id"), nullable=False)
    # canonical audit lineage: the exact immutable requirement version this rule was
    # compiled from (rules.requirement_version_id -> requirement_versions.version_id
    # -> document_versions.version_id)
    requirement_version_id = Column(Integer, ForeignKey("requirement_versions.version_id"))
    category = Column(String(64), nullable=False)
    field = Column(String(128), nullable=False)
    operator = Column(String(16), nullable=False)
    value_json = Column(Text)
    unit = Column(String(32))
    mandatory = Column(Boolean, nullable=False, default=False)
    critical = Column(Boolean, nullable=False, default=False)
    message = Column(Text, nullable=False)
    status = Column(String(16), nullable=False, default="DRAFT")
    current_version = Column(Integer, nullable=False, default=1)
    expression_json = Column(JSON)  # full expression tree (AND/OR/NOT/IF-THEN/UNLESS/REQUIRES)
    condition_json = Column(JSON)
    exception_json = Column(JSON)

    # provenance (inline — single-row, no FK needed for runtime)
    source_id = Column(String(32))
    document_id = Column(String(32))
    document_version = Column(Integer)
    chunk_id = Column(Integer)
    evidence_text = Column(Text)

    __table_args__ = (
        Index("idx_rules_program", "program_id"),
        Index("idx_rules_status", "status"),
        Index("idx_rules_requirement", "requirement_id"),
        # a rule can only ever belong to the program of its parent requirement
        ForeignKeyConstraint(
            ["requirement_id", "program_id"],
            ["requirements.requirement_id", "requirements.program_id"],
            name="fk_rule_req_program_pair",
        ),
        CheckConstraint(
            "status IN ('DRAFT','VALIDATED','APPROVED','ACTIVE',"
            "'SUPERSEDED','RETIRED','REJECTED')",
            name="ck_rule_status",
        ),
        CheckConstraint("current_version > 0", name="ck_rule_current_version"),
        # At most one ACTIVE rule per (program_id, category, field) — matches
        # EnhancedRuleRepo.activate_rule's own logical key
        # (_find_active_for_logical_key(program_id, category, field)). The
        # previous version of this index covered only (program_id, field),
        # which is narrower than the invariant it's meant to enforce: it
        # rejected a legitimate second ACTIVE rule that shares a field but has
        # a different category, something activate_rule's own supersede logic
        # explicitly treats as a different, non-conflicting logical rule (see
        # migration 7c9e2a4f1b6d). Found by driving a real rule through
        # approve->compile->activate and then testing the DB constraint
        # directly with raw INSERTs.
        Index(
            "uq_rules_active_per_field", "program_id", "category", "field", unique=True,
            postgresql_where=text("status = 'ACTIVE'"),
            sqlite_where=text("status = 'ACTIVE'"),
        ),
    )


class RuleVersion(Base, TimestampMixin):
    __tablename__ = "rule_versions"
    version_id = Column(Integer, primary_key=True, autoincrement=True)
    rule_id = Column(String(96), ForeignKey("rules.rule_id"), nullable=False)
    version_number = Column(Integer, nullable=False)
    snapshot_json = Column(JSON, nullable=False)
    change_type = Column(String(16), nullable=False)
    created_by = Column(String(64), nullable=False, default="system")
    # canonical audit lineage: which exact requirement version produced this rule snapshot
    requirement_version_id = Column(Integer, ForeignKey("requirement_versions.version_id"))

    __table_args__ = (
        UniqueConstraint("rule_id", "version_number", name="uq_rule_version"),
        Index("idx_rule_versions_rule", "rule_id"),
        CheckConstraint("version_number > 0", name="ck_rulever_version_number"),
    )


class Ruleset(Base, TimestampMixin):
    """Immutable released ruleset: the deterministic set of rule_versions an assessment
    evaluated against. Rows are append-only (PG trigger blocks UPDATE/DELETE)."""
    __tablename__ = "rulesets"
    ruleset_id = Column(String(36), primary_key=True)
    program_id = Column(String(32), ForeignKey("immigration_programs.program_id"), nullable=False)
    content_hash = Column(String(64), nullable=False)
    status = Column(String(16), nullable=False, default="RELEASED")  # DRAFT/RELEASED
    released_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    released_by = Column(String(64), nullable=False, default="system")
    rule_count = Column(Integer, nullable=False, default=0)

    __table_args__ = (
        # idempotency: releasing the exact same rule set twice reuses one ruleset row
        UniqueConstraint("program_id", "content_hash", name="uq_ruleset_content"),
        CheckConstraint("status IN ('DRAFT','RELEASED')", name="ck_ruleset_status"),
    )


class RulesetRule(Base):
    """Member of an immutable ruleset: the exact rule_version, in a stable order.

    No TimestampMixin — the real ruleset_rules table has no created_at/updated_at columns.
    """
    __tablename__ = "ruleset_rules"
    ruleset_id = Column(String(36), ForeignKey("rulesets.ruleset_id"), nullable=False, primary_key=True)
    rule_version_id = Column(Integer, ForeignKey("rule_versions.version_id"), nullable=False, primary_key=True)
    position = Column(Integer, nullable=False)

    __table_args__ = (
        UniqueConstraint("ruleset_id", "position", name="uq_ruleset_rule_position"),
        CheckConstraint("position >= 0", name="ck_ruleset_rule_position"),
        Index("idx_ruleset_rule_version", "rule_version_id"),
    )


# ===========================================================================
#  PHASE 3C — Applicant Profiles + Assessments
# ===========================================================================

class ApplicantProfile(Base, TimestampMixin):
    __tablename__ = "applicant_profiles"
    applicant_id = UUIDColumn(primary_key=True)
    user_id = Column(UUID(as_uuid=False), ForeignKey("api_users.user_id"), nullable=False)
    first_name = Column(String(128))
    last_name = Column(String(128))
    date_of_birth = Column(DateTime(timezone=True))
    passport_nationality = Column(String(8))
    current_visa_subclass = Column(String(8))
    profile_json = Column(JSON, nullable=False, default=dict)
    # applicant-level fields cached at assessment time
    age = Column(Integer)
    english_level = Column(String(32))
    points = Column(Integer)
    occupation = Column(String(32))
    skills_assessment_status = Column(String(32))
    health_status = Column(String(32))
    character_status = Column(String(32))
    has_regional_nomination = Column(Boolean)
    visa_subclass = Column(String(8))
    # First batch of newly modelled applicant concepts (Phase 4). Each one is
    # a fact an applicant can supply, so a rule that references it can resolve
    # to PASS/FAIL instead of always UNKNOWN.
    visa_history = Column(String(32))
    employer_sponsored = Column(Boolean)
    work_experience_years = Column(Integer)
    in_australia_at_application = Column(Boolean)
    has_health_insurance = Column(Boolean)
    genuine_temporary_entrant = Column(Boolean)

    __table_args__ = (Index("idx_applicant_user", "user_id"),)


class VisaSelection(Base, TimestampMixin):
    __tablename__ = "visa_selections"
    selection_id = UUIDColumn(primary_key=True)
    applicant_id = Column(UUID(as_uuid=False), ForeignKey("applicant_profiles.applicant_id"), nullable=False)
    program_id = Column(String(32), ForeignKey("immigration_programs.program_id"), nullable=False)
    selected_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)

    __table_args__ = (
        UniqueConstraint("applicant_id", "program_id", name="uq_applicant_program"),
        Index("idx_visa_selection_applicant", "applicant_id"),
    )


class Assessment(Base, TimestampMixin):
    __tablename__ = "assessments"
    assessment_id = UUIDColumn(primary_key=True)
    applicant_id = Column(UUID(as_uuid=False), ForeignKey("applicant_profiles.applicant_id"), nullable=False)
    program_id = Column(String(32), ForeignKey("immigration_programs.program_id"), nullable=False)
    business_status = Column(String(16), nullable=False)  # ELIGIBLE/INELIGIBLE/INCOMPLETE
    overall_status = Column(String(16), nullable=False)   # raw engine status
    total_rules = Column(Integer, nullable=False)
    passed_count = Column(Integer, nullable=False)
    failed_count = Column(Integer, nullable=False)
    unknown_count = Column(Integer, nullable=False)
    not_applicable_count = Column(Integer, nullable=False)
    evaluated_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    request_id = Column(String(36))  # correlation ID for request tracing
    applicant_snapshot = Column(JSON)  # frozen applicant profile at assessment time
    ruleset_hash = Column(String(64))  # hash of all ACTIVE rule versions used in this assessment
    # immutable released ruleset this assessment evaluated against (nullable for
    # assessments created before ruleset tracking / when a rule has no version rows)
    ruleset_id = Column(String(36), ForeignKey("rulesets.ruleset_id"))

    __table_args__ = (
        Index("idx_assessment_applicant", "applicant_id"),
        Index("idx_assessment_program", "program_id"),
        Index("idx_assessment_date", "evaluated_at"),
        CheckConstraint(
            "business_status IN ('ELIGIBLE','INELIGIBLE','INCOMPLETE')",
            name="ck_assessment_business",
        ),
        CheckConstraint(
            "overall_status IN ('PASS','FAIL','UNKNOWN','NOT_APPLICABLE','ERROR')",
            name="ck_assessment_overall",
        ),
    )


class AssessmentRuleResult(Base, TimestampMixin):
    __tablename__ = "assessment_rule_results"
    result_id = UUIDColumn(primary_key=True)
    assessment_id = Column(UUID(as_uuid=False), ForeignKey("assessments.assessment_id"), nullable=False)
    rule_id = Column(String(96), ForeignKey("rules.rule_id"), nullable=False)
    rule_version = Column(Integer, nullable=False)
    # canonical audit lineage: the exact immutable rule_versions row this result came from
    rule_version_id = Column(Integer, ForeignKey("rule_versions.version_id"))
    status = Column(String(16), nullable=False)  # PASS/FAIL/UNKNOWN/NOT_APPLICABLE/ERROR
    field = Column(String(128))
    actual_value = Column(Text)
    expected_value = Column(Text)
    operator = Column(String(16))
    message = Column(Text)
    reason = Column(Text)

    __table_args__ = (
        Index("idx_assessment_result_assessment", "assessment_id"),
        Index("idx_assessment_result_rule", "rule_id"),
        # one evaluation per rule per assessment snapshot
        UniqueConstraint("assessment_id", "rule_id", name="uq_result_assessment_rule"),
        CheckConstraint(
            "status IN ('PASS','FAIL','UNKNOWN','NOT_APPLICABLE','ERROR')",
            name="ck_result_status",
        ),
    )


class Gap(Base, TimestampMixin):
    __tablename__ = "gaps"
    gap_id = UUIDColumn(primary_key=True)
    assessment_id = Column(UUID(as_uuid=False), ForeignKey("assessments.assessment_id"), nullable=False)
    field = Column(String(128), nullable=False)
    priority = Column(String(16), nullable=False)  # CRITICAL/HIGH/MEDIUM/LOW
    message = Column(Text, nullable=False)
    rule_id = Column(String(96))
    evidence_text = Column(Text)
    answer = Column(Text)
    source_chunk_ids = Column(JSON, default=list)

    __table_args__ = (Index("idx_gap_assessment", "assessment_id"),)


# ===========================================================================
#  PRODUCTION — Pipeline Run Tracking
# ===========================================================================

class PipelineRun(Base, TimestampMixin):
    """Audit trail for government source sync executions."""
    __tablename__ = "pipeline_runs"
    run_id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    source_id = Column(String(32), ForeignKey("government_sources.source_id"), nullable=False)
    trigger_type = Column(String(16), nullable=False)  # SCHEDULED/MANUAL/ON_DEMAND
    status = Column(String(16), nullable=False, default="RUNNING")  # RUNNING/COMPLETED/FAILED/PARTIAL
    started_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    completed_at = Column(DateTime(timezone=True))
    duration_ms = Column(Integer)
    old_hash = Column(String(64))
    new_hash = Column(String(64))
    # Item 6: requirements_extracted counts attempts, not stored rows - see
    # the identical comment on ExtractionRun. stored_new is the "how much new
    # knowledge did this run add" figure admin summaries/UI should surface.
    requirements_extracted = Column(Integer, nullable=False, default=0)
    stored_new = Column(Integer, nullable=False, default=0)
    requirements_approved = Column(Integer, nullable=False, default=0)
    rules_created = Column(Integer, nullable=False, default=0)
    rules_validated = Column(Integer, nullable=False, default=0)
    rules_approved = Column(Integer, nullable=False, default=0)
    rules_activated = Column(Integer, nullable=False, default=0)
    errors = Column(JSON, default=list)
    metadata_json = Column(JSON, default=dict)

    __table_args__ = (
        Index("idx_pipeline_run_source", "source_id"),
        Index("idx_pipeline_run_status", "status"),
        Index("idx_pipeline_run_started", "started_at"),
        CheckConstraint(
            "status IN ('RUNNING','COMPLETED','FAILED','PARTIAL')",
            name="ck_pipeline_status",
        ),
    )


class SchedulerSettings(Base, TimestampMixin):
    """Live, admin-editable override of the government sync schedule.

    Single-row table (id is fixed to 1 by the CheckConstraint below) so there
    is always exactly one current schedule, global across all sources - not
    per-source. When this row exists, its values override the
    GOVERNMENT_SYNC_*_UTC / GOVERNMENT_SYNC_INTERVAL_MINUTES env-var defaults
    at startup (see src/api/main.py's lifespan) and PUT /admin/scheduler
    applies a change live via APScheduler's reschedule_job/pause_job, without
    requiring a restart.
    """
    __tablename__ = "scheduler_settings"
    id = Column(Integer, primary_key=True, default=1)
    enabled = Column(Boolean, nullable=False, default=True)
    mode = Column(String(16), nullable=False, default="daily")  # daily | interval
    hour_utc = Column(Integer)
    minute_utc = Column(Integer)
    interval_minutes = Column(Integer)
    updated_by = Column(String(256))  # admin's email, server-derived - never client-supplied

    __table_args__ = (
        CheckConstraint("id = 1", name="ck_scheduler_settings_singleton"),
        CheckConstraint("mode IN ('daily', 'interval')", name="ck_scheduler_settings_mode"),
        CheckConstraint(
            "hour_utc IS NULL OR (hour_utc >= 0 AND hour_utc <= 23)",
            name="ck_scheduler_settings_hour",
        ),
        CheckConstraint(
            "minute_utc IS NULL OR (minute_utc >= 0 AND minute_utc <= 59)",
            name="ck_scheduler_settings_minute",
        ),
        CheckConstraint(
            "interval_minutes IS NULL OR interval_minutes >= 15",
            name="ck_scheduler_settings_interval",
        ),
    )


# ===========================================================================
#  PRODUCTION — Authentication + API + Audit
# ===========================================================================

class ApiUser(Base, TimestampMixin):
    __tablename__ = "api_users"
    user_id = UUIDColumn(primary_key=True)
    email = Column(String(256), unique=True, nullable=False)
    password_hash = Column(String(256), nullable=False)
    full_name = Column(String(256))
    role = Column(String(32), nullable=False, default="applicant")  # applicant/admin
    is_active = Column(Boolean, nullable=False, default=True)
    auth_version = Column(Integer, nullable=False, default=0, server_default=text("0"))
    last_login_at = Column(DateTime(timezone=True))

    __table_args__ = (Index("idx_user_email", "email"),)


class ConsultancyTenant(Base, TimestampMixin):
    __tablename__ = "consultancy_tenants"
    tenant_id = UUIDColumn(primary_key=True)
    name = Column(String(200), nullable=False)
    slug = Column(String(120), nullable=False, unique=True)
    status = Column(String(16), nullable=False, default="ACTIVE")
    created_by = Column(
        UUID(as_uuid=False), ForeignKey("api_users.user_id"), nullable=False,
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('ACTIVE','SUSPENDED')",
            name="ck_consultancy_tenant_status",
        ),
    )


class TenantMembership(Base, TimestampMixin):
    __tablename__ = "tenant_memberships"
    membership_id = UUIDColumn(primary_key=True)
    tenant_id = Column(
        UUID(as_uuid=False),
        ForeignKey("consultancy_tenants.tenant_id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id = Column(
        UUID(as_uuid=False), ForeignKey("api_users.user_id"), nullable=False,
    )
    role = Column(String(16), nullable=False)
    status = Column(String(16), nullable=False, default="ACTIVE")

    __table_args__ = (
        UniqueConstraint("tenant_id", "user_id", name="uq_tenant_member_user"),
        Index("idx_tenant_membership_user", "user_id", "status"),
        Index(
            "uq_tenant_active_owner",
            "tenant_id",
            unique=True,
            sqlite_where=text("role = 'OWNER' AND status = 'ACTIVE'"),
            postgresql_where=text("role = 'OWNER' AND status = 'ACTIVE'"),
        ),
        CheckConstraint(
            "role IN ('OWNER','CONSULTANT','REVIEWER','APPLICANT')",
            name="ck_tenant_membership_role",
        ),
        CheckConstraint(
            "status IN ('ACTIVE','REVOKED')",
            name="ck_tenant_membership_status",
        ),
    )


class ApplicationCase(Base, TimestampMixin):
    __tablename__ = "application_cases"
    case_id = UUIDColumn(primary_key=True)
    tenant_id = Column(
        UUID(as_uuid=False),
        ForeignKey("consultancy_tenants.tenant_id", ondelete="CASCADE"),
        nullable=False,
    )
    applicant_id = Column(
        UUID(as_uuid=False),
        ForeignKey("applicant_profiles.applicant_id"),
        nullable=False,
    )
    program_id = Column(
        String(32), ForeignKey("immigration_programs.program_id"), nullable=False,
    )
    created_by = Column(
        UUID(as_uuid=False), ForeignKey("api_users.user_id"), nullable=False,
    )
    assigned_consultant_id = Column(
        UUID(as_uuid=False), ForeignKey("api_users.user_id"),
    )
    assessment_id = Column(
        UUID(as_uuid=False), ForeignKey("assessments.assessment_id"),
    )
    ruleset_id = Column(
        String(36), ForeignKey("rulesets.ruleset_id"),
    )
    status = Column(String(24), nullable=False, default="DRAFT")
    title = Column(String(200))

    __table_args__ = (
        UniqueConstraint("case_id", "tenant_id", name="uq_case_tenant"),
        Index("idx_case_tenant_status", "tenant_id", "status"),
        Index("idx_case_applicant", "applicant_id"),
        Index("idx_case_assigned_consultant", "assigned_consultant_id", "status"),
        CheckConstraint(
            "status IN ('DRAFT','IN_PROGRESS','NEEDS_INFO','READY_FOR_REVIEW',"
            "'CHANGES_REQUESTED','APPROVED','PACKAGE_READY','CLOSED')",
            name="ck_application_case_status",
        ),
    )


class CaseDocument(Base, TimestampMixin):
    __tablename__ = "case_documents"
    document_id = UUIDColumn(primary_key=True)
    tenant_id = Column(
        UUID(as_uuid=False),
        ForeignKey("consultancy_tenants.tenant_id", ondelete="CASCADE"),
        nullable=False,
    )
    case_id = Column(UUID(as_uuid=False), nullable=False)
    uploaded_by = Column(
        UUID(as_uuid=False), ForeignKey("api_users.user_id"), nullable=False,
    )
    filename = Column(String(256), nullable=False)
    content_type = Column(String(64), nullable=False)
    size_bytes = Column(Integer, nullable=False)
    sha256 = Column(String(64), nullable=False)
    storage_key = Column(String(256), nullable=False, unique=True)
    review_status = Column(String(24), nullable=False, default="PENDING_REVIEW")
    checklist_item_id = Column(
        UUID(as_uuid=False), ForeignKey("case_checklist_items.item_id", ondelete="SET NULL")
    )
    version = Column(Integer, nullable=False, default=1)
    replaces_document_id = Column(
        UUID(as_uuid=False), ForeignKey("case_documents.document_id", ondelete="SET NULL")
    )
    scanner_status = Column(String(24), nullable=False, default="NOT_CONFIGURED")  # NOT_CONFIGURED/PENDING/CLEAN/INFECTED/ERROR
    scanner_detail = Column(Text)
    rejection_reason = Column(Text)
    reviewed_by = Column(
        UUID(as_uuid=False), ForeignKey("api_users.user_id")
    )
    reviewed_at = Column(DateTime(timezone=True))

    __table_args__ = (
        ForeignKeyConstraint(
            ["case_id", "tenant_id"],
            ["application_cases.case_id", "application_cases.tenant_id"],
            ondelete="CASCADE",
            name="fk_case_document_case_tenant",
        ),
        Index("idx_case_document_tenant_case", "tenant_id", "case_id"),
        CheckConstraint("size_bytes > 0", name="ck_case_document_size"),
        CheckConstraint(
            "review_status IN ('PENDING_REVIEW','ACCEPTED','REJECTED')",
            name="ck_case_document_review_status",
        ),
    )


class PasswordResetToken(Base, TimestampMixin):
    __tablename__ = "password_reset_tokens"
    reset_id = UUIDColumn(primary_key=True)
    user_id = Column(UUID(as_uuid=False), ForeignKey("api_users.user_id"), nullable=False)
    token_hash = Column(String(64), nullable=False, unique=True)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    used_at = Column(DateTime(timezone=True))

    __table_args__ = (
        Index("idx_password_reset_user", "user_id"),
        Index("idx_password_reset_expiry", "expires_at"),
    )


class UserSession(Base, TimestampMixin):
    __tablename__ = "user_sessions"
    session_id = UUIDColumn(primary_key=True)
    user_id = Column(UUID(as_uuid=False), ForeignKey("api_users.user_id"), nullable=False)
    token_hash = Column(String(64), nullable=False)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    revoked = Column(Boolean, nullable=False, default=False)

    __table_args__ = (
        Index("idx_session_user", "user_id"),
        Index("idx_session_token", "token_hash"),
    )


class AuditLog(Base):
    """Append-only audit trail. Every state change gets a row."""
    __tablename__ = "audit_log"
    audit_id = UUIDColumn(primary_key=True)
    user_id = Column(UUID(as_uuid=False), ForeignKey("api_users.user_id"))
    action = Column(String(64), nullable=False)
    entity_type = Column(String(64), nullable=False)
    entity_id = Column(String(64))
    before_json = Column(JSON)
    after_json = Column(JSON)
    request_id = Column(String(36))
    ip_address = Column(String(64))
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)

    __table_args__ = (
        Index("idx_audit_user", "user_id"),
        Index("idx_audit_entity", "entity_type", "entity_id"),
        Index("idx_audit_created", "created_at"),
    )


class LlmUsage(Base):
    """Per-call LLM usage tracking."""
    __tablename__ = "llm_usage"
    usage_id = UUIDColumn(primary_key=True)
    request_id = Column(String(36))
    user_id = Column(UUID(as_uuid=False), ForeignKey("api_users.user_id"))
    provider = Column(String(32), nullable=False)  # openai/gemini/anthropic
    model = Column(String(64), nullable=False)
    prompt_tokens = Column(Integer, nullable=False, default=0)
    completion_tokens = Column(Integer, nullable=False, default=0)
    total_tokens = Column(Integer, nullable=False, default=0)
    cost_usd = Column(Numeric(16, 6))
    latency_ms = Column(Integer)
    success = Column(Boolean, nullable=False, default=True)
    error_message = Column(Text)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)

    __table_args__ = (
        Index("idx_llm_usage_user", "user_id"),
        Index("idx_llm_usage_provider", "provider", "created_at"),
    )


class LlmBudgetLog(Base):
    """Daily/monthly budget aggregation."""
    __tablename__ = "llm_budget_log"
    log_id = UUIDColumn(primary_key=True)
    period = Column(String(16), nullable=False)  # "daily" / "monthly"
    period_start = Column(DateTime(timezone=True), nullable=False)
    provider = Column(String(32))
    total_cost_usd = Column(Numeric(16, 6), nullable=False, default=0.0)
    total_calls = Column(Integer, nullable=False, default=0)
    total_tokens = Column(Integer, nullable=False, default=0)
    budget_limit_usd = Column(Numeric(16, 6))
    exceeded = Column(Boolean, nullable=False, default=False)

    __table_args__ = (
        Index("idx_budget_period", "period", "period_start"),
    )


class TenantInvitation(Base, TimestampMixin):
    """Pending tenant membership invitation. Accepted by the invited email owner."""
    __tablename__ = "tenant_invitations"
    invitation_id = UUIDColumn(primary_key=True)
    tenant_id = Column(
        UUID(as_uuid=False),
        ForeignKey("consultancy_tenants.tenant_id", ondelete="CASCADE"),
        nullable=False,
    )
    email = Column(String(256), nullable=False)
    role = Column(String(16), nullable=False)
    token_hash = Column(String(64), nullable=False, unique=True)
    invited_by = Column(
        UUID(as_uuid=False), ForeignKey("api_users.user_id"), nullable=False
    )
    expires_at = Column(DateTime(timezone=True), nullable=False)
    accepted_at = Column(DateTime(timezone=True))
    revoked_at = Column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint(
            "role IN ('CONSULTANT', 'REVIEWER', 'APPLICANT')",
            name="ck_tenant_invitation_role",
        ),
        Index("idx_invitation_tenant_email", "tenant_id", "email"),
        Index("idx_invitation_token", "token_hash"),
    )


class CaseChecklistItem(Base, TimestampMixin):
    """Checklist item for an application case: document or information required."""
    __tablename__ = "case_checklist_items"
    item_id = UUIDColumn(primary_key=True)
    tenant_id = Column(
        UUID(as_uuid=False),
        ForeignKey("consultancy_tenants.tenant_id", ondelete="CASCADE"),
        nullable=False,
    )
    case_id = Column(UUID(as_uuid=False), nullable=False)
    requirement_key = Column(String(128), nullable=False)
    label = Column(String(200), nullable=False)
    description = Column(Text)
    category = Column(String(32), nullable=False)  # document / information / form
    required = Column(Boolean, nullable=False, default=True)
    status = Column(String(24), nullable=False, default="PENDING")  # PENDING/SUBMITTED/ACCEPTED/REJECTED/WAIVED
    evidence_document_id = Column(
        UUID(as_uuid=False), ForeignKey("case_documents.document_id", ondelete="SET NULL")
    )
    rejection_reason = Column(Text)
    reviewed_by = Column(
        UUID(as_uuid=False), ForeignKey("api_users.user_id")
    )
    reviewed_at = Column(DateTime(timezone=True))
    position = Column(Integer, nullable=False, default=0)

    __table_args__ = (
        ForeignKeyConstraint(
            ["case_id", "tenant_id"],
            ["application_cases.case_id", "application_cases.tenant_id"],
            name="fk_case_checklist_case_tenant",
            ondelete="CASCADE",
        ),
        UniqueConstraint("case_id", "requirement_key", name="uq_case_checklist_requirement"),
        Index("idx_case_checklist_case", "case_id", "status"),
        CheckConstraint(
            "status IN ('PENDING','SUBMITTED','ACCEPTED','REJECTED','WAIVED')",
            name="ck_case_checklist_status",
        ),
        CheckConstraint(
            "category IN ('document','information','form')",
            name="ck_case_checklist_category",
        ),
    )


class FormTemplate(Base):
    """Versioned application form template. JSON schema describes the questions."""
    __tablename__ = "form_templates"
    template_id = UUIDColumn(primary_key=True)
    tenant_id = Column(
        UUID(as_uuid=False),
        ForeignKey("consultancy_tenants.tenant_id", ondelete="CASCADE"),
    )
    form_key = Column(String(64), nullable=False)
    version = Column(Integer, nullable=False, default=1)
    title = Column(String(200), nullable=False)
    program_id = Column(String(32), ForeignKey("immigration_programs.program_id"))
    schema_json = Column(JSON, nullable=False, default=dict)
    status = Column(String(16), nullable=False, default="PUBLISHED")
    created_by = Column(
        UUID(as_uuid=False), ForeignKey("api_users.user_id"), nullable=False
    )
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)

    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "form_key",
            "version",
            name="uq_form_template_tenant_key_version",
        ),
        Index(
            "uq_form_template_global_key_version",
            "form_key",
            "version",
            unique=True,
            postgresql_where=tenant_id.is_(None),
            sqlite_where=tenant_id.is_(None),
        ),
        Index("idx_form_template_tenant_program", "tenant_id", "program_id"),
        Index("idx_form_template_program", "program_id"),
        CheckConstraint("status IN ('PUBLISHED','ARCHIVED')", name="ck_form_template_status"),
    )


class FormAnswer(Base, TimestampMixin):
    """Applicant/consultant answers to a form template, tied to a case."""
    __tablename__ = "form_answers"
    answer_id = UUIDColumn(primary_key=True)
    tenant_id = Column(
        UUID(as_uuid=False),
        ForeignKey("consultancy_tenants.tenant_id", ondelete="CASCADE"),
        nullable=False,
    )
    case_id = Column(UUID(as_uuid=False), nullable=False)
    template_id = Column(
        UUID(as_uuid=False), ForeignKey("form_templates.template_id", ondelete="CASCADE"), nullable=False
    )
    answers_json = Column(JSON, nullable=False, default=dict)
    status = Column(String(24), nullable=False, default="DRAFT")  # DRAFT/SUBMITTED/REVIEWED/APPROVED
    submitted_at = Column(DateTime(timezone=True))
    applicant_confirmed_at = Column(DateTime(timezone=True))
    reviewed_by = Column(
        UUID(as_uuid=False), ForeignKey("api_users.user_id")
    )
    reviewed_at = Column(DateTime(timezone=True))
    review_notes = Column(Text)

    __table_args__ = (
        ForeignKeyConstraint(
            ["case_id", "tenant_id"],
            ["application_cases.case_id", "application_cases.tenant_id"],
            name="fk_form_answer_case_tenant",
            ondelete="CASCADE",
        ),
        UniqueConstraint("case_id", "template_id", name="uq_form_answer_case_template"),
        Index("idx_form_answer_case", "case_id", "status"),
        CheckConstraint(
            "status IN ('DRAFT','SUBMITTED','REVIEWED','APPROVED')",
            name="ck_form_answer_status",
        ),
    )
