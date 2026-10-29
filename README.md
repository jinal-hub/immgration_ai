# AU Immigration Knowledge Platform

An evidence-backed eligibility system for Australian visa programs. Government
pages are fetched politely and stored as immutable versions, requirements are
extracted by an LLM but never trusted on their own, humans approve them, approved
requirements compile into deterministic rules, and those rules produce
PASS/FAIL/UNKNOWN answers with a citation trail back to the exact government text.

**Core principle**: no LLM decides eligibility. LLMs help read government pages;
the eligibility answer is computed by deterministic code from versioned rules.

**State (2026-10-27)**: the internal admin workflow is complete and tested
(ingest, review, compile, activate, assess, search) and every change is gated by
CI. It is **not production-ready** until the human gates at the end of this file
are done. Production hardening is in place and verified locally; no target host has
been exercised. No live government host or paid LLM is contacted by the test suite.

---

## What works

| Area | State |
|---|---|
| Source registry | 14 verified sources across SKILLED_189/190/491, WORK_482, STUDENT_500, VISITOR_600 and the Migration Regulations; seeded idempotently from `sources/source_registry.seed.json` |
| Fetch | One Playwright Chromium path, headless, honest `FETCH_USER_AGENT`, per-host spacing + jitter, `Crawl-delay`/`Retry-After`, refusal cooldown per host, optional static egress proxy. No User-Agent rotation, no IP rotation, no challenge solving |
| Raw snapshots + versions | Immutable raw files on a Docker volume; SHA-256 content hashes; `INITIAL`/`CHANGED`/`UNCHANGED` classification; historical versions are never rewritten (append-only trigger) |
| Extraction | Cohere Command A by default (`EXTRACTION_MODEL`); strict JSON schema; evidence must be an exact substring of the source chunk; dedup, cache, budget/rate governance; zero-chunk and schema failures are visible, not silent |
| Requirement review | Admin queue with approve/reject/needs-review; reviewer is always the authenticated admin; full audit trail |
| Rule compilation and review | Requirement -> rule bridge with a closed concept vocabulary; DRAFT -> VALIDATED -> APPROVED -> ACTIVE; transactional supersession; batch transitions; rules mentioning a field with no applicant counterpart are flagged "not evaluable" and need a typed confirmation to activate |
| Dry run | `POST /admin/rules/evaluate` evaluates existing rules against a hypothetical profile without saving or activating anything; the UI exposes this as "Test rules" |
| Assessment | Eligible/ineligible/incomplete with gaps and follow-up questions; persisted `ruleset_hash`, rule versions and per-rule results |
| Search | `POST /api/v1/search` with keyword and pgvector semantic modes; `auto` falls back honestly and reports which mode ran |
| Admin UI | React + Vite console: dashboard, sources, review queue, rules (test/approve/activate), documents (search, versions, diff), applicants, scheduler. Talks only to existing API routes |
| Docker | API image bakes Playwright Chromium and runs unprivileged (UID 10001); `docker-compose.yml` runs PostgreSQL 16 + pgvector and the API; a separate `frontend/Dockerfile` builds the UI and serves it behind unprivileged nginx on the same origin; `docker-compose.prod.yml` adds TLS termination, private networking and no published API/DB ports |
| CI | Backend (hermetic), PostgreSQL e2e, frontend unit + browser/axe, and both container images, on every push/PR |

Known content limits are recorded in [Known limitations](#known-limitations).

---

## Quick start

Requires Python 3.12. On Windows/PowerShell, `pytest` alone does not put `src/`
on the path, so use the venv interpreter and let `conftest.py` handle imports:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pytest -q
```

The default suite is hermetic: it makes no network calls, launches no browser and
needs no database. Live/postgres suites are opt-in (see [Testing](#testing)).

### Docker stack

```powershell
Copy-Item .env.example .env   # then edit it
docker compose up -d
```

Compose runs migrations, seeds the registry, then serves the API on
`http://localhost:8000`. If another PostgreSQL already owns host port 5432, set
`POSTGRES_HOST_PORT=5433` and the matching `DATABASE_URL` port in `.env`; the
container-internal port does not change.

The admin UI is not part of the development `docker-compose.yml`. Run it from
source, or build its image and point nginx at the API:

```powershell
cd frontend
npm ci
npm run dev          # http://localhost:5173, API at VITE_API_BASE_URL
npm run build        # static bundle in dist/
docker build -t immigration-admin .
docker run --rm -p 8080:8080 --network immigration-knowledge_default immigration-admin
```

Both images run as unprivileged users (API UID 10001; nginx UID 101). The API
runs `alembic upgrade head` and seeds the registry before starting uvicorn.

If an existing raw-snapshot volume was created by an older root-running image,
give it to the API user once before starting the hardened image:

```powershell
docker run --rm -v immigration-knowledge-prod_raw_snapshots:/raw alpine chown -R 10001:10001 /raw
```

New named volumes inherit the image's `/app/raw` ownership and do not need this.

### Production compose (not exercised here)

`docker-compose.prod.yml` describes the production topology: PostgreSQL and the
API have no published ports, the UI is only reachable through Caddy on 80/443,
Caddy obtains and renews TLS, and the database/UI networks have no egress. The
API keeps one egress network for government/LLM calls. Required values have no
defaults, so compose refuses to start with a missing secret.

```powershell
Copy-Item deploy\prod.env.example <outside-repo>\immigration-prod.env
# fill DOMAIN, DATABASE_URL, POSTGRES_PASSWORD, JWT_SECRET_KEY, ADMIN_*
docker compose --env-file <outside-repo>\immigration-prod.env -f docker-compose.prod.yml up -d --build
docker compose --env-file <outside-repo>\immigration-prod.env -f docker-compose.prod.yml ps
```

There is **no provisioned target** and this file has not been run against a real
host. DNS/TLS issuance, firewall rules, PostgreSQL backup/restore, raw-snapshot
backup/restore, alerting and secret rotation must be verified in that environment
before any production-ready claim. Example backup/restore commands (run from the
repository, with the same env file):

```powershell
# Database (custom format, restore with pg_restore).
docker compose --env-file <outside-repo>\immigration-prod.env -f docker-compose.prod.yml exec -T postgres pg_dump -U immigration -d immigration -Fc > immigration.dump

# Raw snapshots (immutable files referenced by government_documents.raw_path).
docker run --rm -v immigration-knowledge-prod_raw_snapshots:/raw:ro -v ${PWD}:/backup alpine tar czf /backup/raw-snapshots.tgz -C /raw .
```

Restore into a **new, empty** database/volume, verify hashes, then switch over;
never restore over the live database or append-only audit tables in place.

On startup the API creates or promotes an admin when both `ADMIN_EMAIL` and
`ADMIN_PASSWORD` are set. Otherwise create one explicitly:

```powershell
.\.venv\Scripts\python.exe src\create_admin.py --email admin@example.com --password '<strong>'
```

Self-registration is **disabled by default** (`ALLOW_PUBLIC_REGISTRATION=false`)
and, even when enabled, always creates an `applicant` - never an admin.

---

## Configuration

Copy `.env.example` and fill in only what you need. The most important values:

| Variable | Default | Meaning |
|---|---|---|
| `DATABASE_URL` | none | SQLAlchemy URL. Name the driver: `postgresql+psycopg2://...` |
| `POSTGRES_HOST_PORT` | 5432 | Host port published by compose |
| `SECRET_KEY`, `JWT_SECRET_KEY` | placeholders | Replace before any real use |
| `ENVIRONMENT` | `development` | `production` fail-closes on weak/placeholder config |
| `CORS_ORIGINS` | localhost 3000/5173 | Comma-separated origins; production requires absolute HTTPS, no localhost |
| `ADMIN_EMAIL` / `ADMIN_PASSWORD` | unset | Idempotent admin bootstrap |
| `ALLOW_PUBLIC_REGISTRATION` | `false` | Must be `true` for `/auth/register` |
| `PASSWORD_MIN_LENGTH` | 8 | API minimum; production refuses values below 8 |
| `LOGIN_MAX_ATTEMPTS` / `LOGIN_LOCKOUT_SECONDS` | 5 / 900 | Per-email login throttle |
| `SOURCE_ALLOWED_HOST_SUFFIXES` | `.gov.au` | Source host allowlist; never a bare wildcard |
| `COHERE_API_KEY` | unset | Required for default Cohere extraction and real embeddings |
| `EXTRACTION_MODEL` | `command-a-03-2025` | Cohere Command model used for requirement extraction |
| `LLM_DAILY_BUDGET_USD` / `LLM_MONTHLY_BUDGET_USD` | 10 / 200 | Budget guard for extraction |
| `EMBEDDINGS_ENABLED` | `false` | Turns semantic search on; no silent mock fallback |
| `EMBEDDING_MODEL` | `embed-v4.0` | Cohere Embed v4; 1536 dimensions, matches pgvector column |
| `FETCH_USER_AGENT` | `AU-Immigration-Source-Monitor/0.1` | One honest UA per process |
| `FETCH_MIN_INTERVAL_SECONDS` / `FETCH_JITTER_SECONDS` | 8 / 4 | Same-host spacing |
| `FETCH_COOLDOWN_HOURS` | 0.25 (15 minutes) | Pause a host after 403/429 |
| `FETCH_PROXY_URL` | unset | One fixed egress proxy; malformed values fail closed |
| `ENABLE_SCHEDULER` | `true` | Daily sync scheduler |
| `GOVERNMENT_SYNC_HOUR_UTC` / `_MINUTE_UTC` | 3 / 0 | Daily schedule default (overridable in the DB/API) |

`GOVERNMENT_SYNC_INTERVAL_MINUTES` is a legacy interval mode; a schedule saved
through `PUT /api/v1/admin/scheduler` wins on restart.

---

## API

All `/api/v1/admin/*` routes require an admin JWT: 401 without a token, 403 for a
non-admin, 200 for an admin. Applicant-owned routes require a valid JWT and
enforce ownership.

| Group | Routes |
|---|---|
| Health | `GET /health` (liveness), `GET /ready` (DB check, scrubbed errors) |
| Auth | `POST /auth/register` (gated), `POST /auth/login`, `GET /auth/me` |
| Applicants | `POST/GET /applicants`, `GET/PATCH /applicants/{id}` |
| Assessments | `POST/GET /assessments`, `GET /assessments/{id}`, `POST /assessments/evaluate` |
| Search | `POST /search` - `{query, program_id?, document_id?, mode: auto\|keyword\|semantic, limit}`; 409 with a reason when semantic is requested but unavailable |
| Rules | `GET /admin/rules`, `/queue`, `/{id}`; `POST /{id}/validate`, `/approve`, `/reject`, `/activate`, `/retire`; batch variants; `POST /admin/rules/evaluate` (dry run) |
| Requirements | `GET /admin/requirements/review-queue`, `/{id}`; `POST /review/{id}/approve`, `/reject`, `/mark-needs-review` |
| Documents | `GET /admin/documents`, `/{id}`, `/{id}/versions`, `/{id}/chunks`, `/{id}/diff` |
| Sources | `GET/POST /admin/sources`, `GET/PATCH /{id}`, `POST /{id}/sync`; discovery: `POST /discover`, `GET /discovered`, approve/reject |
| Sync | `POST /admin/sync` (all ACTIVE sources; 202 + `run_id`, 409 if one is running) |
| Runs | `GET /admin/runs/{id}` - poll a sync job to completion |
| Scheduler | `GET/PUT /admin/scheduler` |
| Admin applicants | `GET /admin/applicants`, `/{id}` (cross-user, read-only) |

Activating a rule whose field has no applicant counterpart requires
`{"confirm_not_evaluable": "ACTIVATE"}`. The UI makes this explicit with a typed
confirmation; there is no silent fallback.

---

## How the pieces fit

```
government source
  -> polite fetch (Playwright, one host at a time)
  -> immutable raw snapshot
  -> document version + sha256 change detection
  -> chunks (headings, tables, page numbers)
  -> LLM requirement extraction + verbatim evidence check
  -> human review (approve / reject / needs review)
  -> deterministic rule compilation
  -> validate -> approve -> activate (older version superseded)
  -> assessment (PASS / FAIL / UNKNOWN, gaps, questions)
  -> persisted ruleset hash + per-rule results
```

Every rule can be traced to: rule version -> requirement -> evidence -> chunk ->
document version -> document -> source -> URL.

### Rule model

- Operators: `EQ NEQ LT LTE GT GTE IN NOT_IN CONTAINS NOT_CONTAINS EXISTS NOT_EXISTS`
- Expression nodes: leaf, AND, OR, NOT, conditional (IF/THEN), exception, dependency
- Missing applicant data is `UNKNOWN`, never `FAIL`
- At most one ACTIVE version per (program, category, field); activation supersedes atomically
- Assessment outcomes: `ELIGIBLE` (all pass), `INELIGIBLE` (any fail), `INCOMPLETE` (unknowns, no fail)
- Applicant concepts include points, English level, age, occupation, skills assessment,
  health/character, regional nomination, visa history, employer sponsorship,
  years of work experience, in-Australia-at-application, health insurance and
  genuine temporary entrant

### Semantic search

Keyword search always works. Semantic search needs `EMBEDDINGS_ENABLED=true`, an
embedding model, pgvector, and embedded chunks:

```powershell
# Set COHERE_API_KEY and EMBEDDINGS_ENABLED=true in the environment first.
$env:PYTHONPATH = "src"
.\.venv\Scripts\python.exe -m src.extraction.embeddings --backfill
```

The existing extraction pipeline creates evidence-preserving text chunks; Cohere
embeds those stored chunks and embeds search queries using the matching query mode.
Embedding is opt-in because it makes paid API calls. After enabling embeddings,
backfill existing chunks; new chunks can be backfilled the same way.

A semantic request that cannot run returns 409 with the specific reason;
`mode=auto` tries semantic, then keyword, and reports `mode` in the response so
the caller never has to guess. Embeddings are off by default because they cost
money and change ranking.

---

## Admin UI

`frontend/` is a plain React + Vite console (no component framework):

- **Dashboard** - source health, pipeline counters
- **Sources** - status, host cooldown, "Run now", source detail and history
- **Review** - requirement queue with evidence text
- **Rules** - badges for not-evaluable rules, dry-run panel ("Test rules"), single and
  batch transitions with typed confirmation
- **Documents** - keyword/semantic search, versions, chunk diff
- **Applicants** - cross-user read-only view
- **Scheduler** - edit the daily sync schedule

Commands: `npm test` (Vitest), `npm run test:e2e` (Playwright + axe), `npm run build`.

---

## Testing

Everything below was run on 2026-10-27.

| Suite | Command | Result |
|---|---|---|
| Backend, hermetic | `python -m pytest -q` | **1,695 passed, 65 skipped** (~2m24s) |
| PostgreSQL e2e | `POSTGRES_E2E=1 POSTGRES_E2E_URL=... python -m pytest tests/e2e tests/services/test_search_service.py tests/db/test_vector_type.py -q` | **56 passed** (~6s) |
| Frontend unit | `cd frontend; npm test` | **42 passed** (12 files) |
| Frontend browser | `cd frontend; npm run test:e2e` | **5 passed** (incl. axe: no serious/critical) |

The hermetic suite skips only tests that require a real PostgreSQL or an explicit
live opt-in; it never silently downgrades a live test into a fake one.

Larger layers (exact collection counts):

| Layer | Tests |
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
| `test_playwright_client.py` / `test_browser_fetcher.py` | 49 / 39 |
| `test_security.py` / `test_llm_governance.py` | 44 / 32 |
| `test_api.py` / `test_auth.py` | 41 / 13 |
| `test_scheduler.py` / `test_proxy.py` | 38 / 33 |
| `test_search_api.py` | 12 |

PostgreSQL e2e is guarded: `tests/e2e/conftest.py` refuses to run against a
database named `immigration` (the shared dev DB). Use a dedicated database:

```powershell
$env:POSTGRES_E2E = "1"
$env:POSTGRES_E2E_URL = "postgresql+psycopg2://immigration:immigration_dev@localhost:5433/immigration_e2e"
.\.venv\Scripts\python.exe -m pytest tests/e2e -q
```

CI (`.github/workflows/ci.yml`) runs the same four gates on every push/PR:
hermetic backend, PostgreSQL 16 + pgvector + migrations + e2e, frontend
unit/build/Playwright/axe, and both Docker builds. Dependabot opens grouped
weekly updates for pip, npm and actions.

---

## Project structure

```
immigration-knowledge/
├── src/
│   ├── api/                  # FastAPI app, routes, Pydantic schemas
│   │   └── routes/           # auth, applicants, assessments, search, admin_*, health
│   ├── db/                   # models (33 tables), connection, repositories, vector helpers
│   ├── fetch/                # playwright_client, politeness, proxy, extractors
│   ├── extraction/           # LLM clients, schema/evidence checks, embeddings, gold harness
│   ├── rules/                # operators, expression tree, compiler, activation, evaluability
│   ├── assessment/           # applicant model, assessment + gap engine
│   ├── change_detection/     # hashing, change classification
│   ├── orchestration/        # government sync, host cooldown
│   ├── services/             # auth, applicants, assessments, eligibility, search
│   ├── utils/                # fetch -> store -> version -> extract
│   ├── pipeline.py           # CLI: run the ingestion pipeline
│   ├── scheduler.py          # APScheduler setup
│   ├── seed_registry.py      # CLI: seed countries/programs/sources
│   └── create_admin.py       # CLI: bootstrap an admin
├── tests/                    # pytest suites (+ tests/e2e, guarded PostgreSQL tests)
├── frontend/                 # React admin console, Vitest, Playwright, Dockerfile
├── migrations/versions/      # 25 alembic migrations (head e5f6a7b8c9d0)
├── sources/                  # source_registry.seed.json
├── prompts/                  # extraction prompts per visa category
├── schema/                   # JSON schema + static SQL snapshot
├── data/gold/                # verbatim gold sets (skilled populated; others placeholders)
├── scripts/                  # db_structure.py, live_fetch_check.py, browser_smoke.py
├── .github/                  # CI + Dependabot
├── Dockerfile                # API image (Playwright Chromium baked in)
├── frontend/Dockerfile       # nginx image serving the admin UI
├── docker-compose.yml        # dev: postgres (pgvector) + api
├── docker-compose.prod.yml   # prod: TLS, private networks, no published API/DB
├── deploy/                   # Caddyfile + production env example
├── DBSTRUCTURE.md            # regenerated with scripts/db_structure.py
├── DESIGN.md                 # architecture, gaps, roadmap and verification
└── README.md
```

`DBSTRUCTURE.md` is generated, not hand-written:

```powershell
.\.venv\Scripts\python.exe scripts\db_structure.py
```

---

## Fetching, honestly

The fetch layer is one code path: Playwright drives a real headless Chromium
(`src/fetch/playwright_client.py`). It sends an honest User-Agent, loads the page
in `en-AU`, and uses only the launch flags a container needs
(`--no-sandbox`, `--disable-dev-shm-usage`). The image itself runs unprivileged,
so those flags do not also mean running as root. It waits for real content (bounded)
because several sources render after `domcontentloaded` - waiting is not evasion,
it just stops racing the page.

Politeness is enforced in `src/fetch/politeness.py`:

- Requests to the same host are spaced by `FETCH_MIN_INTERVAL_SECONDS` plus jitter.
- `robots.txt` `Crawl-delay` and `Retry-After` are honoured.
- A 403/429 pauses every source on that host for `FETCH_COOLDOWN_HOURS` (default
  15 minutes); there is
  deliberately no override flag and no retry with a different identity.
- `FETCH_PROXY_URL` is one fixed egress, never rotated, and fails closed.

A retired fetch path remains behind `FETCH_BACKEND=legacy` only until the live
check against `SRC-AU-MIGRATION-REGS-1994` confirms the Playwright port; then it
and the `patchright` dependency are deleted. Nothing new imports it.

---

## Security

- Real JWT auth on every non-public route; admin role checked in the database.
- Admin identity for audit rows comes from the token, never from the request body.
- Public registration is off unless explicitly enabled, and cannot create an admin.
- bcrypt is pinned (`bcrypt==4.2.1`) for passlib compatibility.
- Secrets come from the environment; `.env` is gitignored and never baked into images.
- Proxy credentials are redacted from logs and error strings; malformed proxy config fails closed.
- PII (date of birth, passport, email) is redacted in structured logs.
- Historical rows (assessments, rule versions, document versions, evidence) are
  protected by append-only triggers.

---

## Curated gold rules

The real-capture datasets for subclasses 189 and 190 are not automatic
production seeds. To make their positive annotations reviewable in the admin
console, run the importer against the configured database:

```powershell
python src/seed_gold_rules.py
python src/seed_gold_rules.py --gold-file data/gold/skilled/skilled_190_real.json --chunks-file data/real_validation/skilled_190_real_chunks.json
```

The importer verifies every evidence quote against its captured chunk snapshot,
stores that snapshot and its evidence lineage, skips negative examples, and
creates rules only as `DRAFT` (with requirements in `NEEDS_REVIEW`). It is
idempotent and never activates rules. The dashboard groups the imported rules
by program, displays each rule as JSON, and offers explicit Activate/Reject
decisions; activation records the existing validation and human-approval
stages, and rules no applicant field can answer still require typing `ACTIVATE`.

The Student, Visitor, and Work gold files currently contain no curated rules;
the dashboard shows that state rather than inventing examples.
`data/gold/requirements_gold.json` is synthetic test data, not verbatim
government evidence. The dashboard displays its examples separately as
read-only, and they cannot be activated.

## Known limitations

- **Coverage is uneven.** `SRC-AU-CRICOS-REGISTER` is a search-only SPA and yields
  no listable content; it needs a different source (e.g. a bulk export). WORK,
  STUDENT and VISITOR gold sets are still empty placeholders - no fabricated
  evidence is used to fill them.
- **Provider changes require validation.** Cohere extraction and embedding use
  the same strict schema/evidence checks; provider failures remain visible on
  the extraction run.
- **Supersession with real government data** is proven by the e2e test but has not
  yet been exercised against a second real version of a real source.
- **No applicant-facing UI.** The console is an internal admin tool.
- **Single-process LLM governance** (in-memory rate/budget state); horizontal
  scaling would need a shared store.
- **No deploy target has been provisioned or exercised.** Images build and
  `docker-compose.prod.yml` defines orchestration/TLS, but DNS, certificates,
  firewall, backups/restores, monitoring and rollback have not been validated on
  a real host.
- Multi-country support is not started; the schema carries `country_id`, but only
  Australia is seeded.

---

## Before production: human gates

These require a person, a real key, or a real network call, so they are not
automated and not run in CI.

1. **Confirm the Playwright port on a real source** (writes nothing):
   ```powershell
   $env:LIVE_FETCH_OK = "1"
   .\.venv\Scripts\python.exe scripts\live_fetch_check.py SRC-AU-MIGRATION-REGS-1994
   ```
   Exit 0 with the expected hash proves the new fetch path returns the same bytes
   the old path recorded. Only then remove the legacy fetch path and `patchright`.
2. **One Home Affairs source**: in the UI, Sources -> "Run now" on a single
   source; confirm a new version and chunks appear and the host is not hammered.
3. **All never-synced sources**: Sources -> "Run all"; refusals must be recorded
   as blocked/paused and never retried with a different mechanism.
4. **Review the SKILLED_189 rules**: dry-run them, then approve and activate.
   Rules flagged "not evaluable" need the typed confirmation.
5. **Rotate secrets**: real `COHERE_API_KEY`, `SECRET_KEY`,
   `JWT_SECRET_KEY`, `POSTGRES_PASSWORD`, and a strong admin password. Keep
   `ALLOW_PUBLIC_REGISTRATION=false` unless public sign-up is intended.

---

## License

Internal project. Not for distribution.
