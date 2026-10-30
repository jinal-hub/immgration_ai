# Immigration Platform Roadmap: Implementation and Verification Report

**Audit date:** 2026-10-30  
**Overall status:** P1 is implemented; P3 applicant and consultant workflow foundations and P4 tenant lifecycle/case isolation are implemented and covered by API and mocked-browser tests. P2, P5, and P6 remain partial; PostgreSQL lifecycle and live government fetching are not verified.

Existing user data was preserved. A single local-development demo applicant account and starter profile were added and verified; its temporary password was not stored in this report.

| Priority | Area | Status |
|---|---|---|
| P1 | Eligibility engine corrections | Completed |
| P2 | Government fetching and Playwright | Partially verified |
| P3 | Applicant application workflow | Core case, document, checklist, form, review, and package workflow implemented; production integrations incomplete |
| P4 | Multi-tenant consultancy | Tenant lifecycle and consultant portal implemented; operational deployment validation incomplete |
| P5 | Integration testing | Partially verified |
| P6 | Immigration country/program coverage | Incomplete |

## Executive summary

The eligibility engine had two correctness defects: optional rules could determine an applicant's business outcome, and persisted conditions/exceptions were not evaluated by the rule engine. Those are corrected with structured predicate evaluation, conservative handling of unsupported policy text, and the agreed rule that a verified exception waives the base rule. Date of birth now takes precedence over a stale stored age, invalid ages are rejected at the API boundary, and missing applicants are rejected before snapshot construction.

The project is **not** ready to claim that all sources have been fetched, that every applicant workflow is complete, or that consultancy tenancy is production-complete. The latest database audit available in this work recorded 28 of 96 sources checked: 9 OK, 6 access-blocked, and 13 fetch-failed; 68 were never attempted. A full live-source sync was not completed; refused hosts must continue to be respected rather than retried around.

All-source runs now have durable batch and per-source progress, restart recovery, and a resume path. This fixes the previous loss of in-memory progress after a process interruption, but it does not by itself prove that blocked sources can be fetched. A Playwright conditional-request bug that could send HEAD instead of GET was also corrected.

The consultancy sprint adds applicant and consultant case-workspace screens, tenant invitations and memberships, consultant case claiming, assessment review, assessment-linked checklists, private document storage and review, tenant-scoped dynamic form templates, applicant confirmation, and downloadable ZIP packages. The backend and UI do not make a visa decision or represent generated drafts as official government forms.

## Priority 1 — Correct eligibility

**Status: Implemented and regression-tested.**

- Only mandatory or critical rules gate the business eligibility outcome. Optional-rule results remain visible in the evaluation trace and counts.
- Rule conditions now support a bounded structured format: field/operator/value predicates, the legacy `if`/`equals` shape, and nested `and`/`or`/`not`. Unsupported or free-text conditions produce `UNKNOWN`; no policy text is executed or guessed.
- A condition that evaluates false marks its base rule `NOT_APPLICABLE`.
- A verified exception marks the base rule `NOT_APPLICABLE` (waived). If exception applicability is unknown and the base rule fails or is unknown, the result stays `UNKNOWN`.
- Applicant age is calculated from date of birth when present. Future birth dates are rejected, and entered ages outside 0–120 are rejected. The database age is used only when date of birth is absent.
- Assessment creation now raises the expected not-found error before trying to build a snapshot for a missing applicant.

Regression coverage includes optional failures/unknowns, critical rules, structured and unsupported conditions, exception waiver and uncertainty, age precedence and future-date rejection, and the missing-applicant path.

## Priority 2 — Verify government fetching

**Status: Fetch durability implemented; live-source verification still partial.**

- Python Playwright is installed. Chromium **151.0.7922.34** launched successfully and captured a local HTML page in this verification.
- Fetch tests cover HTML/PDF handling, timeouts, HTTP refusal classification, and host cooldown behavior. The full backend suite passed.
- All-source sync now creates durable `sync_jobs` and `sync_job_sources` rows before fetching, commits each source’s terminal state, marks interrupted work `INTERRUPTED`, and resumes without re-fetching already completed sources. Startup recovery handles orphaned `RUNNING` rows.
- Scheduled and manual runs share the same durable job identifier. The API can read a durable job after process restart and supports `POST /api/v1/admin/runs/{run_id}/resume` plus the `/api/v1/admin/sync/{run_id}/resume` alias.
- The Playwright conditional pre-check now uses GET rather than HEAD, avoiding hosts that reject HEAD with a misleading 403/405. Real 403/429 responses still pause the host and are never reported as success.
- The prior live checks recorded a Home Affairs HTTP 403, a CRICOS HTTP 200, and a legislation.gov.au PDF HTTP 200. A 403 establishes that the individual request was refused; it does **not** establish an IP-wide block.
- The latest database audit available for this report recorded 28/96 sources checked: 9 OK, 6 access-blocked, 13 fetch-failed, and 68 never attempted. This is a bounded audit, not a registry-wide fresh live sweep. No full-source sync was run in this pass.
- The host cooldown intentionally pauses requests after HTTP 403/429. No attempt was made here to bypass a refusal, use stealth/proxy rotation, or rerun every source.

**Operational conclusion:** Do not report the registry as fully fetched. No PostgreSQL migration or live-source batch was run as part of this consultancy sprint. Run only a small, approved source batch after reviewing cooldowns, then verify durable job rows, fetch logs, and timestamps before expanding the batch.

## Priority 3 — Complete applicant workflows

**Status: Core workflow implemented; storage, scanner, email, and deployment integrations still require configuration and production verification.**

Existing surfaces include applicant account registration/login and password reset, applicant profile operations, program listing, and persisted/inline eligibility assessments. The applicant portal displays saved profile information with an edit flow, lists both active and reference-only programs with search/category filtering, persists pathway selection, and shows the applicant's tenant-linked cases. Admin applicant views include tenant memberships and consultancy cases. Assessment gaps can be generated for missing required facts.

P3.1 answer persistence remains available with field validation and reassessment. Applicants can create and view tenant-linked cases, update permitted case state, use assessment-linked checklists, upload/download/delete private documents, complete versioned dynamic forms, confirm their answers, and download an application package. PDF/PNG/JPEG uploads are limited to 10 MiB. Storage supports private local development storage and signed S3-compatible object requests; document bytes are not served from a public static directory. Uploads are scanned only when a scanner implementing the integration contract is configured; scanner errors fail closed, and no scanner is claimed to run by default. Review state and actions are audited. Packages contain a draft summary PDF, accepted documents, and a manifest; they are not official government forms or evidence of visa eligibility.

The consultancy portal supports tenant creation, invitations, membership/workload views, tenant applicant and case lists, case assignment and consultant claiming, assessment review, checklist generation, form-template publication, document review, case-status tracking, and case activity. Tenant-created templates are isolated from other tenants; legacy global templates remain visible. Tenant and case routes enforce role and ownership checks, and platform-admin routes remain separate/global. Applicants' profile and eligibility workflows remain separate from application submission.

### Target applicant journey

1. Applicant registration
2. Applicant profile
3. Visa selection
4. Eligibility assessment
5. Follow-up questions
6. Document checklist
7. Document upload and verification
8. Visa application form generation
9. Consultant review
10. Applicant approval
11. Final application package

Eligibility assessment must remain separate from application generation. An assessment result is not a guarantee of qualification under every government requirement and does not mean an application is ready to lodge.

### P3 implementation milestones

| Milestone | Implementation | Expected result |
|---|---|---|
| P3.1 | Applicant question-and-answer persistence | Implemented with validation and reassessment |
| P3.2 | Document management | Private storage, upload/download/delete, scanning contract, tenant-aware review, and applicant/consultant UI |
| P3.3 | Dynamic document checklist | Case/assessment-linked checklist generation and applicant/consultant display |
| P3.4 | Application case management | Persistent tenant-owned cases, assignment/claim, status validation, activity audit, and case workspace |
| P3.5 | Application form generation | Tenant-scoped versioned templates, saved/validated answers, confirmation, PDF draft and ZIP package |
| P3.6 | Consultant review and approval | Assessment/document review and applicant form confirmation implemented; external sign-off/lodgement is not implemented |

## Priority 4 — Multi-tenant architecture

**Status: Tenant lifecycle and case/document/form isolation implemented; production deployment validation and broader platform-wide tenancy remain incomplete.**

The schema and API support tenant creation (the creating user is the tenant owner), invitations with hashed single-use tokens, tenant membership/roles, membership administration, applicant-to-tenant assignment, tenant-owned cases, consultant assignment/claiming, tenant-scoped assessments/documents/forms, and role-gated case operations. Applicants are restricted to their own profile and applicant-facing cases; consultants see assigned cases plus a metadata-only queue for unassigned cases in their tenant; tenant owners manage tenant membership. Cross-tenant case/document access and applicant roster access are denied by regression tests. Platform administrators remain on separate platform-global routes. Admin sources/rules are intentionally not tenant-scoped, and applicant profile/assessment ownership does not imply tenant ownership.

The local Playwright journeys mock API responses, while API tests use in-memory SQLite. No live PostgreSQL lifecycle or cross-tenant deployment test was run in this pass; prior reported database counts are historical and must not be interpreted as a fresh operational audit.

### Architecture sequencing decision

Keep platform-administrator privileges separate from tenant roles. Before production, run migration and lifecycle tests against PostgreSQL, configure email delivery and private object storage/scanning, and review whether any additional sensitive endpoints need tenant-level scoping. Do not extend tenant filters to intentionally platform-global source/rule operations.

## Priority 5 — Full integration testing

**Status: Full backend, frontend, build, and mocked Chromium journeys verified; live PostgreSQL integration remains incomplete.**

- Backend: full `pytest -q` passed **1,779 tests**, with **65 skipped**. The focused tenant/case/document suite passed **15 tests**.
- Frontend: **65 tests passed across 16 files**; the production Vite build succeeded.
- Chromium Playwright: **8 tests passed**, including applicant case/document/form/package, tenant-owner invitation/template/assignment, consultant claim, admin workflows, and accessibility checks. These journeys mock backend APIs and do not establish live full-stack behavior.
- Ruff: all checks passed for the changed backend workflow/migration/test files.
- The new form-template migration passed standalone SQLite upgrade/downgrade and tenant/global uniqueness checks; `alembic heads` reports one head. Generating SQL for the entire migration chain failed in an earlier migration (`f7a6d5c4b3a2`) because its preflight query requires a live connection. The entire chain and PostgreSQL migration were not applied or verified.
- No live PostgreSQL lifecycle, full-stack applicant/consultant browser journey, fresh government-source sweep, or end-to-end provenance audit was run.

## Priority 6 — Expand immigration coverage

**Status: Australia registry expanded; actual visa/rule coverage remains partial.**

The source registry contains 63 Australian program rows and 96 sources. Seven programs are `ACTIVE`; 56 are `REFERENCE_ONLY`. Reference-only entries are inventory, not applicant-assessment coverage. No non-Australian country was found in the registry. A source URL does not itself establish a reviewed, activated rule set.

## Changed implementation and tests

- `src/assessment/assessment.py` — required-rule aggregation.
- `src/rules/rule_engine.py` and `src/rules/expressions.py` — condition evaluation and exception-waiver semantics.
- `src/services/assessment_service.py` and `src/api/schemas/models.py` — age validation/authority and missing-applicant handling.
- `tests/assessment/test_assessment.py`, `tests/rules/test_exceptions.py`, `tests/rules/test_rule_engine.py`, `tests/test_assessment_service.py`, and `tests/test_api.py` — regression tests, including the account flow's automatically provisioned starter applicant.
- `src/db/models.py` and `src/db/repositories.py` — durable `SyncJob`/`SyncJobSource` records and repository operations for progress, interruption, recovery, and resume.
- `src/orchestration/government_sync.py` — per-source commits, durable skip reasons, status mapping, interruption handling, and resume behavior.
- `src/scheduler.py`, `src/api/main.py`, and `src/api/routes/pipeline_runs.py` — shared durable job IDs, startup recovery, durable job reads, and resume endpoints.
- `src/fetch/playwright_client.py` — conditional pre-check uses GET; refusal classification remains honest.
- `migrations/versions/a7b8c9d0e1f2_add_sync_jobs.py` — sync-job tables, indexes, and constraints.
- `tests/test_sync_job_progress.py`, `tests/test_pipeline_runs.py`, and `tests/test_playwright_client.py` — progress/interruption/resume, API durability, and request-method regression coverage.
- `frontend/src/hooks/useSyncRunner.js` — terminal handling for `PARTIAL` and `INTERRUPTED`.
- `src/api/routes/assessments.py`, `src/services/assessment_service.py`, and `frontend/src/pages/ApplicantPortalPage.jsx` — persisted follow-up answers and assessment refresh.
- `src/api/routes/applicants.py`, `src/api/routes/admin_applicants.py`, `frontend/src/api/client.js`, `frontend/src/pages/ApplicantPortalPage.jsx`, `frontend/src/pages/ApplicantsPage.jsx`, and `frontend/src/pages/ApplicantDetailPage.jsx` — persisted pathway selection, applicant-owned case visibility, saved profile editing, and admin tenant/case summaries. Visa selections are serialized before the database session closes; selecting an existing pathway refreshes its selection timestamp.
- `tests/test_applicant_programs_api.py` and `frontend/src/pages/__tests__/ApplicantPortalPage.test.jsx` — regression coverage for applicant pathway persistence and portal messaging.
- `src/db/models.py`, `src/services/tenant_service.py`, and `src/api/routes/tenants.py` / `cases.py` — tenant membership, case access, and case-document boundaries.
- `migrations/versions/b2c3d4e5f6a7_add_consultancy_tenants_and_cases.py` and `c3d4e5f6a7b8_add_case_documents.py` — tenant/case/document schema, applied to PostgreSQL.
- `src/services/case_document_storage.py` — private local/S3-compatible storage, signed requests, generated object keys, and server-side encryption request.
- `tests/test_tenant_cases.py` — membership privacy, cross-tenant case/document denial, upload validation, review role, and deletion authorization.
- `migrations/versions/e1d2c3b4a5f6_add_consultancy_workflow.py` and `f2a4c6e8b0d1_scope_form_templates_to_tenants.py` — workflow schema and tenant-scoped form templates; the latter was independently tested on SQLite, not applied to PostgreSQL.
- `src/api/routes/cases.py`, `src/api/routes/tenants.py`, `src/api/schemas/models.py`, and `src/services/tenant_service.py` — case workflow APIs, tenant lifecycle, permissions, checklist/form rules, audit events, assessment review, scanner integration, and package download.
- `src/services/case_document_scanner.py` and `src/services/case_package_service.py` — injectable fail-closed malware-scanning contract and PDF/ZIP application draft generation.
- `frontend/src/App.jsx`, `frontend/src/api/client.js`, `frontend/src/components/ApplicantLayout.jsx`, `CaseWorkspace.jsx`, `TenantInvitationAcceptPanel.jsx`, `frontend/src/pages/ApplicantCasesPage.jsx`, `ConsultancyPortalPage.jsx`, and `frontend/src/styles/case-workflow.css` — applicant and consultant workflow UI and navigation.
- `frontend/e2e/helpers.js` and `frontend/e2e/applicant-workflow.spec.js` — browser fixtures and mocked applicant, tenant-owner, and consultant journeys.

### Workflow API and UI summary

- Tenant APIs under `/api/v1/tenants` cover tenant creation/listing, memberships, invitations, applicant/case lists, assignment and claim queues, dashboard/workload summaries, and tenant-owned form templates.
- Case APIs under `/api/v1/cases` cover case read/status/assessment, checklists, document upload/list/download/review/delete, form templates/answers/confirmation, activity, and downloadable case packages.
- Applicant UI: case creation/listing, tenant invitation acceptance, case workspace, checklist, secure document actions, dynamic forms, confirmation, and package download.
- Consultant UI: tenant setup, invite/membership management, applicant and case lists, assignment/claim, assessment and document review, checklist/form management, status tracking, and case activity.

## Remaining work and limitations

1. **PostgreSQL integration:** apply and validate the complete Alembic chain, tenant lifecycle, cross-tenant denial, and document/form operations against PostgreSQL before deployment.
2. **Production integrations:** configure and test private object storage, a real malware scanner, and invitation email delivery. Invitation tokens are not emailed by this implementation.
3. **Document lifecycle:** explicit replacement/version history and external malware scanning are not implemented as built-in services.
4. **End-to-end validation:** current Playwright journeys mock backend APIs; run applicant and consultant browser flows against a live application/database before claiming full-stack verification.
5. **Government sources:** the last reported 28/96 audit is historical; run bounded, rate-limited batches and verify durable state without retrying around 403/429 refusals.
6. **Immigration coverage:** promote programs only after official-source verification, reviewed rule extraction, and program-specific eligibility tests.

Generated PDFs/packages are application drafts, not official government forms, legal advice, a visa decision, or a guarantee of eligibility. Eligibility assessment remains separate from application readiness. Existing user data was not deleted; no demo data was created or database reset during this sprint. No commits were made.
