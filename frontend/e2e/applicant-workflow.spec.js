import { test, expect } from "@playwright/test";
import { openApplicantPage } from "./helpers.js";

const PROFILE = {
  applicant_id: "applicant-1",
  user_id: "applicant-user-1",
  first_name: "Alex",
  last_name: "Applicant",
};

const PROGRAM = {
  program_id: "SKILLED_189",
  name: "Skilled Independent visa",
  country_id: "AU",
  visa_category: "SKILLED",
  subclass_code: "189",
  status: "ACTIVE",
};

const TEMPLATE = {
  template_id: "template-1",
  tenant_id: "tenant-1",
  form_key: "applicant-details",
  version: 1,
  title: "Applicant details",
  program_id: "SKILLED_189",
  status: "PUBLISHED",
  schema_json: {
    questions: [
      { key: "full_name", label: "Full legal name", type: "text", required: true },
      { key: "age", label: "Age", type: "number", required: true },
    ],
  },
};

test("applicant creates a case, uploads evidence, completes a form, and downloads a package", async ({ page }) => {
  let caseRecord = null;
  let formAnswer = null;
  let documents = [];
  const tenant = {
    tenant_id: "tenant-1",
    name: "North Star Consultancy",
    slug: "north-star",
    role: "APPLICANT",
    status: "ACTIVE",
  };
  const templates = [TEMPLATE];

  await openApplicantPage(page, "/portal/cases", {
    "GET /api/v1/applicants": [PROFILE],
    "GET /api/v1/tenants": [tenant],
    "GET /api/v1/applicants/programs": [PROGRAM],
    "GET /api/v1/applicants/applicant-1/cases": () => caseRecord ? [caseRecord] : [],
    "GET /api/v1/assessments": { assessments: [], total: 0 },
    "POST /api/v1/tenants/tenant-1/cases": (request) => {
      const body = request.postDataJSON();
      caseRecord = {
        case_id: "case-1",
        tenant_id: "tenant-1",
        tenant_name: tenant.name,
        applicant_id: body.applicant_id,
        program_id: body.program_id,
        created_by: "applicant-user-1",
        assigned_consultant_id: null,
        assessment_id: body.assessment_id || null,
        ruleset_id: null,
        status: "DRAFT",
        title: body.title || null,
        created_at: "2026-10-10T03:00:00Z",
        updated_at: "2026-10-10T03:00:00Z",
      };
      return caseRecord;
    },
    "PATCH /api/v1/cases/case-1/status": (request) => {
      caseRecord.status = request.postDataJSON().status;
      return caseRecord;
    },
    "GET /api/v1/cases/case-1/documents": () => documents,
    "POST /api/v1/cases/case-1/documents": () => {
      const document = {
        document_id: "document-1",
        case_id: "case-1",
        tenant_id: "tenant-1",
        uploaded_by: "applicant-user-1",
        filename: "passport.pdf",
        content_type: "application/pdf",
        size_bytes: 20,
        sha256: "abc123",
        review_status: "PENDING_REVIEW",
        version: 1,
        scanner_status: "NOT_CONFIGURED",
        created_at: "2026-10-10T03:00:00Z",
      };
      documents = [document];
      return document;
    },
    "GET /api/v1/cases/case-1/checklist": [],
    "GET /api/v1/cases/case-1/forms": () => formAnswer ? [formAnswer] : [],
    "GET /api/v1/tenants/tenant-1/form-templates": templates,
    "GET /api/v1/cases/case-1/assessment": {
      status: 404,
      body: { detail: "No linked assessment" },
    },
    "GET /api/v1/tenants/tenant-1/cases/case-1/activities": [],
    "POST /api/v1/cases/case-1/forms/template-1": (request) => {
      formAnswer = {
        answer_id: "answer-1",
        case_id: "case-1",
        tenant_id: "tenant-1",
        template_id: "template-1",
        answers_json: request.postDataJSON().answers_json,
        status: "DRAFT",
        missing_fields: [],
      };
      return formAnswer;
    },
    "POST /api/v1/cases/case-1/forms/template-1/submit": () => {
      formAnswer.status = "SUBMITTED";
      formAnswer.submitted_at = "2026-10-10T04:00:00Z";
      return formAnswer;
    },
    "POST /api/v1/cases/case-1/forms/template-1/confirm": () => {
      formAnswer.applicant_confirmed_at = "2026-10-10T04:15:00Z";
      return formAnswer;
    },
    "GET /api/v1/cases/case-1/package": { draft_package: true },
  });

  await page.getByLabel("Visa program").selectOption("SKILLED_189");
  await page.getByRole("button", { name: "Create application case" }).click();
  await expect(page.getByRole("region", { name: "Application case workspace" })
    .getByRole("heading", { name: "Subclass SKILLED_189" })).toBeVisible();
  await page.getByRole("button", { name: "IN PROGRESS" }).click();
  await expect(page.getByRole("region", { name: "Application case workspace" })
    .locator(".case-status-pill").first()).toHaveText("IN PROGRESS");

  await page.getByLabel(/Full legal name/).fill("Alex Applicant");
  await page.getByLabel("Age *").fill("32");
  await page.getByRole("button", { name: "Save draft" }).click();
  await expect(page.getByText("Form draft saved.")).toBeVisible();
  await page.getByRole("button", { name: "Submit for review" }).click();
  await expect(page.getByText("SUBMITTED", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Confirm these answers" }).click();
  await expect(page.getByText("Form confirmation recorded.")).toBeVisible();

  await page.locator('input[type="file"]').setInputFiles({
    name: "passport.pdf",
    mimeType: "application/pdf",
    buffer: Buffer.from("%PDF-1.7 applicant evidence"),
  });
  await expect(page.getByText("passport.pdf")).toBeVisible();
  const download = page.waitForEvent("download");
  await page.getByRole("button", { name: "Download package" }).click();
  expect((await download).suggestedFilename()).toBe("case-case-1-package.zip");
});

test("tenant owner creates a workspace, invites a consultant, publishes a form, and assigns a case", async ({ page }) => {
  let tenants = [];
  let cases = [];
  let invitations = [];
  let templates = [];
  let checklist = [];
  const tenant = {
    tenant_id: "tenant-2",
    name: "North Star Migration",
    slug: "north-star-migration",
    role: "OWNER",
    status: "ACTIVE",
  };
  const consultant = {
    membership_id: "membership-consultant",
    tenant_id: "tenant-2",
    user_id: "consultant-user",
    email: "consultant@example.test",
    full_name: "Jordan Consultant",
    role: "CONSULTANT",
    status: "ACTIVE",
  };
  const applicant = {
    applicant_id: "applicant-2",
    user_id: "applicant-user-2",
    email: "sam@example.test",
    full_name: "Sam Applicant",
    first_name: "Sam",
    last_name: "Applicant",
  };

  await openApplicantPage(page, "/consultancy", {
    "GET /api/v1/auth/me": {
      user_id: "owner-user",
      email: "owner@example.test",
      full_name: "Taylor Owner",
      role: "applicant",
    },
    "GET /api/v1/tenants": () => tenants,
    "POST /api/v1/tenants": (request) => {
      const body = request.postDataJSON();
      tenant.name = body.name;
      tenant.slug = body.slug;
      tenants = [tenant];
      return tenant;
    },
    "GET /api/v1/applicants/programs": [PROGRAM],
    "GET /api/v1/tenants/tenant-2/dashboard": () => ({
      case_count: cases.length,
      by_status: {},
      consultant_workload: [],
    }),
    "GET /api/v1/tenants/tenant-2/cases": () => cases,
    "GET /api/v1/tenants/tenant-2/applicants": [applicant],
    "GET /api/v1/tenants/tenant-2/members": [
      {
        membership_id: "membership-owner",
        tenant_id: "tenant-2",
        user_id: "owner-user",
        email: "owner@example.test",
        full_name: "Taylor Owner",
        role: "OWNER",
        status: "ACTIVE",
      },
      consultant,
    ],
    "GET /api/v1/tenants/tenant-2/invitations": () => invitations,
    "GET /api/v1/tenants/tenant-2/form-templates": () => templates,
    "POST /api/v1/tenants/tenant-2/invitations": (request) => {
      const body = request.postDataJSON();
      const invitation = {
        invitation_id: "invitation-1",
        tenant_id: "tenant-2",
        email: body.email,
        role: body.role,
        invitation_token: "one-time-consultant-token",
        status: "PENDING",
      };
      invitations = [{ ...invitation, invitation_token: undefined }];
      return invitation;
    },
    "POST /api/v1/tenants/tenant-2/form-templates": (request) => {
      const body = request.postDataJSON();
      const template = {
        ...body,
        template_id: "template-2",
        tenant_id: "tenant-2",
        status: "PUBLISHED",
        created_by: "owner-user",
      };
      templates = [template];
      return template;
    },
    "POST /api/v1/tenants/tenant-2/cases": (request) => {
      const body = request.postDataJSON();
      const created = {
        case_id: "case-2",
        tenant_id: "tenant-2",
        applicant_id: body.applicant_id,
        program_id: body.program_id,
        created_by: "owner-user",
        assigned_consultant_id: null,
        assessment_id: null,
        ruleset_id: null,
        status: "DRAFT",
        title: body.title || null,
        created_at: "2026-10-10T03:00:00Z",
        updated_at: "2026-10-10T03:00:00Z",
      };
      cases = [created];
      return created;
    },
    "POST /api/v1/tenants/tenant-2/cases/case-2/assign": (request) => {
      cases[0].assigned_consultant_id = request.postDataJSON().consultant_id;
      return cases[0];
    },
    "GET /api/v1/cases/case-2/documents": [],
    "GET /api/v1/cases/case-2/checklist": () => checklist,
    "POST /api/v1/cases/case-2/checklist/generate": () => {
      checklist = [{
        item_id: "checklist-1",
        case_id: "case-2",
        tenant_id: "tenant-2",
        requirement_key: "passport_copy",
        label: "Passport copy",
        description: "Provide the passport identity page.",
        category: "document",
        required: true,
        status: "PENDING",
        position: 0,
      }];
      return checklist;
    },
    "GET /api/v1/cases/case-2/forms": [],
    "GET /api/v1/cases/case-2/assessment": {
      status: 404,
      body: { detail: "No linked assessment" },
    },
    "GET /api/v1/tenants/tenant-2/cases/case-2/activities": [],
  });

  await page.getByLabel("Consultancy name").fill("North Star Migration");
  await page.getByRole("button", { name: "Create tenant" }).click();
  await expect(page.getByRole("heading", { name: "Case dashboard" })).toBeVisible();

  await page.getByLabel("Email address").fill("consultant@example.test");
  await page.getByRole("button", { name: "Create invitation" }).click();
  await expect(page.getByText("one-time-consultant-token")).toBeVisible();

  await page.getByLabel("Form key").fill("applicant-details");
  await page.getByLabel("Title", { exact: true }).fill("Applicant details");
  await page.getByRole("button", { name: "Publish form template" }).click();
  await expect(page.getByText("Versioned form template published.")).toBeVisible();

  await page.getByLabel("Visa program").selectOption("SKILLED_189");
  await page.getByRole("button", { name: "Create application case" }).click();
  await page.getByLabel("Assign case").selectOption("consultant-user");
  await expect(page.getByText("Assigned: Jordan Consultant")).toBeVisible();
  await page.getByRole("button", { name: "Generate checklist" }).click();
  await expect(page.getByText("Passport copy")).toBeVisible();
});

test("consultant claims a tenant-scoped unassigned case", async ({ page }) => {
  const tenant = {
    tenant_id: "tenant-3",
    name: "Harbour Consultancy",
    slug: "harbour",
    role: "CONSULTANT",
    status: "ACTIVE",
  };
  let claimed = false;
  const caseRecord = {
    case_id: "case-3",
    tenant_id: "tenant-3",
    applicant_id: "applicant-3",
    program_id: "SKILLED_189",
    created_by: "owner-3",
    assigned_consultant_id: null,
    assessment_id: null,
    ruleset_id: null,
    status: "DRAFT",
    title: "189 case",
    created_at: "2026-10-10T03:00:00Z",
    updated_at: "2026-10-10T03:00:00Z",
  };

  await openApplicantPage(page, "/consultancy", {
    "GET /api/v1/auth/me": {
      user_id: "consultant-user-3",
      email: "consultant3@example.test",
      full_name: "Morgan Consultant",
      role: "applicant",
    },
    "GET /api/v1/tenants": [tenant],
    "GET /api/v1/applicants/programs": [PROGRAM],
    "GET /api/v1/tenants/tenant-3/dashboard": {
      case_count: claimed ? 1 : 0,
      by_status: {},
      consultant_workload: [],
    },
    "GET /api/v1/tenants/tenant-3/cases": () => claimed
      ? [{ ...caseRecord, assigned_consultant_id: "consultant-user-3" }]
      : [],
    "GET /api/v1/tenants/tenant-3/applicants": [],
    "GET /api/v1/tenants/tenant-3/members": [],
    "GET /api/v1/tenants/tenant-3/form-templates": [],
    "GET /api/v1/tenants/tenant-3/unassigned-cases": () => claimed ? [] : [{
      case_id: caseRecord.case_id,
      tenant_id: tenant.tenant_id,
      program_id: caseRecord.program_id,
      status: caseRecord.status,
      title: caseRecord.title,
      created_at: caseRecord.created_at,
    }],
    "POST /api/v1/tenants/tenant-3/cases/case-3/assign": (request) => {
      claimed = request.postDataJSON().consultant_id === "consultant-user-3";
      return { ...caseRecord, assigned_consultant_id: "consultant-user-3" };
    },
    "GET /api/v1/cases/case-3/documents": [],
    "GET /api/v1/cases/case-3/checklist": [],
    "GET /api/v1/cases/case-3/forms": [],
    "GET /api/v1/cases/case-3/assessment": {
      status: 404,
      body: { detail: "No linked assessment" },
    },
    "GET /api/v1/tenants/tenant-3/cases/case-3/activities": [],
  });

  await page.getByRole("heading", { name: "Unassigned case queue" }).waitFor();
  await page.getByRole("button", { name: "Claim case" }).click();
  await expect(page.getByRole("heading", { name: "189 case" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Unassigned case queue" })).toBeVisible();
  await expect(page.getByText("No unassigned cases are available.")).toBeVisible();
});
