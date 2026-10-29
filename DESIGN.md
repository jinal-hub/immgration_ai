# Immigration Knowledge Platform — Full Architecture Design

## 0. Scope

End-to-end deterministic immigration eligibility system for Australian skilled migration visas.

- **Country**: Australia
- **Visas**: 189 (Skilled Independent), 190 (Skilled Nominated), 491 (Skilled Work Regional)
- **Authorities**: Department of Home Affairs, Federal Register of Legislation
- **Core principle**: Every eligibility decision is deterministic, auditable, traceable to government evidence

### Phases

| Phase | Purpose | Status |
|-------|---------|--------|
| 1 | Data Ingestion (source registry, fetch, extract, hash, change detect) | COMPLETE |
| 2 | Knowledge Extraction (LLM requirement extraction, evidence validation, human review) | COMPLETE |
| 3 | Deterministic Rule Engine (operators, expressions, IF/THEN, exceptions, dependencies, versioning) | COMPLETE |
| 3C | Eligibility + Gap Engine (applicant model, applicability, assessment, gap detection, questions) | COMPLETE |
| 3D | Government Knowledge Activation (req→rule bridge, status machine, admin approval, e2e) | COMPLETE |
| API | FastAPI service layer (auth, CRUD, admin review, assessments, documents, search) | COMPLETE |
| Infra | DB abstraction, Docker, LLM governance, structured logging | COMPLETE |
| 4a | Six modelled applicant concepts (visa history, sponsorship, experience, location, insurance, GTE) | COMPLETE |
| 4b | Rule dry run + "not evaluable" guard on activation | COMPLETE |
| 4c | PostgreSQL HTTP e2e for the review → compile → activate → assess flow | COMPLETE |
| 5 | Admin UI review/search/sync actions + Playwright/axe browser suite | COMPLETE |
| 6 | CI gates (hermetic, PostgreSQL e2e, frontend, images) + Dependabot | COMPLETE |
| 7 | Test/package cleanup (no phase-named test packages, no scratch scripts) | COMPLETE |
| 8+ | Real-data supersession proof, RAG, recommendations, applicant product, multi-country | NOT STARTED |

**Current state (2026-10-27)**: 1,695 backend tests pass in the hermetic suite and 56 pass
against real PostgreSQL + pgvector; the frontend runs 42 unit tests and 5 Playwright/axe
tests. The production compose topology is config-checked and the non-root images were
smoke-tested, but no target host has been provisioned. CI runs all four gates on every
change. The remaining work is human-gated (live fetch confirmation, real source syncs, rule
activation, secret rotation) and breadth (real supersession, WORK/STUDENT/VISITOR gold,
applicant-facing UI, multi-country).

---

## 1. Phase 1 — Data Ingestion

### Pipeline

```
SOURCE REGISTRY (seed JSON)
        │
        ▼
     FETCHER  ── Playwright Chromium (headless, honest UA), records status/headers/timing
        │
        ▼
  RAW STORAGE  ── immutable HTML/PDF bytes on disk, one snapshot per fetch date
        │
        ▼
  EXTRACTION   ── HTML → structured text (headings/paragraphs/lists/tables)
        │
        ▼
  CONTENT HASH ── sha256 of raw bytes AND of extracted clean text
        │
        ▼
CHANGE DETECTION ── hash comparison → NEW / UNCHANGED / CHANGED
        │
        ▼
   POSTGRESQL  ── sources, documents, versions, chunks, fetch_log (SQLite in tests)
```

No LLM. No rules. Pure data engineering.

### Retrieval path (2026-10-23)

There is one fetch path: `src/fetch/playwright_client.py` drives a real headless
Chromium through Playwright. It sends one honest `FETCH_USER_AGENT`, loads pages in
`en-AU`, and uses only the launch flags a container needs (`--no-sandbox`,
`--disable-dev-shm-usage`). It waits (bounded) for a real structural selector because
several sources render after `domcontentloaded`; waiting stops racing the page, it does
not disguise the client.

Politeness lives in `src/fetch/politeness.py`, `src/orchestration/host_cooldown.py` and
`src/fetch/proxy.py`: same-host spacing plus jitter, `Crawl-delay`/`Retry-After`,
a 403/429 cooldown for the whole host, and one optional fixed egress proxy that fails
closed. A refusal is recorded and the run gets shorter, never more aggressive.

A retired requests/Patchright path remains reachable only through
`FETCH_BACKEND=legacy` until `scripts/live_fetch_check.py` confirms the Playwright port
against a real source. It is then deleted together with the `patchright` dependency.

### Source Registry

14 verified government sources across all four visa categories — SKILLED (189/190/491 + Migration Regulations), WORK (482 main + Skill Occupation List), STUDENT (500 main + CRICOS register), VISITOR (600 main) — covering 7 programs in total.

### Change Detection

- `NEW`: No prior hash → create document + version 1
- `CHANGED`: Hash differs → bump version, re-extract
- `UNCHANGED`: Hash matches → update `last_checked_at`, skip re-extraction

---

## 2. Phase 2 — Knowledge Extraction

### Pipeline

```
document_chunks (Phase 1)
        │
        ▼
chunk_selector.py       — recall-oriented candidate detection
        │
        ▼
llm_client.py           — Cohere by default (OpenAI/Gemini legacy model routing); MockLLMClient (unit tests only)
        │
        ▼
requirement_validator.py — JSON Schema + logic consistency
        │
        ▼
evidence_validator.py    — exact substring verification against source chunks
        │
        ▼
requirement_normalizer.py — controlled vocabulary for operator/unit/category
        │
        ▼
requirement_deduplicator.py — semantic dedup (EXACT_DUPLICATE / COMPLEMENTARY / CONFLICT)
        │
        ▼
requirement_repository.py  — persistence with version tracking
        │
        ▼
review.py                  — human approve/reject with audit trail
```

### Key Models

```
Government Source → Document → Document Version → Document Chunk → Requirement → Rule
```

### Evidence Anti-Hallucination

Every extracted requirement must have `evidence_text` that is an exact substring of its cited `source_chunk`. Fabricated/paraphrased evidence is hard-rejected.

### Gold Standard

Real gold datasets are organized **per visa category** under `data/gold/<category>/` and each is validated against its own prompt contract — enforcing **zero cross-category contamination** (a Student source is never scored with the Skilled contract).

- **skilled/** — populated with real, verbatim gold: `skilled_189_real.json` (23 items) + `skilled_190_real.json` (6 items). The 189 dataset's 17 evidence strings are verified verbatim substrings; it also carries annotations for the 190 cross-source set.
- **work/student/visitor/** — placeholders marked `AWAITING_REAL_SOURCE` with **zero items**.
  The anti-hallucination rule forbids fabricating `evidence_text`, and no human-gated capture
  for these categories has been run yet. The browser path can reach real Home Affairs content;
  `SRC-AU-CRICOS-REGISTER` is a search-only SPA and needs a different source. Earlier
  "headless vs headed" WAF claims were wrong (see §16); the current, honest position is that
  the WAF blocks intermittently under request volume, so capture is attempted politely and
  recorded when it fails — never retried with a different identity.

The integrity harness `src/extraction/category_gold.py` fingerprints each category's prompt contract and every gold dataset, and `tests/extraction/test_category_gold_harness.py` (12 tests) asserts the contamination-free + no-fabricated-evidence invariants hold.

### Production Model

gpt-4o at temperature=0: F1=0.882, Evidence Accuracy=1.0, Critical Recall=1.0 (8/8).

**2026-09-25**: Gemini (`gemini-flash-lite-latest`) was added as a second fully-supported
production provider alongside OpenAI — `get_client()` in `src/extraction/llm_client.py` routes to
`OpenAILLMClient` or `GeminiLLMClient` by model name prefix, both wired through the same
governance layer (rate limiting, budget, response cache, usage logging). Anthropic/mock models
are still rejected at the factory (fail-fast, no silent fallback). No comparative F1/accuracy
numbers have been measured for Gemini yet on this project's gold sets.

---

## 3. Phase 3 — Deterministic Rule Engine

### Data Model

```
Rule
├── rule_id: str
├── requirement_id: str (traceability to Phase 2)
├── program_id: str (SKILLED_189 etc.)
├── category: str
├── field: str (field name in applicant data)
├── operator: str (EQ, LT, GTE, IN, etc.)
├── value: Any (comparison value)
├── unit: str | None
├── mandatory: bool
├── critical: bool
├── message: str
├── provenance: Provenance
│   ├── source_id → document_id → chunk_id → requirement_id
│   └── evidence_text (verbatim from source)
├── status: DRAFT | ACTIVE | SUPERSEDED | RETIRED
├── version: int
└── created_at: str (ISO 8601)
```

### Four-State Semantics

| Status | Meaning |
|--------|---------|
| PASS | Rule applies, condition passed |
| FAIL | Rule applies, condition failed |
| UNKNOWN | Rule applies, required information missing |
| NOT_APPLICABLE | Rule does not apply to this applicant/pathway |

### Operators (12)

`EQ` `NEQ` `LT` `LTE` `GT` `GTE` `IN` `NOT_IN` `CONTAINS` `NOT_CONTAINS` `EXISTS` `NOT_EXISTS`

### Expression Tree (7 Types)

#### RuleLeaf
Single rule evaluation. Looks up `rule_id` in registry, evaluates `field operator value` against applicant data.

#### ANDNode
All children must PASS. NOT_APPLICABLE is filtered out before evaluation. FAIL > UNKNOWN > PASS.

#### ORNode
At least one child must PASS. NOT_APPLICABLE is filtered out. PASS > UNKNOWN > FAIL.

#### NOTNode
Inverts PASS ↔ FAIL. UNKNOWN and NOT_APPLICABLE stay unchanged.

#### ConditionalNode (IF/THEN)

| Condition | Consequence Evaluated? | Result |
|-----------|----------------------|--------|
| PASS | Yes | Consequence status |
| FAIL | No | NOT_APPLICABLE |
| UNKNOWN | No | UNKNOWN |
| NOT_APPLICABLE | No | NOT_APPLICABLE |

#### ExceptionNode (Rule UNLESS Exception)

| Exception | Main | Result |
|-----------|------|--------|
| PASS (applies) | PASS | FAIL (overridden) |
| PASS (applies) | FAIL | FAIL |
| PASS (applies) | UNKNOWN | UNKNOWN |
| FAIL/UNKNOWN/NOT_APPLICABLE | any | Main result unchanged |

#### DependencyNode (Rule REQUIRES Prerequisites)

| Dependency | Result |
|-----------|--------|
| FAIL | NOT_APPLICABLE |
| NOT_APPLICABLE | NOT_APPLICABLE |
| UNKNOWN | UNKNOWN |
| All PASS | Evaluate rule normally |

### Versioning

Rules have `version` (int) and `status` (DRAFT/VALIDATED/APPROVED/ACTIVE/SUPERSEDED/RETIRED). When policy changes:
1. Create v2 with ACTIVE status
2. Mark v1 as SUPERSEDED
3. Historical assessments still reference v1
4. Re-evaluating historical assessments against v1 produces identical results

Verified by `TestHistoricalAssessmentReproducibility` (3 tests).

---

## 4. Phase 3D — Government Knowledge Activation Pipeline

### Overview

Connects Phase 2 approved requirements to active, persistent rules in the database through a human-in-the-loop approval workflow.

### Pipeline Flow

```
Government Source
    │
    ▼ Phase 1 & 2
Approved Requirement (status=APPROVED)
    │
    ▼ compile_approved_requirements()
Rule Dict (compiled from requirement)
    │
    ▼ EnhancedRuleRepo.save()
Rule Version (status=DRAFT)
    │
    ▼ Admin: POST /validate
Rule Version (status=VALIDATED)
    │
    ▼ Admin: POST /approve
Rule Version (status=APPROVED)
    │
    ▼ Admin: POST /activate
Rule Version (status=ACTIVE)
    │
    ▼ (if v2) Transactional supersession
    │   v1 status: ACTIVE → SUPERSEDED
    │   v2 status: APPROVED → ACTIVE
```

### Status Machine

```
RuleLifecycleStatus:
    DRAFT → VALIDATED → APPROVED → ACTIVE → SUPERSEDED → RETIRED
                                   ↓
                               REJECTED (from any pre-active state)
```

**Illegal transitions raise `RuleActivationError`:**
- DRAFT → ACTIVE (bypasses VALIDATED/APPROVED)
- ACTIVE → DRAFT (no backwards)
- SUPERSEDED → ACTIVE (no resurrection)
- RETIRED → anything

### EnhancedRuleRepo

```python
class EnhancedRuleRepo:
    """Full CRUD + status transitions + transactional activation + version snapshots."""

    def save(self, rule_dict: dict, created_by: str) -> RuleVersion
    def get(self, rule_id: str) -> Rule | None
    def list(self, status: str | None = None) -> list[Rule]
    def activate(self, rule_id: str, rule_version_id: str, activated_by: str) -> None
    """Transactional: supersede v1 → activate v2. Never two ACTIVE for same logical key."""

    def get_active_version(self, rule_id: str) -> RuleVersion | None
    def get_all_versions(self, rule_id: str) -> list[RuleVersion]
```

**Version auto-increment**: `version_number` is computed as `max(existing version_numbers for this rule_id) + 1`, preventing UNIQUE constraint collisions on `rule_versions(rule_id, version_number)`.

### Requirement → Rule Bridge

```python
def compile_approved_requirements(session) -> list[dict]:
    """Load all approved requirements, compile each to a rule dict.
    Re-compiles existing ACTIVE requirements too (they get reset to DRAFT)."""

def _load_requirement_dict(req: Requirement) -> dict:
    """Load requirement with evidence into a compilation-ready dict."""
```

### Orchestrator

```python
sync_source(session, source_id, dry_run=False, trigger_type="ON_DEMAND") → SyncResult
# Creates PipelineRun at start, updates on completion/failure
# Steps: re-fetch (always) → classify fresh vs previous hash → compile rules
#   dry_run = stored-state preview via detect_change, no network

sync_all_sources(session, dry_run=False, trigger_type="ON_DEMAND") → [SyncResult]
# Syncs all ACTIVE sources, one PipelineRun per source
# SCHEDULED runs skip sources not yet due per crawl_frequency/last_checked_at;
# MANUAL/ON_DEMAND always re-fetch
```

Every sync execution tracked via `PipelineRun` (trigger_type: SCHEDULED/MANUAL/ON_DEMAND).

### Admin API

```python
# /api/v1/admin/rules
GET  /queue               → Review queue (DRAFT + VALIDATED + APPROVED rules)
GET  /                    → List all rules, optional ?status filter
GET  /{id}                → Get single rule with version history
POST /{id}/validate       → DRAFT → VALIDATED
POST /{id}/approve        → VALIDATED → APPROVED
POST /{id}/activate       → APPROVED → ACTIVE (transactional supersession)
POST /{id}/reject         → Any pre-active → REJECTED
POST /{id}/retire         → SUPERSEDED/RETIRED → RETIRED
POST /batch/activate      → Bulk activate multiple rules
```

### Pipeline Run API

```python
# /api/v1/admin/runs
GET  /                    → List all runs (newest first, paginated)
GET  /summary             → Aggregate counts by status
GET  /{id}                → Get single run details
GET  /source/{id}         → List runs for a specific source
```

### PipelineRun Model

```python
PipelineRun:
    run_id: UUID
    source_id: FK → GovernmentSource
    trigger_type: str          # SCHEDULED / MANUAL / ON_DEMAND
    status: str                # RUNNING / COMPLETED / FAILED / PARTIAL
    started_at: datetime
    completed_at: datetime | None
    duration_ms: int | None
    old_hash: str | None       # content hash before sync
    new_hash: str | None       # content hash after sync
    requirements_extracted: int
    requirements_approved: int
    rules_created: int
    rules_validated: int       # computed at query time (not stored)
    rules_approved: int        # computed at query time (not stored)
    rules_activated: int       # computed at query time (not stored)
    errors: JSON               # list of error strings
    metadata_json: JSON        # arbitrary metadata
```

**Computed counters**: `rules_validated`, `rules_approved`, `rules_activated` are calculated at query time via `_compute_rule_counts(session, source_id)` by counting Rule rows by status for the given source. These are not stored on the PipelineRun itself, as rules may be approved/activated after the run completes.

Non-admin users: 403.

### Provenance Chain (Complete)

```
RuleVersion.snapshot_json.requirement_id
    → Requirement (Phase 2 approved)
        → RequirementEvidence (extracted evidence)
            → DocumentChunk.text (verbatim source)
                → DocumentVersion (version number, hash)
                    → GovernmentDocument (filename, visa_category)
                        → GovernmentSource (URL, program, authority)
```

Every ACTIVE rule can answer: "Why does the system currently believe this rule exists?" with an exact chain from rule to government URL.

---

## 5. Phase 3C — Eligibility + Gap Engine

### Architecture

```
ACTIVE RULES (Phase 3D)
      +
APPLICANT PROFILE (user input)
      │
      ▼
APPLICABILITY FILTER
├── Program filter: rule.program_id == applicant.visa.program_id
├── Status filter: rule.status == ACTIVE
└── Version filter: highest ACTIVE version per rule_id
      │
      ▼
EXPRESSION TREE EVALUATION
├── Rule engine evaluates field/operator/value
├── AND/OR/NOT/IF_THEN/EXCEPTION/DEPENDENCY compose results
└── Four-state: PASS/FAIL/UNKNOWN/NOT_APPLICABLE
      │
      ▼
ELIGIBILITY ASSESSMENT
├── ELIGIBLE:    All applicable mandatory rules PASS
├── INELIGIBLE:  Any mandatory/critical rule FAIL
└── INCOMPLETE:  No FAIL, but some required information UNKNOWN
      │
      ▼ (if INCOMPLETE)
GAP ENGINE
├── Find all UNKNOWN evaluations
├── Resolve missing fields
├── Deduplicate (multiple rules → same field)
├── Prioritize: CRITICAL > HIGH > MEDIUM > LOW
├── Link to source rules and evidence
└── Generate deterministic questions
      │
      ▼
USER ANSWERS → NORMALIZE → RE-EVALUATE
```

### Applicant Profile Model

```python
ApplicantProfile(
    visa=VisaSelection(program_id="SKILLED_189"),
    age=30,                        # int | None
    english_level="proficient",    # str | None (none|vocational|competent|proficient|superior)
    points=70,                     # int | None
    occupation="261313",           # str | None (ANZSCO code)
    skills_assessment_status="VALID",  # str | None
    health_status="PASSED",        # str | None
    character_status="PASSED",     # str | None
    has_regional_nomination=None,  # bool | None
    visa_subclass=None,            # str | None

    # Second batch (Phase 4a) - facts an applicant can supply
    visa_history=None,                    # str | None
    employer_sponsored=None,              # bool | None
    work_experience_years=None,           # int | None
    in_australia_at_application=None,     # bool | None
    has_health_insurance=None,            # bool | None
    genuine_temporary_entrant=None,       # bool | None
)
```

Fields are modelled only when an applicant can answer them. A rule that references a
program-level fact (for example an application window) has no applicant counterpart, so
it is reported as not evaluable rather than silently UNKNOWN forever; activating such a
rule requires `confirm_not_evaluable="ACTIVATE"`.

**Critical rule**: Missing fields are `None`, NOT default values. `to_rule_data()` excludes `None` fields from the dict. Rule engine returns `UNKNOWN` for any rule referencing a field not in the data.

### Applicability Filtering

```python
filter_applicable_rules(rules, applicant) → [Rule]
# Filters by: program_id match + ACTIVE status only

select_rule_version(rules, rule_id) → Rule | None
# Returns highest ACTIVE version
```

### Eligibility Assessment

```python
assess_eligibility(expression, rules_by_id, applicant) → EligibilityOutcome
# Evaluates expression tree against profile
# Maps raw statuses to business outcome

# Business outcomes:
# ELIGIBLE:   All PASS
# INELIGIBLE: Any FAIL
# INCOMPLETE: Some UNKNOWN, no FAIL
```

### Gap Engine

```python
GapEngine.find_gaps(evaluations, rules_by_id) → [Gap]

Gap:
    field: str                              # "points", "english_level", etc.
    priority: CRITICAL | HIGH | MEDIUM | LOW
    source_rule_ids: list[str]              # which rules need this field
    source_evidence: str | None             # government evidence
    question: str                           # user-facing question
```

Priority derivation:
- `critical=True` → CRITICAL
- `mandatory=True, critical=False` → HIGH
- `mandatory=False, critical=False` → MEDIUM

### Deterministic Questions

```python
QuestionBank.get_question("english_level") → QuestionDefinition
# text: "What is your current English proficiency level?"
# accepted_values: ["none", "vocational", "competent", "proficient", "superior"]
# validate(answer) → bool

AnswerNormalizer.normalize("english_level", "PROFICIENT") → "proficient"
AnswerNormalizer.normalize("english_level", "IELTS 7") → "competent"
```

---

## 6. API Layer

### Application Factory

```python
from api.main import create_app, app
# create_app() → FastAPI instance (used by uvicorn)
# app is the module-level singleton

# Lifespan handler:
#   - Sets up structured logging (INFO, JSON output)
#   - Initializes DB engine from DATABASE_URL
#   - Stores engine in app.state.db_engine
```

### CORS Configuration

```python
# main.py
_cors_origins_str = os.getenv("CORS_ORIGINS", "http://localhost:3000")
_cors_origins = [o.strip() for o in _cors_origins_str.split(",") if o.strip()]
# Explicitly NOT allow_origins=["*"] — browser security risk with credentials
```

### Request Middleware

```python
# Every request gets:
#   - X-Request-ID (from header or auto-generated UUID)
#   - Structured logging context with request_id
#   - Response header X-Request-ID echoed back
```

### Route Registry

Updated as part of the admin-frontend build-out (see "Route removal" note below for what
changed and why).

```
/health                                        — GET health check (no auth)
/ready                                         — GET readiness check (no auth)
/api/v1/auth/register                          — POST (no auth; 403 unless ALLOW_PUBLIC_REGISTRATION=true)
/api/v1/auth/login                             — POST (no auth)
/api/v1/auth/me                                — GET (JWT required)
/api/v1/applicants/                            — POST, GET (JWT required, owner-scoped)
/api/v1/applicants/{id}                        — GET, PATCH (JWT required, owner-scoped)
/api/v1/assessments/                           — POST, GET (JWT required, owner-scoped)
/api/v1/assessments/{id}                       — GET (JWT required, owner-scoped)
/api/v1/assessments/evaluate                   — POST (JWT required, owner-scoped)
/api/v1/search                                 — POST (JWT required; keyword/semantic/auto)
/api/v1/admin/rules/queue                      — GET (admin only)
/api/v1/admin/rules/evaluate                   — POST (admin only; dry run, saves nothing)
/api/v1/admin/rules/                           — GET, ?status=/?program_id= (admin only)
/api/v1/admin/rules/batch/validate             — POST (admin only)
/api/v1/admin/rules/batch/approve              — POST (admin only)
/api/v1/admin/rules/batch/activate             — POST (admin only)
/api/v1/admin/rules/{id}                       — GET (admin only)
/api/v1/admin/rules/{id}/validate              — POST (admin only)
/api/v1/admin/rules/{id}/approve               — POST (admin only)
/api/v1/admin/rules/{id}/activate              — POST (admin only)
/api/v1/admin/rules/{id}/reject                — POST (admin only)
/api/v1/admin/rules/{id}/retire                — POST (admin only)
/api/v1/admin/requirements/review-queue        — GET, ?status=/?program_id= (admin only)
/api/v1/admin/requirements/{id}                — GET (admin only)
/api/v1/admin/requirements/review/{id}/approve           — POST (admin only)
/api/v1/admin/requirements/review/{id}/reject            — POST (admin only)
/api/v1/admin/requirements/review/{id}/mark-needs-review — POST (admin only)
/api/v1/admin/sources/                         — GET, POST (admin only)
/api/v1/admin/sources/{id}                     — GET, PATCH (admin only)
/api/v1/admin/sources/{id}/sync                — POST (admin only; shares the sync lock with /admin/sync)
/api/v1/admin/sources/discover                 — POST (admin only, API-only - no frontend page)
/api/v1/admin/sources/discovered               — GET (admin only, API-only)
/api/v1/admin/sources/discovered/{id}/approve  — POST (admin only, API-only)
/api/v1/admin/sources/discovered/{id}/reject   — POST (admin only, API-only)
/api/v1/admin/documents/                       — GET (admin only)
/api/v1/admin/documents/{id}                   — GET (admin only)
/api/v1/admin/documents/{id}/versions          — GET (admin only)
/api/v1/admin/documents/{id}/chunks            — GET (admin only)
/api/v1/admin/documents/{id}/diff              — GET (admin only)
/api/v1/admin/runs/{id}                        — GET (admin only; status polling only)
/api/v1/admin/scheduler/                       — GET, PUT (admin only)
/api/v1/admin/applicants/                      — GET (admin only, read-only, cross-user)
/api/v1/admin/applicants/{id}                  — GET (admin only, read-only, cross-user)
/api/v1/admin/sync                             — POST (admin only; all active sources)
```

**Not-evaluable activation guard**: single and batch activation accept
`{"confirm_not_evaluable": "ACTIVATE"}`. Without it, activating a rule whose field has no
applicant counterpart returns 422 with the field name and the reason. The admin UI requires
the word to be typed, so the override is deliberate rather than a default.

**Route removal (admin-frontend build-out)**: `GET /api/v1/admin/schema` (dev/introspection,
backed by `db/schema_info.py`) and `GET /api/v1/admin/knowledge-state` (unused by any test or
by the new frontend) were deleted outright, along with their dedicated test files.
`GET /api/v1/admin/runs` (list), `/admin/runs/summary`, and `/admin/runs/source/{id}` were
also removed — the admin frontend deliberately has no pipeline-runs/extraction-runs page
(only a non-blocking "sync in progress" banner backed by `GET /admin/runs/{run_id}`, which is
kept). Source auto-discovery (`/admin/sources/discover` + `/discovered/*`) is a real, tested
feature that stays — it's simply not wired into a frontend page in this build. All 7
previously-duplicated local `_require_admin()` helpers across the admin route files were
replaced with the single shared `Depends(require_admin)` from `api/routes/auth.py`.

### Error Responses

```json
{"detail": "User already exists"}       // 409 duplicate email
{"detail": "Not authenticated"}         // 401 no token
{"detail": "Not an admin"}              // 403 non-admin on admin route
{"detail": "Rule not found"}            // 404
{"detail": "Invalid status transition"} // 422 bad activation
```

---

## 7. Authentication

### JWT Flow

```
POST /register  →  bcrypt(password) → UserORM → 201
POST /login     →  verify(password) → jwt.encode({sub: user_id, role}) → 200
GET /me         →  Authorization: Bearer <token>
                    jwt.decode(token) → UserORM → 200
```

### Secret Management

```python
def _get_jwt_secret() -> str:
    """Fail-fast in production if JWT_SECRET_KEY not set."""
    secret = os.getenv("JWT_SECRET_KEY")
    if not secret:
        environment = os.getenv("ENVIRONMENT", "development")
        if environment == "production":
            raise ValueError(
                "JWT_SECRET_KEY environment variable is required in production. "
                "Set a strong, random secret in your environment."
            )
        logger.warning("JWT_SECRET_KEY not set, using development default")
        return "dev-secret-change-me"
    return secret
```

### Password Hashing

- bcrypt 4.2.1 (pinned to avoid passlib incompatibility)
- `passlib.context.CryptContext(schemes=["bcrypt"])`
- Duplicate email → 409 Conflict

---

## 8. Database

### ORM Models (33 tables at head `c2d3e4f5a6b7`; see `DBSTRUCTURE.md`)

```
countries
immigration_programs
government_sources          (NOT NULL visa_category; extract_requirements scope flag)
government_documents        (NOT NULL visa_category, raw_path, retrieved_at)
document_versions           (append-only)
document_chunks             (NOT NULL version_id FK; embedding vector(1536) when pgvector)
fetch_log
host_cooldowns              (host PK; cooldown_until)
discovered_sources

requirements                (NOT NULL status, extracted_at; concept)
requirement_evidence
requirement_versions        (append-only)
requirement_references
extraction_runs
extraction_cache
requirement_review

rules
rule_versions               (append-only; UNIQUE rule_id + version_number)
rulesets
ruleset_rules               (append-only)

applicant_profiles          (incl. the six Phase 4a concept fields)
assessments                 (append-only)
assessment_rule_results     (append-only)
gaps

audit_log
llm_usage
llm_budget_log
scheduler_settings

pipeline_runs
```

`document_chunks.embedding` is pgvector's `vector(1536)` with an HNSW cosine index; it is
`NULL` until an embedding backfill runs. `scripts/db_structure.py` regenerates
`DBSTRUCTURE.md` from the live database after a migration.

### Database Abstraction

```python
# db/connection.py
get_engine(url) → Engine
get_session_factory(engine) → sessionmaker
session_scope(session_factory) → contextmanager
init_db(engine) → create_all()

# db/repositories.py — 20+ repository classes
# Each wraps a single table with typed CRUD operations
```

### PostgreSQL vs SQLite

| Feature | PostgreSQL (production) | SQLite (tests) |
|---------|------------------------|----------------|
| UUIDs | Native UUID type | `String(36)` |
| JSON | JSONB | JSON |
| Timestamps | TIMESTAMPTZ | DATETIME |
| Pool | StaticPool + async | StaticPool + check_same_thread=False |
| Migrations | Alembic | Manual |

### Indexes

```sql
uq_rule_version            ON rule_versions(rule_id, version_number)
uq_rules_active_per_field  UNIQUE (program_id, category, field) WHERE status = 'ACTIVE'
uq_doc_version             ON document_versions(document_id, version_number)
uq_doc_ver_content         ON document_versions(document_id, content_hash)
idx_chunks_document        ON document_chunks(document_id, version_id)
idx_chunks_embedding_hnsw  USING hnsw (embedding vector_cosine_ops)
idx_fetchlog_source        ON fetch_log(source_id, fetched_at)
```

The partial unique index is the database-level version of the one-active-rule
invariant: a second ACTIVE row for the same (program, category, field) is rejected even
if application code is bypassed.

---

## 8a. Search and embeddings

`src/services/search_service.py` provides two retrieval functions and
`src/api/routes/search.py` exposes them together:

- **Keyword** - SQL `ILIKE` over chunk text, always available, ordered deterministically.
- **Semantic** - cosine distance over `document_chunks.embedding` using pgvector's `<=>`
  operator and the HNSW index, optionally filtered by program or document.
- **auto** (default) - semantic when `EMBEDDINGS_ENABLED=true`, pgvector is present and at
  least one chunk is embedded; otherwise keyword. The response's `mode` says which ran,
  plus `semantic_available` and `embedded_chunks`, so the UI never claims meaning-based
  matching when it fell back.
- Requesting `mode=semantic` when it cannot run is a 409 with a specific reason, not a
  silent downgrade.

Embeddings are off by default. The mock embedding client exists only for offline tests and
must be named explicitly (`EMBEDDING_MODEL=mock-embedding-v1`); there is no silent fallback.
Backfill with `$env:PYTHONPATH="src"; python -m src.extraction.embeddings --backfill`.

Every hit is decorated with its citation (document, version, chunk, page/section heading),
so search results are as traceable as rule evidence.

---

## 9. LLM Governance

### Components

```
RateLimiter        — Per-model request throttling (sliding window)
RetryPolicy        — Exponential backoff with max delay cap
ResponseCache      — SHA256-keyed LRU cache with TTL
BudgetTracker      — Daily/monthly token budget limits
UsageLog           — Per-request cost tracking
LLMGovernance      — Facade orchestrating all components
```

### Cache Key Design

```python
cache_key = SHA256(
    content_hash + model_name + temperature + system_prompt_hash
)
```

### Usage Tracking

```python
request_id       → str (UUID)
model            → str ("gpt-4o")
input_tokens     → int
output_tokens    → int
estimated_cost   → float
cached           → bool
latency_ms       → int
created_at       → datetime
```

---

## 10. Structured Logging

### Features

- JSON output format for log aggregation
- Request-scoped context (request_id, user_id)
- PII redaction: date of birth, passport, email never logged
- Log levels: DEBUG, INFO, WARNING, ERROR, CRITICAL

### PII Filters

```python
# Patterns automatically redacted from log output:
# - Date of birth (various formats)
# - Passport numbers
# - Email addresses
# - User IDs in sensitive contexts
```

---

## 11. Docker Deployment

**Updated 2026-10-27**: one Playwright/Chromium path (headless), a durable volume for raw
snapshots, a pgvector-enabled database image, an unprivileged API image, and a separate
unprivileged nginx image for the admin UI. `docker-compose.prod.yml` now describes a TLS
topology, but no target host has been provisioned or exercised.

### Dockerfile

```dockerfile
FROM python:3.12-slim
# Installs requirements, copies src/, runs uvicorn as fixed UID/GID 10001 (app).
# Bakes the browser with `playwright install --with-deps chromium`; the browser is
# headless, so there is no Xvfb/xauth/DISPLAY. PLAYWRIGHT_BROWSERS_PATH is fixed so
# the path does not depend on the container user's HOME.
# CMD: uvicorn src.api.main:create_app --factory --host 0.0.0.0 --port 8000
```

`frontend/Dockerfile` is a two-stage build: Node 22 runs `npm ci` and `npm run build`, then
unprivileged nginx serves `dist/` on port 8080 and proxies `/api` to the API on the
same origin (no CORS, no localhost baked into the bundle). It is built by CI, and the
production compose file adds Caddy TLS in front of it; no target host has been run yet.

### docker-compose.yml

```yaml
services:
  postgres:
    image: pgvector/pgvector:pg16       # vector extension for semantic search
    environment:
      POSTGRES_DB: immigration
      POSTGRES_USER: immigration
      POSTGRES_PASSWORD: immigration_dev
    ports:
      - "${POSTGRES_HOST_PORT:-5432}:5432"
    volumes:
      - pgdata:/var/lib/postgresql/data
  api:
    build: .
    depends_on: [postgres]
    volumes:
      - raw_snapshots:/app/raw   # added 2026-10-05 — without this, every container
                                 # recreation silently lost previously-fetched raw files
    environment:
      # Driver named explicitly (postgresql+psycopg2://) so the resolved driver doesn't
      # depend on the installed SQLAlchemy version's default dialect (fixed 2026-10-01).
      DATABASE_URL: postgresql+psycopg2://immigration:immigration_dev@postgres:5432/immigration
      JWT_SECRET_KEY: ${JWT_SECRET_KEY}
      CORS_ORIGINS: ${CORS_ORIGINS}
    command: >
      sh -c "alembic upgrade head && python src/seed_registry.py &&
      uvicorn src.api.main:create_app --factory --host 0.0.0.0 --port 8000"
volumes:
  pgdata:
  raw_snapshots:
```

---

## 12. Data Model Summary

### Core Entity Chain

```
Government Source
    → Government Document
        → Document Version
            → Document Chunk
                → Requirement (+ Evidence)
                    → Rule Version (snapshot_json)
                        → Expression Tree
                            → Eligibility Assessment
                                → ELIGIBLE / INELIGIBLE / INCOMPLETE
                                    → Gap Engine → Questions
```

### Phase 1 Tables

`countries` `immigration_programs` `government_sources` `government_documents` `document_versions` `document_chunks` `fetch_log`

### Phase 2 Tables

`requirements` `requirement_evidence` `requirement_versions` `requirement_references` `extraction_runs` `requirement_review`

### Phase 3D Tables

`rules` `rule_versions` (with `snapshot_json` containing full compiled rule + evidence text)

### Phase 3C Models (in-memory)

`ApplicantProfile` `VisaSelection` `Rule` `Provenance` `RuleEvaluation` `EligibilityOutcome` `Gap` `QuestionDefinition`

---

## 13. Test Inventory

**Current totals (2026-10-27)**: 1,695 passed / 65 skipped in the hermetic backend suite;
56 passed against PostgreSQL + pgvector; 42 frontend unit tests; 5 Playwright/axe tests.
The per-file tables below are a 2026-09-15 snapshot kept for orientation; the counts that
follow the "Current collection counts" heading are exact.

### Current collection counts

| Suite | Tests |
|---|---|
| `tests/rules/` | 245 |
| `tests/rule_review/` | 124 |
| `tests/assessment/` | 115 |
| `tests/extraction/` | 119 |
| `tests/db/` | 28 |
| `tests/e2e/` | 27 |
| `tests/services/` | 23 |
| `test_politeness.py` | 46 |
| `test_host_cooldown.py` | 53 |
| `test_playwright_client.py` | 49 |
| `test_browser_fetcher.py` | 39 |
| `test_security.py` | 44 |
| `test_scheduler.py` | 38 |
| `test_proxy.py` | 33 |
| `test_llm_governance.py` | 32 |
| `test_api.py` | 41 |
| `test_logging.py` | 17 |
| `test_e2e_scenarios.py` | 14 |
| `test_auth.py` | 13 |
| `test_search_api.py` | 12 |

`tests/rule_review/` is the renamed `tests/phase3d/` (the phase label was not a subject).
The 65 hermetic skips are PostgreSQL-gated or explicit live opt-ins (`POSTGRES_E2E`,
`PYTEST_LLM_LIVE`, `BROWSER_LIVE`); no live test is silently faked.

### Phase 3 Core (snapshot: 192 tests)

| File | Tests | Coverage |
|------|-------|----------|
| test_models.py | 8 | Rule/Provenance serialization, status values |
| test_operators.py | 27 | All 12 operators: PASS + FAIL + edge cases |
| test_rule_compiler.py | 8 | Phase 2 → Phase 3 compilation, rejection |
| test_rule_engine.py | 18 | Single rule evaluation, aggregate status |
| test_expressions.py | 33 | AND/OR/NOR truth tables, nesting |
| test_integration.py | 10 | Compile → evaluate → explain end-to-end |
| test_conditions.py | 31 | IF/THEN truth table, NOT_APPLICABLE in AND/OR |
| test_exceptions.py | 17 | Exception truth table, nesting, real cases |
| test_dependencies.py | 17 | Dependency truth table, chaining |
| test_versioning.py | 21 | Version selection, historical reproducibility |

### Phase 3C Eligibility (97 tests)

| File | Tests | Coverage |
|------|-------|----------|
| test_applicant.py | 22 | Profile construction, serialization, to_rule_data |
| test_applicability.py | 16 | Program/status/version filtering |
| test_assessment.py | 18 | ELIGIBLE/INELIGIBLE/INCOMPLETE, traces, counts |
| test_gap_engine.py | 14 | Gap detection, dedup, priority, linkage |
| test_questions.py | 27 | Question definitions, validation, normalization |

### Rule review / activation (`tests/rule_review/`, 124 tests)

| File | Coverage |
|------|----------|
| test_rule_activation.py | Status machine, illegal transitions |
| test_rule_repository.py | EnhancedRuleRepo CRUD, versioning, activation |
| test_requirement_to_rule.py | Requirement→rule bridge, compile_approved_requirements |
| test_admin_rules_api.py | Admin API endpoints, auth, validation, activate |
| test_e2e_pipeline.py | Full pipeline: v1→ACTIVE→v2→SUPERSEDED |
| test_e2e_pipeline_lifecycle.py | Extended lifecycle scenarios |
| test_rule_review_hardening.py | One-active invariant, transactional activation, provenance, retirement, failure safety, compilation safety |

### Phase 2 (61 tests)

| File | Tests | Coverage |
|------|-------|----------|
| test_schema.py | 11 | JSON Schema validation, logic rules |
| test_evidence.py | 8 | Exact substring verification, hallucination detection |
| test_normalization.py | 8 | Operator/unit/category normalization |
| test_deduplication.py | 8 | Semantic dedup, conflict detection |
| test_extraction.py | 9 | LLM contract tests, mock responses |
| test_integration.py | 4 | Full pipeline integration |
| test_gold_dataset.py | 3 | Gold standard evaluation |
| test_category_prompts.py | 10 | Deterministic visa_category → prompt selection (Phase 4) |
| test_category_gold_harness.py | 12 | Category-aware gold integrity: zero cross-category contamination, no fabricated evidence (Phase 3) |

### Infrastructure

| File | Tests | Coverage |
|------|-------|----------|
| test_db_models.py | 12 | ORM model constraints, relationships |
| test_repositories.py | 10 | Repository CRUD, query filters |
| test_logging.py | 17 | Structured logging, PII redaction |
| test_api.py | 38 | API routes: health, applicants, assessments (real), middleware, G9 regression, G12 FK fixes, admin-sync auth (401/403/200) |
| test_auth.py | 13 | JWT, register/login, bcrypt, duplicate email, role in token |
| test_llm_governance.py | 30 | Rate limiter, cache, budget, retry |
| test_security.py | 44 | JWT secrets, PII redaction, CORS, authorization, password security, token claims |
| test_scheduler.py | 28 | APScheduler start/stop, manual sync trigger, concurrent-sync lock |
| test_pipeline_runs.py | 42 | PipelineRun model, repo CRUD, API endpoints, computed counters, orchestrator integration |
| test_e2e_scenarios.py | 14 | Multi-program, multi-version, gap engine, evaluation, orchestration |

### Total: 1,695 passed / 65 skipped (2026-10-27)

(The per-file tables above are a representative 2026-09-15 snapshot — several files have
grown since. The exact current counts are under "Current collection counts" above and in the
README test suite summary.)

---

## 14. Key Files

### Phase 3

- `src/rules/models.py` — Rule, Provenance, RuleEvaluation, EligibilityResult
- `src/rules/operators.py` — 12 declarative operators
- `src/rules/expressions.py` — 7 expression types, recursive evaluator
- `src/rules/rule_compiler.py` — Phase 2 requirement → Phase 3 rule
- `src/rules/rule_engine.py` — Single rule + aggregate evaluation
- `src/rules/explanations.py` — Human-readable evaluation explanations
- `src/rules/rule_activation.py` — Status machine (DRAFT→ACTIVE lifecycle)
- `src/rules/rule_repository.py` — EnhancedRuleRepo (CRUD, versioning, activation)
- `src/rules/requirement_to_rule.py` — Approved requirement → rule compilation bridge

### Phase 3C

- `src/assessment/applicant.py` — ApplicantProfile, VisaSelection
- `src/assessment/applicability.py` — Program/status/version filtering
- `src/assessment/assessment.py` — ELIGIBLE/INELIGIBLE/INCOMPLETE mapping
- `src/assessment/gap_engine.py` — Missing field detection + prioritization
- `src/assessment/questions.py` — Deterministic question templates + validation

### API & Auth

- `src/api/main.py` — FastAPI app factory, CORS, middleware, lifespan (TESTING guard)
- `src/api/helpers.py` — `parse_uuid_or_404()` utility
- `src/api/routes/auth.py` — Register/login/me
- `src/api/routes/applicants.py` — Applicant CRUD (JWT-protected)
- `src/api/routes/assessments.py` — Assessment create/list/evaluate
- `src/api/routes/admin_rules.py` — Admin review/approve/activate
- `src/api/routes/admin_requirements.py` — Admin requirement review (ORM-based)
- `src/api/routes/admin_sources.py` — Source admin API
- `src/api/routes/admin_documents.py` — Document admin API: versions, chunks, diffs
- `src/api/routes/search.py` — Keyword/semantic/auto search over stored chunks
- `src/api/routes/pipeline_runs.py` — Pipeline run/background-job status polling
- `src/services/auth_service.py` — JWT, bcrypt, secret management
- `src/services/search_service.py` — keyword SQL + pgvector cosine search, citations
- `src/extraction/embeddings.py` — embedding clients, backfill CLI, pgvector storage
- `src/rules/rule_checker.py` — dry run against a hypothetical profile (no persistence)
- `frontend/src/pages/RulesPage.jsx` + `components/RuleTestPanel.jsx` — review, test, activate
- `frontend/e2e/admin.spec.js` — Playwright flows + axe checks

### Infrastructure

- `src/db/models.py` — 33 ORM models
- `src/db/connection.py` — Engine, session factory, session_scope
- `src/db/repositories.py` — 20+ repository classes
- `src/llm_governance.py` — RateLimiter, RetryPolicy, ResponseCache, BudgetTracker
- `src/logging_config.py` — Structured logging, PII redaction
- `src/orchestration/government_sync.py` — Source sync orchestrator (on-demand)
- `src/fetch/politeness.py` — Per-host spacing with jitter (shared limiter)
- `src/orchestration/host_cooldown.py` — Refusal (403/429) host pauses
- `src/fetch/proxy.py` — Optional single static egress proxy (fail-closed, redacted)

### Configuration

- `.env.example` — CORS_ORIGINS, JWT_SECRET_KEY, all config placeholders
- `alembic.ini` — Alembic migration config
- `Dockerfile` — Python 3.12-slim + Playwright Chromium + uvicorn
- `frontend/Dockerfile` — Node build + nginx same-origin admin UI
- `docker-compose.yml` — pgvector/pgvector:pg16 + api service
- `requirements.txt` — bcrypt==4.2.1 pinned; Playwright pinned major
- `conftest.py` — Root conftest, adds `src/` to sys.path
- `scripts/db_structure.py` — regenerates `DBSTRUCTURE.md` from the live database
- `scripts/live_fetch_check.py` — one read-only live fetch for the human gate

### Baselines

- `validation/run_report.json` — Phase 1 pipeline results (gitignored, regenerated on each run)

---

## 15. Design Principles

1. **Declarative rules only**: No lambdas, no dynamic code execution. Rules are data. Engine interprets generically.

2. **Evidence is verbatim**: Every extracted requirement must have evidence that is an exact substring of its source chunk.

3. **Missing = UNKNOWN, not FAIL**: `None` fields produce `UNKNOWN`, preserving the distinction between "we don't know" and "rule failed."

4. **No LLM in eligibility**: LLMs extract knowledge (Phase 2). Rules decide eligibility (Phase 3). RAG explains rules (future). None overlap.

5. **Historical integrity**: Rule v1 assessments stay v1. Government changes create v2. Historical users are never evaluated against current rules.

6. **Provenance preserved**: Every rule traces back through: requirement → chunk → document version → document → source → government URL.

7. **Four-state is necessary**: PASS/FAIL is insufficient. UNKNOWN ("we don't know your points") must not become FAIL ("you don't have enough points"). NOT_APPLICABLE ("189 rules don't apply to 190 applicants") must not pollute AND/OR.

8. **Gap priority from metadata**: CRITICAL/HIGH/MEDIUM/LOW comes from rule `critical`/`mandatory` flags, not arbitrary UI logic.

9. **Transactional activation**: Activating v2 atomically supersedes v1 for the same logical key. Never two ACTIVE for same field.

10. **Fail-fast in production**: JWT secret must be set. CORS wildcard is blocked. Database errors surface immediately.

11. **Polite, non-evasive retrieval**: Egress discipline changes *when* we ask, never *how we identify ourselves*. Per-host spacing with jitter, honouring `Crawl-delay`/`Retry-After`, and pausing a host that refused us are the whole toolkit. No User-Agent rotation, no IP/proxy rotation, no CAPTCHA bypass, no stealth headers; `FETCH_PROXY_URL` is one static egress, not a rotation.

---

## 17. Retrieval Politeness & Access Discipline

**Motivation.** The 2026-10-05 finding (§16) was that `immi.homeaffairs.gov.au`'s WAF degrades under
**per-URL request volume**: a full 14-source sync could return 0/14 failures, while a single URL hit
repeatedly minutes later progressively degraded (200 → corrupted content → 403), with every
other URL left healthy. The measured baseline was ~10 requests to one host in ~3.4s (~3 req/s), with
no spacing, no jitter and no reaction to a refusal. The correct response is ordinary client-side
etiquette — not evasion.

**Spacing with jitter** (`src/fetch/politeness.py`). A single process-wide `HostLimiter` is keyed by
`host[:port]`. Every request to the same host waits at least `FETCH_MIN_INTERVAL_SECONDS` (8) plus a
random `uniform(0, FETCH_JITTER_SECONDS)` (4), so the pattern is not metronomic. Different hosts do
not delay each other. Both retrieval paths (`fetcher.fetch`, `browser_fetcher.fetch_with_browser`)
share the one limiter, and a `robots.txt` `Crawl-delay` / `Retry-After` tightens the interval further.
Clock, sleep and RNG are injectable so tests run on a virtual clock and never sleep.

**Refusal cooldown** (`src/orchestration/host_cooldown.py`). Only **403/429** are refusals; a timeout,
DNS failure or 5xx is a *fault* and never pauses anything. A refusal upserts a `host_cooldowns` row
(one per host, `cooldown_until = now + FETCH_COOLDOWN_HOURS`, default 0.25 hours / 15 minutes) and every source on that host
is skipped until it expires — by a full sync, by a single-source sync, and surfaced to the admin UI
("Paused until" column, `409 host_cooldown` on manual run). A successful fetch clears the row. The
reaction to a refusal is **pause only**: the IP, the User-Agent and the HTTP-vs-browser choice are
byte-for-byte unchanged, and there is no override flag.

**Optional static egress** (`src/fetch/proxy.py`). `FETCH_PROXY_URL` (default unset = direct) routes
every fetch through **one fixed** proxy for reachability/egress control. It is never rotated and never
changes in response to a refusal. It **fails closed**: a malformed value makes affected fetches return
`NETWORK_RESTRICTED` + `error_type=proxy_config_error` rather than fall back to a direct connection,
and `user:password@` credentials are redacted from logs and error strings.

**Cost.** With defaults, same-host requests are ~`8 + uniform(0,4)`s apart (first request immediate),
so 10 sources on one host take ~`72s` min / `~90s` average / `108s` max, versus the ~`3.4s` pre-change
baseline. A refusal makes a run shorter, not longer, by skipping the rest of that host. See the README
"Fetch politeness guard" section for the table. The trade is intentional: a run that stays welcome is
worth far more than one that is fast and gets the IP blocked.



## 16. Production Readiness Assessment

### Strengths

- 1,695 hermetic tests passing / 65 skipped, plus 56 PostgreSQL and 47 frontend tests, all green
- CI gates every push/PR on backend, PostgreSQL e2e, frontend unit/browser/axe and image builds
- Strong domain separation (LLM extraction vs deterministic rules)
- Complete provenance chain from rule to government URL
- Structured logging with PII redaction
- JWT authentication with production secret fail-fast
- Versioned rule lifecycle with transactional activation
- Docker deployment with PostgreSQL
- LLM governance (rate limiting, caching, budget tracking)
- Real assessment API (ACTIVE rules → Phase 3C → persist)
- Automatic source sync scheduler (APScheduler)
- 40 security tests (auth, PII, CORS, passwords, authorization)
- PipelineRun audit trail for every sync execution (42 tests with computed counters)
- Admin documents API (versions, chunks, diffs)
- Semantic + keyword search with honest mode reporting and citations
- PipelineRun computed counters (rules validated/approved/activated at query time)
- Lifespan TESTING mode guard (G9 fix)
- **Real PostgreSQL ORM verification** (FK constraints, NOT NULL, full rule lifecycle)
- **Deterministic visa_category → prompt selection** in Phase 2 extraction (10 tests)
- **Category-aware gold scaffolding + integrity harness** (12 tests): per-category gold dirs under `data/gold/{skilled,work,student,visitor}/`, zero cross-category prompt contamination, no fabricated evidence
- Rule dry run (`/admin/rules/evaluate`) and a not-evaluable activation guard with typed confirmation
- Admin UI now covers the review workflow (requirement queue, rule test/approve/activate, document search/diff) with a Playwright + axe suite
- PostgreSQL e2e proves the real HTTP flow (auth → review → compile → activate → assess), including access control and the not-evaluable guard

### Gaps

| Gap | Priority | Status | Description |
|-----|----------|--------|-------------|
| ~~Assessment API stubbed~~ | P0 | DONE | `POST /assessments` and `POST /assessments/evaluate` now real |
| ~~No scheduler~~ | P1 | DONE | APScheduler with configurable interval, manual trigger endpoint |
| ~~PostgreSQL verification~~ | P1 | DONE | ORM rule lifecycle + FK/NOT NULL verified against real PostgreSQL 2026-08-27 |
| ~~No PipelineRun tracking~~ | P2 | DONE | Full audit trail: model, repo, API, orchestrator integration |
| ~~No security test suite~~ | P2 | DONE | 44 tests covering auth, PII, CORS, passwords, authorization |
| ~~Admin documents API~~ | P2 | DONE | Document versions, chunks, diffs for admin visibility |
| ~~Admin knowledge-state endpoint~~ | P2 | REMOVED | It was a hierarchical browsing tree; the admin UI now calls the specific source/document/requirement endpoints instead, so the aggregate route was deleted rather than kept as dead API surface. |
| ~~G9 lifespan testing bug~~ | P2 | DONE | TESTING env var guard prevents DB init in test mode |
| ~~G10 UUID validation~~ | P2 | DONE | parse_uuid_or_404 helper on all UUID-based routes |
| ~~G13 computed PipelineRun counters~~ | P2 | DONE | rules_validated/approved/activated computed at query time |
| ~~G16/Phase 4 category prompts~~ | P2 | DONE | run_extraction.py derives visa_category → category prompt |
| ~~Admin-only enforcement on every admin endpoint~~ | P1 | DONE | 2026-09-15: `POST /api/v1/admin/sync` was unauthenticated — now gated via shared `require_admin`; all admin routes audited |
| ~~Playwright collection crash~~ | P1 | DONE | Browser script lives in `scripts/`, so pytest never launches a browser at collection. The single production driver is Playwright/headless; the legacy Patchright path is opt-in only until the human gate deletes it. |
| WORK/STUDENT/VISITOR sources | P3 | DONE | 2026-09-15: registry extended to 14 sources across all four categories (482/500/600 mains + occupation list + CRICOS) |
| Live Home Affairs fetch | P1 | **PARTIAL, human-gated** | The WAF blocks intermittently under per-URL request volume, independent of headless vs headed. One full sync once got 0/14 failures, while repeated direct hits to one URL degraded to 403. The response is politeness (spacing, cooldown) and honest failure recording, never evasion. Re-confirmation needs the operator gate, not automation. |
| ~~Auth temporarily bypassed for manual testing~~ | P0 | DONE | Restored 2026-09-22; bypass fully deleted, not just toggled off |
| ~~Phase2→Phase3C field-naming mismatch~~ | **P0** | DONE | 2026-09-25: found via the first real E2E run on Postgres — every real extracted rule would have evaluated UNKNOWN forever regardless of applicant data (dotted extraction-contract field names vs. `ApplicantProfile.to_rule_data()`'s flat names, exact-match dict lookup with zero normalization). Fixed in `rule_engine.py`. |
| ~~Compiler rejects REQUIRED/NOT_REQUIRED operators~~ | P1 | DONE | 2026-09-25: `compile_requirement_to_rule` raised on any requirement using REQUIRED (health/character/skills-assessment/values-statement criteria — common in real government text). Now translated to EQ/True or EQ/False at compile time. |
| ~~Ordinal string comparison is lexicographic~~ | P2 | DONE | 2026-09-25: `"vocational" >= "competent"` wrongly evaluated `True` (raw Python string comparison). Fixed with an explicit ordinal-scale lookup in `operators.py`. |
| ~~Zero-chunk extraction silently reports COMPLETED~~ | P2 | DONE | 2026-09-25: a CHANGED/INITIAL fetch yielding 0 chunks now records a visible `PARTIAL` PipelineRun warning instead of an indistinguishable-from-success `COMPLETED`. |
| `SRC-AU-MIGRATION-REGS-1994` wrong URL | P2 | DONE | 2026-09-25: fixed to a verified-real PDF deep-link (validated during source inspection; the one-off registry report was retired); **re-confirmed live 2026-10-05** — real sync extracted 13 real requirements |
| `SRC-AU-CRICOS-REGISTER` yields 0 chunks | P2 | OPEN | Confirmed (2026-09-25) a genuine search-interface SPA, not fixable via render-wait; needs re-sourcing |
| ~~Raw immutable snapshot persistence~~ | P1 | **DONE (2026-10-05)** | `docker-compose.yml` had no volume for `/app/raw` — every container recreation silently lost previously-fetched raw files while `government_documents.raw_path` kept pointing at now-missing paths. Added a `raw_snapshots` named volume; verified a fetch's file survives a recreate with its hash matching the DB. |
| ~~Fresh Docker build can't reach PostgreSQL~~ | P0 | **DONE (2026-10-01)** | `DATABASE_URL` now names `postgresql+psycopg2://` explicitly instead of a bare `postgresql://`. The earlier host-venv-only psycopg v3 failures are gone: the suite at that time was 1,587 passed / 65 skipped with zero failures. |
| ~~`requirement_id`/`rule_id` truncation on PostgreSQL~~ | **P0, new** | **DONE (2026-10-05)** | `String(48)` silently dropped rows for long `program_id`s on Postgres (`StringDataRightTruncation`) — SQLite never caught it since it doesn't enforce VARCHAR length. Widened to `String(96)` with a fail-fast length guard (migration `a3c5e7f9b1d3`). |
| `SRC-AU-600-MAIN` (and 6 other `immi.homeaffairs.gov.au` pages) yield 0 chunks | **P1** | **Partially done — two bugs, not one** | (1) DONE: content sat inside a legacy ASP.NET `<form>` that `extractor_html.py` deleted wholesale as noise — fixed by unwrapping, not decomposing, `<form>` tags. (2) Found the same day, bigger: a full-registry sync still showed 7/14 sources at 0 chunks on a genuinely successful fetch — these Angular pages render real content from a hidden-input JSON blob client-side, *after* `domcontentloaded`, and `browser_fetcher.py` was capturing too early. Fixed with a bounded wait for a real structural selector; unit-tested against the real captured shape; **live reconfirmation blocked** by the WAF (see row above) as of this writing. |
| ~~`uq_rules_active_per_field` narrower than its own invariant~~ | **P1, new** | **DONE (2026-10-05)** | Covered only `(program_id, field)`; `activate_rule`'s own logical key is `(program_id, category, field)`. A legitimate same-field-different-category ACTIVE rule was incorrectly rejected. Widened via migration `7c9e2a4f1b6d`. |
| **NEW**: Gemini schema-adherence gap on `SRC-AU-600-MAIN` content | P2 | OPEN | Found 2026-10-05 only because the 0-chunk bug above is fixed: all 16 candidate chunks from this page return schema-violating JSON (wrong keys, invalid enums) — 0 requirements persisted, vs. 13/13 and 2/2 success on other real sources with the same model. Not investigated further. |
| Real WORK/STUDENT/VISITOR gold data | P3 | OPEN | Gold dirs scaffolded with zero-item AWAITING_REAL_SOURCE placeholders (no fabricated evidence); skilled/ populated with real verbatim gold |
| Multi-country deferred | P3 | OPEN | Australia only, NZ/Canada/etc. later |
| 491 real-world coverage | P3 | OPEN | Partial real scenario validation |
| Applicant-facing frontend | P2 | OPEN | The internal admin console (`frontend/`) now covers sources, review, rules (test/activate), documents/search, applicants and scheduler. The applicant-facing product UI is still unstarted. |
| Supersession with real government data | P1 | OPEN | Works in the PostgreSQL e2e (v1 ACTIVE → v2 activates → v1 SUPERSEDED → historical assessment untouched), but no second real version of a real source has been processed yet. |
| Deployment target (TLS, orchestration) | P2 | **PARTIAL** | `docker-compose.prod.yml` + Caddy define TLS, private networks, no published API/DB ports and unprivileged containers. No target host has been provisioned; DNS, certificates, backups/restores, monitoring and rollback are unverified. |

### Verification performed (2026-08-27; re-verified 2026-09-15, 2026-09-25, 2026-10-05, 2026-10-23, 2026-10-27)

1. **1,695 passed / 65 skipped, 0 failed** (2026-10-27) in the hermetic backend suite, plus
   **56 passed** against PostgreSQL 16 + pgvector, **42** frontend unit tests and **5**
   Playwright/axe tests. The suite is hermetic: it makes no live network call, launches no
   browser and needs no database. The skips are explicit PostgreSQL/live opt-ins.
2. **Real PostgreSQL**: `docker compose up -d postgres` + `alembic upgrade head`, then full ORM pipeline Country → Program → Source → Document → Version → Chunk → Requirement → Rule (DRAFT → VALIDATED → APPROVED → ACTIVE). FK constraints and NOT NULL enforced correctly. **2026-10-05: the full chain was run for real, not just the E2E test fixture** — `SKILLED_189` carries a real ACTIVE rule compiled from real extracted requirements, and a real applicant's assessment against it produced a real `ruleset_hash`/`rule_version` with correct PASS/UNKNOWN/gap output. The dev database currently holds 14 sources, 3 documents, 653 requirements, 2 rules (1 ACTIVE / 1 DRAFT) and 775 chunks.
3. **Live fetch**: refusals are classified, recorded and the source demoted; there is no retry
   with a different identity. The honest position after repeated live observation is that the
   Home Affairs WAF blocks **intermittently under per-URL request volume**, independent of
   headless vs headed. The fix is politeness (spacing, cooldown) and an honest failure record,
   not evasion. Re-running the live gate is a human decision (`scripts/live_fetch_check.py`).
4. **CLI Phase 1/2**: offline pipeline + extraction run_extraction against DOC-AU-189-MAIN extract 6 validated requirements with category-aware prompt wiring.
5. **Category-aware gold harness**: per-category dirs created; skilled gold (189 + 190, 29 items) migrated; work/student/visitor zero-item placeholders; integrity report clean (no contract violations, no cross-category leak).
6. **Folder structure** (2026-10-05): `phase2`/`phase3`/`phase3c` renamed to `extraction`/`rules`/`assessment`; fetch-related modules grouped under `src/fetch/`; `change_detector.py`/`hashing.py` grouped under `src/change_detection/`. One rename at a time, full suite re-run after each, same pass/fail/skip baseline held throughout.
7. **Admin console** (2026-10-23): `frontend/` (plain React + Vite) covers the dashboard,
   sources (run now / cooldown), requirement review, rules (dry run, typed activation),
   document search/versions/diff, applicants and scheduler. Verified by Vitest, Playwright
   against a mocked API, and axe (no serious/critical violations).
8. **PostgreSQL e2e** (2026-10-23): a dedicated `immigration_e2e` database exercises the HTTP
   flow end to end — register/login, review, compile, activation guard, supersession,
   assessment — with a guard that refuses to run against the shared `immigration` database.
9. **CI** (2026-10-23): four jobs (hermetic backend, PostgreSQL + migrations + e2e, frontend
   unit/build/Playwright/axe, both images) run on every push/PR; Dependabot groups weekly
   pip/npm/actions updates.
10. **Container builds** (2026-10-23): both the API image and the nginx UI image build
    locally and in CI.
11. **Production hardening** (2026-10-27): the API image runs as UID/GID `10001` and
    launches Chromium headlessly; the admin image runs as UID `101` and proxies
    `/health` + `/ready` to the API; a fresh named volume inherits app ownership; unsafe
    production env values abort startup; `docker-compose.prod.yml` renders with no
    published API/DB/frontend ports; and `caddy validate` accepts the Caddyfile.
    Migration `d3e4f5a6b7c8`/`e5f6a7b8c9d0` downgrade and re-upgrade cleanly on
    PostgreSQL. No target host, DNS, certificate, backup or monitoring test has been
    performed.

### Next Milestones

1. **Run the human gates** in README ("Before production"): live fetch hash, one Home Affairs
   source, the never-synced sources, SKILLED_189 rule activation, secret rotation
2. **Prove rule supersession on real data** — the one remaining Definition-of-Done box (the PostgreSQL e2e proves the mechanism, but not yet with a second real source version)
3. **Delete the legacy fetch path and `patchright`** once the live gate confirms the port
4. **Fill real gold datasets** for WORK/STUDENT/VISITOR categories
5. **Re-source `SRC-AU-CRICOS-REGISTER`** (search-only SPA)
6. **Applicant-facing product UI**
7. **Multi-country support** (NZ, Canada, UK) and 491 real-world coverage
8. **RAG & recommendations** — LLM explains rules, suggests pathways; never decides eligibility
