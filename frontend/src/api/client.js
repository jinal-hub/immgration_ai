// Fetch wrapper: base URL from VITE_API_BASE_URL, attaches the admin JWT,
// normalizes errors, and treats ANY 401 as "log out now" - there is no
// refresh endpoint, so a 401 can only mean the session is no longer valid.
const API_BASE = import.meta.env.VITE_API_BASE_URL || "http://localhost:8000";
const TOKEN_KEYS = {
  admin: "ik_admin_token",
  applicant: "ik_applicant_token",
};

const unauthorizedHandlers = {
  admin: null,
  applicant: null,
};

// Registered once by AuthContext on mount. Kept outside React so every
// request (even ones fired from outside a component) can trigger logout.
export function setUnauthorizedHandler(fn, tokenContext = "admin") {
  unauthorizedHandlers[tokenContext] = fn;
}

export function getToken(tokenContext = "admin") {
  try {
    return sessionStorage.getItem(TOKEN_KEYS[tokenContext]);
  } catch {
    return null;
  }
}

export function setToken(token, tokenContext = "admin") {
  try {
    sessionStorage.setItem(TOKEN_KEYS[tokenContext], token);
  } catch {
    /* sessionStorage unavailable (private mode, etc.) - session just won't persist */
  }
}

export function clearToken(tokenContext = "admin") {
  try {
    sessionStorage.removeItem(TOKEN_KEYS[tokenContext]);
  } catch {
    /* ignore */
  }
}

export class ApiError extends Error {
  constructor(message, status, body) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.body = body;
  }
}

function toQuery(params) {
  if (!params) return "";
  const entries = Object.entries(params).filter(([, v]) => v !== undefined && v !== null && v !== "");
  if (entries.length === 0) return "";
  return "?" + new URLSearchParams(entries).toString();
}

async function request(
  path,
  {
    method = "GET",
    body,
    rawBody,
    rawContentType,
    responseType,
    auth = true,
    query,
    tokenContext = "admin",
  } = {}
) {
  const headers = {};
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (rawBody !== undefined && rawContentType) {
    headers["Content-Type"] = rawContentType;
  }
  if (auth) {
    const token = getToken(tokenContext);
    if (token) headers.Authorization = `Bearer ${token}`;
  }

  let res;
  try {
    res = await fetch(`${API_BASE}${path}${toQuery(query)}`, {
      method,
      headers,
      body: body !== undefined ? JSON.stringify(body) : rawBody,
    });
  } catch {
    throw new ApiError("Could not reach the server. Check your connection and try again.", 0, null);
  }

  if (res.ok && responseType === "blob") return res.blob();

  let data = null;
  const text = await res.text();
  if (text) {
    try {
      data = JSON.parse(text);
    } catch {
      data = text;
    }
  }

  if (!res.ok) {
    if (res.status === 401 && auth && unauthorizedHandlers[tokenContext]) {
      unauthorizedHandlers[tokenContext]();
    }
    const detail =
      (data && typeof data === "object" && (data.detail || data.message)) ||
      res.statusText ||
      "Request failed";
    throw new ApiError(typeof detail === "string" ? detail : JSON.stringify(detail), res.status, data);
  }

  return data;
}

// ---------------------------------------------------------------------------
// Auth
// ---------------------------------------------------------------------------
export function login(email, password) {
  return request("/api/v1/auth/login", { method: "POST", body: { email, password }, auth: false });
}

export function loginApplicant(email, password) {
  return request("/api/v1/auth/login", {
    method: "POST",
    body: { email, password },
    auth: false,
  });
}

export function registerApplicant(email, password, fullName) {
  return request("/api/v1/auth/register", {
    method: "POST",
    body: { email, password, full_name: fullName },
    auth: false,
  });
}

export function requestApplicantPasswordReset(email) {
  return request("/api/v1/auth/password-reset/request", {
    method: "POST",
    body: { email },
    auth: false,
  });
}

export function confirmApplicantPasswordReset(token, password) {
  return request("/api/v1/auth/password-reset/confirm", {
    method: "POST",
    body: { token, password },
    auth: false,
  });
}

export function getMe(tokenContext = "admin") {
  return request("/api/v1/auth/me", { tokenContext });
}

// ---------------------------------------------------------------------------
// Dashboard-ish aggregates built from existing endpoints
// ---------------------------------------------------------------------------
export function listSources() {
  return request("/api/v1/admin/sources");
}

export function getSource(sourceId) {
  return request(`/api/v1/admin/sources/${encodeURIComponent(sourceId)}`);
}

export function createSource(body) {
  return request("/api/v1/admin/sources", { method: "POST", body });
}

export function updateSource(sourceId, body) {
  return request(`/api/v1/admin/sources/${encodeURIComponent(sourceId)}`, { method: "PATCH", body });
}

export function syncSource(sourceId) {
  return request(`/api/v1/admin/sources/${encodeURIComponent(sourceId)}/sync`, { method: "POST" });
}

export function syncAllSources() {
  return request("/api/v1/admin/sync", { method: "POST" });
}

export function getRun(runId) {
  return request(`/api/v1/admin/runs/${encodeURIComponent(runId)}`);
}

// ---------------------------------------------------------------------------
// Requirements / review queue
// ---------------------------------------------------------------------------
export function getReviewQueue({ status = "NEEDS_REVIEW", programId, evaluableOnly = true } = {}) {
  return request("/api/v1/admin/requirements/review-queue", {
    query: { status, program_id: programId, evaluable_only: evaluableOnly },
  });
}

export function approveRequirement(requirementId, notes) {
  return request(`/api/v1/admin/requirements/review/${encodeURIComponent(requirementId)}/approve`, {
    method: "POST",
    body: { requirement_id: requirementId, action: "APPROVE", notes },
  });
}

export function rejectRequirement(requirementId, notes) {
  return request(`/api/v1/admin/requirements/review/${encodeURIComponent(requirementId)}/reject`, {
    method: "POST",
    body: { requirement_id: requirementId, action: "REJECT", notes },
  });
}

export function markNeedsReviewRequirement(requirementId, notes) {
  return request(
    `/api/v1/admin/requirements/review/${encodeURIComponent(requirementId)}/mark-needs-review`,
    { method: "POST", body: { requirement_id: requirementId, action: "MARK_NEEDS_REVIEW", notes } }
  );
}

// ---------------------------------------------------------------------------
// Rules
// ---------------------------------------------------------------------------
export function getRulesQueue(programId) {
  return request("/api/v1/admin/rules/queue", { query: { program_id: programId } });
}

export function listRules({ status, programId } = {}) {
  return request("/api/v1/admin/rules", { query: { status, program_id: programId } });
}

export function getSyntheticGoldExamples() {
  return request("/api/v1/admin/rules/gold-examples");
}

export function reviewGoldRule(ruleId, decision, { confirmNotEvaluable } = {}) {
  return request(`/api/v1/admin/rules/${encodeURIComponent(ruleId)}/review`, {
    method: "POST",
    body: {
      decision,
      ...(confirmNotEvaluable ? { confirm_not_evaluable: confirmNotEvaluable } : {}),
    },
  });
}

export function validateRule(ruleId) {
  return request(`/api/v1/admin/rules/${encodeURIComponent(ruleId)}/validate`, { method: "POST", body: {} });
}

export function approveRule(ruleId) {
  return request(`/api/v1/admin/rules/${encodeURIComponent(ruleId)}/approve`, { method: "POST", body: {} });
}

export function rejectRule(ruleId) {
  return request(`/api/v1/admin/rules/${encodeURIComponent(ruleId)}/reject`, { method: "POST", body: {} });
}

export function activateRule(ruleId, { confirmNotEvaluable } = {}) {
  return request(`/api/v1/admin/rules/${encodeURIComponent(ruleId)}/activate`, {
    method: "POST",
    body: confirmNotEvaluable ? { confirm_not_evaluable: confirmNotEvaluable } : {},
  });
}

export function retireRule(ruleId) {
  return request(`/api/v1/admin/rules/${encodeURIComponent(ruleId)}/retire`, { method: "POST", body: {} });
}

export function batchValidateRules(ruleIds) {
  return request("/api/v1/admin/rules/batch/validate", { method: "POST", body: { rule_ids: ruleIds } });
}

export function batchApproveRules(ruleIds) {
  return request("/api/v1/admin/rules/batch/approve", { method: "POST", body: { rule_ids: ruleIds } });
}

export function batchActivateRules(ruleIds, { confirmNotEvaluable } = {}) {
  return request("/api/v1/admin/rules/batch/activate", {
    method: "POST",
    body: confirmNotEvaluable
      ? { rule_ids: ruleIds, confirm_not_evaluable: confirmNotEvaluable }
      : { rule_ids: ruleIds },
  });
}

// Dry-run the rules against a hypothetical applicant. Deterministic and
// side-effect free: it never activates, edits or records anything.
export function evaluateRules(body) {
  return request("/api/v1/admin/rules/evaluate", { method: "POST", body });
}

// ---------------------------------------------------------------------------
// Documents
// ---------------------------------------------------------------------------
export function listDocuments({ sourceId, programId, status } = {}) {
  return request("/api/v1/admin/documents", { query: { source_id: sourceId, program_id: programId, status } });
}

export function getDocument(documentId) {
  return request(`/api/v1/admin/documents/${encodeURIComponent(documentId)}`);
}

export function getDocumentVersions(documentId) {
  return request(`/api/v1/admin/documents/${encodeURIComponent(documentId)}/versions`);
}

export function getDocumentChunks(documentId, versionId) {
  return request(`/api/v1/admin/documents/${encodeURIComponent(documentId)}/chunks`, {
    query: { version_id: versionId },
  });
}

export function getDocumentDiff(documentId, fromVersion, toVersion) {
  return request(`/api/v1/admin/documents/${encodeURIComponent(documentId)}/diff`, {
    query: { from_version: fromVersion, to_version: toVersion },
  });
}

// Search the stored government text. "auto" uses meaning (embeddings) when
// they are configured and embedded, and always falls back to keyword search;
// the response says which mode actually ran.
export function searchDocuments({ query, programId, documentId, mode, limit } = {}) {
  return request("/api/v1/search", {
    method: "POST",
    body: {
      query,
      ...(programId ? { program_id: programId } : {}),
      ...(documentId ? { document_id: documentId } : {}),
      ...(mode ? { mode } : {}),
      ...(limit ? { limit } : {}),
    },
  });
}

// ---------------------------------------------------------------------------
// Admin applicant views
// ---------------------------------------------------------------------------
export function listApplicants({ limit = 50, offset = 0 } = {}) {
  return request("/api/v1/admin/applicants", { query: { limit, offset } });
}

export function getApplicant(applicantId) {
  return request(`/api/v1/admin/applicants/${encodeURIComponent(applicantId)}`);
}

// ---------------------------------------------------------------------------
// Applicant self-service
// ---------------------------------------------------------------------------
export function listApplicantPrograms() {
  return request("/api/v1/applicants/programs", { tokenContext: "applicant" });
}

export function listApplicantProfiles() {
  return request("/api/v1/applicants", { tokenContext: "applicant" });
}

export function listApplicantVisaSelections(applicantId) {
  return request(
    `/api/v1/applicants/${encodeURIComponent(applicantId)}/visa-selections`,
    { tokenContext: "applicant" }
  );
}

export function saveApplicantVisaSelection(applicantId, programId) {
  return request(
    `/api/v1/applicants/${encodeURIComponent(applicantId)}/visa-selections`,
    {
      method: "POST",
      body: { program_id: programId },
      tokenContext: "applicant",
    }
  );
}

export function listApplicantCases(applicantId) {
  return request(
    `/api/v1/applicants/${encodeURIComponent(applicantId)}/cases`,
    { tokenContext: "applicant" }
  );
}

export function updateCaseStatus(caseId, status) {
  return request(`/api/v1/cases/${encodeURIComponent(caseId)}/status`, {
    method: "PATCH",
    body: { status },
    tokenContext: "applicant",
  });
}

export function getCaseAssessment(caseId) {
  return request(`/api/v1/cases/${encodeURIComponent(caseId)}/assessment`, {
    tokenContext: "applicant",
  });
}

export function listCaseDocuments(caseId) {
  return request(`/api/v1/cases/${encodeURIComponent(caseId)}/documents`, {
    tokenContext: "applicant",
  });
}

export function uploadCaseDocument(caseId, file, checklistItemId) {
  return request(`/api/v1/cases/${encodeURIComponent(caseId)}/documents`, {
    method: "POST",
    rawBody: file,
    rawContentType: file.type || "application/octet-stream",
    query: {
      filename: file.name,
      checklist_item_id: checklistItemId,
    },
    tokenContext: "applicant",
  });
}

export function downloadCaseDocument(caseId, documentId) {
  return request(
    `/api/v1/cases/${encodeURIComponent(caseId)}/documents/${encodeURIComponent(documentId)}`,
    { responseType: "blob", tokenContext: "applicant" }
  );
}

export function reviewCaseDocument(caseId, documentId, reviewStatus, rejectionReason) {
  return request(
    `/api/v1/cases/${encodeURIComponent(caseId)}/documents/${encodeURIComponent(documentId)}/review`,
    {
      method: "PATCH",
      body: {
        review_status: reviewStatus,
        rejection_reason: rejectionReason,
      },
      tokenContext: "applicant",
    }
  );
}

export function listCaseChecklist(caseId) {
  return request(`/api/v1/cases/${encodeURIComponent(caseId)}/checklist`, {
    tokenContext: "applicant",
  });
}

export function generateCaseChecklist(caseId, assessmentId) {
  return request(`/api/v1/cases/${encodeURIComponent(caseId)}/checklist/generate`, {
    method: "POST",
    body: { assessment_id: assessmentId || undefined },
    tokenContext: "applicant",
  });
}

export function updateCaseChecklistItem(caseId, itemId, status, rejectionReason) {
  return request(
    `/api/v1/cases/${encodeURIComponent(caseId)}/checklist/${encodeURIComponent(itemId)}`,
    {
      method: "PATCH",
      body: { status, rejection_reason: rejectionReason },
      tokenContext: "applicant",
    }
  );
}

export function listCaseForms(caseId) {
  return request(`/api/v1/cases/${encodeURIComponent(caseId)}/forms`, {
    tokenContext: "applicant",
  });
}

export function saveCaseForm(caseId, templateId, answers) {
  return request(
    `/api/v1/cases/${encodeURIComponent(caseId)}/forms/${encodeURIComponent(templateId)}`,
    {
      method: "POST",
      body: { answers_json: answers },
      tokenContext: "applicant",
    }
  );
}

export function submitCaseForm(caseId, templateId) {
  return request(
    `/api/v1/cases/${encodeURIComponent(caseId)}/forms/${encodeURIComponent(templateId)}/submit`,
    { method: "POST", tokenContext: "applicant" }
  );
}

export function confirmCaseForm(caseId, templateId) {
  return request(
    `/api/v1/cases/${encodeURIComponent(caseId)}/forms/${encodeURIComponent(templateId)}/confirm`,
    { method: "POST", body: {}, tokenContext: "applicant" }
  );
}

export function reviewCaseForm(caseId, templateId, status, reviewNotes) {
  return request(
    `/api/v1/cases/${encodeURIComponent(caseId)}/forms/${encodeURIComponent(templateId)}/review`,
    {
      method: "POST",
      body: { status, review_notes: reviewNotes },
      tokenContext: "applicant",
    }
  );
}

export function downloadCasePackage(caseId) {
  return request(`/api/v1/cases/${encodeURIComponent(caseId)}/package`, {
    responseType: "blob",
    tokenContext: "applicant",
  });
}

export function listCaseActivities(tenantId, caseId) {
  return request(
    `/api/v1/tenants/${encodeURIComponent(tenantId)}/cases/${encodeURIComponent(caseId)}/activities`,
    { tokenContext: "applicant" }
  );
}

export function listTenants() {
  return request("/api/v1/tenants", { tokenContext: "applicant" });
}

export function createTenant(body) {
  return request("/api/v1/tenants", {
    method: "POST",
    body,
    tokenContext: "applicant",
  });
}

export function listTenantCases(tenantId) {
  return request(`/api/v1/tenants/${encodeURIComponent(tenantId)}/cases`, {
    tokenContext: "applicant",
  });
}

export function listUnassignedTenantCases(tenantId) {
  return request(
    `/api/v1/tenants/${encodeURIComponent(tenantId)}/unassigned-cases`,
    { tokenContext: "applicant" }
  );
}

export function createTenantCase(tenantId, body) {
  return request(`/api/v1/tenants/${encodeURIComponent(tenantId)}/cases`, {
    method: "POST",
    body,
    tokenContext: "applicant",
  });
}

export function assignTenantCase(tenantId, caseId, consultantId) {
  return request(
    `/api/v1/tenants/${encodeURIComponent(tenantId)}/cases/${encodeURIComponent(caseId)}/assign`,
    {
      method: "POST",
      body: { consultant_id: consultantId },
      tokenContext: "applicant",
    }
  );
}

export function getTenantDashboard(tenantId) {
  return request(`/api/v1/tenants/${encodeURIComponent(tenantId)}/dashboard`, {
    tokenContext: "applicant",
  });
}

export function listTenantApplicants(tenantId) {
  return request(`/api/v1/tenants/${encodeURIComponent(tenantId)}/applicants`, {
    tokenContext: "applicant",
  });
}

export function listTenantMembers(tenantId) {
  return request(`/api/v1/tenants/${encodeURIComponent(tenantId)}/members`, {
    tokenContext: "applicant",
  });
}

export function createTenantInvitation(tenantId, email, role) {
  return request(`/api/v1/tenants/${encodeURIComponent(tenantId)}/invitations`, {
    method: "POST",
    body: { email, role },
    tokenContext: "applicant",
  });
}

export function acceptTenantInvitation(token) {
  return request("/api/v1/tenants/invitations/accept", {
    method: "POST",
    body: { token },
    tokenContext: "applicant",
  });
}

export function listTenantFormTemplates(tenantId, programId) {
  return request(
    `/api/v1/tenants/${encodeURIComponent(tenantId)}/form-templates`,
    { query: { program_id: programId }, tokenContext: "applicant" }
  );
}

export function createTenantFormTemplate(tenantId, body) {
  return request(`/api/v1/tenants/${encodeURIComponent(tenantId)}/form-templates`, {
    method: "POST",
    body,
    tokenContext: "applicant",
  });
}

export function listTenantInvitations(tenantId) {
  return request(`/api/v1/tenants/${encodeURIComponent(tenantId)}/invitations`, {
    tokenContext: "applicant",
  });
}

export function createApplicantProfile(body) {
  return request("/api/v1/applicants", {
    method: "POST",
    body,
    tokenContext: "applicant",
  });
}

export function updateApplicantProfile(applicantId, body) {
  return request(`/api/v1/applicants/${encodeURIComponent(applicantId)}`, {
    method: "PATCH",
    body,
    tokenContext: "applicant",
  });
}

export function listApplicantAssessments(applicantId, limit = 20) {
  return request("/api/v1/assessments", {
    query: { applicant_id: applicantId, limit },
    tokenContext: "applicant",
  });
}

export function createApplicantAssessment(applicantId, programId) {
  return request("/api/v1/assessments", {
    method: "POST",
    body: { applicant_id: applicantId, program_id: programId },
    tokenContext: "applicant",
  });
}

export function getApplicantAssessment(assessmentId) {
  return request(`/api/v1/assessments/${encodeURIComponent(assessmentId)}`, {
    tokenContext: "applicant",
  });
}

export function answerApplicantGap(assessmentId, gapId, value) {
  return request(
    `/api/v1/assessments/${encodeURIComponent(assessmentId)}/gaps/${encodeURIComponent(gapId)}/answer`,
    {
      method: "POST",
      body: { value },
      tokenContext: "applicant",
    }
  );
}

// ---------------------------------------------------------------------------
// Scheduler
// ---------------------------------------------------------------------------
export function getScheduler() {
  return request("/api/v1/admin/scheduler");
}

export function updateScheduler(body) {
  return request("/api/v1/admin/scheduler", { method: "PUT", body });
}

// ---------------------------------------------------------------------------
// Source discovery
// ---------------------------------------------------------------------------
export function discoverSources(body) {
  return request("/api/v1/admin/sources/discover", { method: "POST", body });
}

export function listDiscoveredSources({ status, suggestedVisaCategory } = {}) {
  return request("/api/v1/admin/sources/discovered", {
    query: { status, suggested_visa_category: suggestedVisaCategory },
  });
}

export function approveDiscoveredSource(discoveredSourceId, body) {
  return request(
    `/api/v1/admin/sources/discovered/${encodeURIComponent(discoveredSourceId)}/approve`,
    { method: "POST", body }
  );
}

export function rejectDiscoveredSource(discoveredSourceId, reason) {
  return request(
    `/api/v1/admin/sources/discovered/${encodeURIComponent(discoveredSourceId)}/reject`,
    { method: "POST", body: { reason } }
  );
}
