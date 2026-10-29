// Fetch wrapper: base URL from VITE_API_BASE_URL, attaches the admin JWT,
// normalizes errors, and treats ANY 401 as "log out now" - there is no
// refresh endpoint, so a 401 can only mean the session is no longer valid.
const API_BASE = import.meta.env.VITE_API_BASE_URL || "http://localhost:8000";
const TOKEN_KEY = "ik_admin_token";

let unauthorizedHandler = null;

// Registered once by AuthContext on mount. Kept outside React so every
// request (even ones fired from outside a component) can trigger logout.
export function setUnauthorizedHandler(fn) {
  unauthorizedHandler = fn;
}

export function getToken() {
  try {
    return sessionStorage.getItem(TOKEN_KEY);
  } catch {
    return null;
  }
}

export function setToken(token) {
  try {
    sessionStorage.setItem(TOKEN_KEY, token);
  } catch {
    /* sessionStorage unavailable (private mode, etc.) - session just won't persist */
  }
}

export function clearToken() {
  try {
    sessionStorage.removeItem(TOKEN_KEY);
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

async function request(path, { method = "GET", body, auth = true, query } = {}) {
  const headers = {};
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (auth) {
    const token = getToken();
    if (token) headers.Authorization = `Bearer ${token}`;
  }

  let res;
  try {
    res = await fetch(`${API_BASE}${path}${toQuery(query)}`, {
      method,
      headers,
      body: body !== undefined ? JSON.stringify(body) : undefined,
    });
  } catch {
    throw new ApiError("Could not reach the server. Check your connection and try again.", 0, null);
  }

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
    if (res.status === 401 && auth && unauthorizedHandler) {
      unauthorizedHandler();
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

export function getMe() {
  return request("/api/v1/auth/me");
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
// Applicants (read-only)
// ---------------------------------------------------------------------------
export function listApplicants({ limit = 50, offset = 0 } = {}) {
  return request("/api/v1/admin/applicants", { query: { limit, offset } });
}

export function getApplicant(applicantId) {
  return request(`/api/v1/admin/applicants/${encodeURIComponent(applicantId)}`);
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
