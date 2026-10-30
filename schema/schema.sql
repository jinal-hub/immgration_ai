-- Phase-1 schema: Government Source Registry + Document Metadata + Versioning
-- Scope: Australia, Skilled Migration (189 / 190 / 491) only.
-- Visa category vocabulary reconciled with live ORM models to:
--   SKILLED / WORK / STUDENT / VISITOR / FAMILY / BUSINESS /
--   HUMANITARIAN / OTHER / GENERAL
-- Engine: SQLite (file-backed, matches the "file-backed now, DB-backed later" plan).

PRAGMA foreign_keys = ON;

-- ---------------------------------------------------------------------------
-- 1. countries
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS countries (
    country_id   TEXT PRIMARY KEY,          -- 'AU'
    name         TEXT NOT NULL,
    iso_code     TEXT NOT NULL
);

-- ---------------------------------------------------------------------------
-- 2. immigration_programs
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS immigration_programs (
    program_id     TEXT PRIMARY KEY,        -- 'SKILLED_189'
    country_id     TEXT NOT NULL REFERENCES countries(country_id),
    visa_category  TEXT NOT NULL CHECK (visa_category IN
                     ('SKILLED','WORK','STUDENT','VISITOR','FAMILY',
                      'BUSINESS','HUMANITARIAN','OTHER','GENERAL')),
    subclass_code  TEXT,                    -- '189'
    name           TEXT NOT NULL,
    status         TEXT NOT NULL DEFAULT 'ACTIVE' CHECK
                     (status IN ('ACTIVE','CLOSED','SUSPENDED','REFERENCE_ONLY'))
);

-- ---------------------------------------------------------------------------
-- 3. government_sources  (the registry itself)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS government_sources (
    source_id           TEXT PRIMARY KEY,          -- 'SRC-AU-189-MAIN'
    country_id          TEXT NOT NULL REFERENCES countries(country_id),
    program_id          TEXT NOT NULL REFERENCES immigration_programs(program_id),
    visa_category        TEXT NOT NULL CHECK (visa_category IN
                     ('SKILLED','WORK','STUDENT','VISITOR','FAMILY',
                      'BUSINESS','HUMANITARIAN','OTHER','GENERAL')),
    authority            TEXT NOT NULL,             -- 'Department of Home Affairs'
    url                  TEXT NOT NULL UNIQUE,
    source_type          TEXT NOT NULL CHECK (source_type IN ('HTML','PDF')),
    source_subtype        TEXT,                     -- 'ELIGIBILITY_GUIDANCE' / 'POINTS_TABLE' / 'LEGISLATION_TEXT' / ...
    priority              TEXT NOT NULL CHECK (priority IN
                     ('PRIMARY','SECONDARY','EMBASSY','GENERAL')),
    monitoring_method      TEXT NOT NULL DEFAULT 'WEB' CHECK (monitoring_method IN ('WEB','API','MANUAL','BROWSER')),
    crawl_frequency        TEXT NOT NULL DEFAULT 'DAILY' CHECK (crawl_frequency IN
                     ('HOURLY','DAILY','WEEKLY','MONTHLY')),
    extract_requirements INTEGER NOT NULL DEFAULT 1,
    content_kind         TEXT NOT NULL DEFAULT 'PAGE' CHECK (content_kind IN ('PAGE','NEWS_LIST')),
    last_checked_at        TEXT,                    -- ISO8601 timestamp
    last_hash              TEXT,                    -- sha256 of raw bytes at last successful fetch
    status                 TEXT NOT NULL DEFAULT 'ACTIVE' CHECK (status IN
                     ('ACTIVE','INACTIVE','BROKEN','ACCESS_BLOCKED','NETWORK_RESTRICTED',
                      'REQUIRES_BROWSER','REQUIRES_MANUAL_DOWNLOAD','NEEDS_MANUAL_CAPTURE')),
    notes                  TEXT
);

CREATE INDEX IF NOT EXISTS idx_sources_program ON government_sources(program_id);
CREATE INDEX IF NOT EXISTS idx_sources_category ON government_sources(visa_category);

-- ---------------------------------------------------------------------------
-- 4. government_documents  (current known state per source)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS government_documents (
    document_id       TEXT PRIMARY KEY,           -- 'DOC-AU-189-MAIN'
    source_id         TEXT NOT NULL REFERENCES government_sources(source_id),
    country_id        TEXT NOT NULL REFERENCES countries(country_id),
    program_id        TEXT NOT NULL REFERENCES immigration_programs(program_id),
    visa_category     TEXT NOT NULL CHECK (visa_category IN
                     ('SKILLED','WORK','STUDENT','VISITOR','FAMILY',
                      'BUSINESS','HUMANITARIAN','OTHER','GENERAL')),
    document_type     TEXT NOT NULL,               -- 'ELIGIBILITY_GUIDANCE','POINTS_TABLE','LEGISLATION_TEXT'
    url               TEXT NOT NULL,
    title             TEXT,
    published_date    TEXT,
    effective_date    TEXT,
    retrieved_at      TEXT NOT NULL,
    content_hash      TEXT NOT NULL,               -- sha256 of raw bytes, current version
    current_version   INTEGER NOT NULL DEFAULT 1,
    language          TEXT NOT NULL DEFAULT 'en',
    raw_path          TEXT NOT NULL,                -- path to current raw snapshot
    extracted_path    TEXT,                         -- path to current extracted JSON
    status            TEXT NOT NULL DEFAULT 'CURRENT' CHECK (status IN ('CURRENT','SUPERSEDED','REMOVED','NEEDS_OCR'))
);

CREATE INDEX IF NOT EXISTS idx_documents_source ON government_documents(source_id);

-- ---------------------------------------------------------------------------
-- 5. document_versions  (append-only history)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS document_versions (
    version_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id        TEXT NOT NULL REFERENCES government_documents(document_id),
    version_number      INTEGER NOT NULL,
    content_hash        TEXT NOT NULL,
    retrieved_at        TEXT NOT NULL,
    raw_path            TEXT NOT NULL,
    extracted_path       TEXT,
    change_type          TEXT NOT NULL CHECK (change_type IN ('INITIAL','CHANGED','UNCHANGED')),
    diff_summary         TEXT,                       -- reserved for Phase-2 semantic diff
    UNIQUE(document_id, version_number)
);

-- ---------------------------------------------------------------------------
-- 6. document_chunks  (structural decomposition; NO rule/requirement classification yet)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS document_chunks (
    chunk_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id   TEXT NOT NULL REFERENCES government_documents(document_id),
    version_id    INTEGER NOT NULL REFERENCES document_versions(version_id),
    chunk_type    TEXT NOT NULL CHECK (chunk_type IN ('HEADING','PARAGRAPH','TABLE','LIST')),
    heading_path  TEXT,                    -- e.g. 'Eligibility > Age requirement'
    order_index   INTEGER NOT NULL,
    raw_text      TEXT NOT NULL,
    page_number   INTEGER                  -- populated for PDF sources only
);

CREATE INDEX IF NOT EXISTS idx_chunks_document ON document_chunks(document_id, version_id);

-- ---------------------------------------------------------------------------
-- 7. fetch_log  (audit trail of every fetch attempt)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS fetch_log (
    log_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id     TEXT NOT NULL REFERENCES government_sources(source_id),
    fetched_at    TEXT NOT NULL,
    http_status   INTEGER,
    status        TEXT NOT NULL DEFAULT 'OK' CHECK (status IN
                     ('OK','FETCH_FAILED','ACCESS_BLOCKED','NETWORK_RESTRICTED',
                      'REQUIRES_BROWSER','REQUIRES_MANUAL_DOWNLOAD','INVALID_CONTENT',
                      'EXTRACTION_FAILED')),
    content_hash  TEXT,
    changed       INTEGER NOT NULL DEFAULT 0 CHECK (changed IN (0,1)),
    duration_ms   INTEGER,
    error         TEXT
);

CREATE INDEX IF NOT EXISTS idx_fetchlog_source ON fetch_log(source_id, fetched_at);
