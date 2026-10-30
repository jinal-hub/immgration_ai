import { useCallback, useEffect, useMemo, useState } from "react";
import * as api from "../api/client.js";
import "../styles/case-workflow.css";

const STAFF_ROLES = new Set(["OWNER", "CONSULTANT", "REVIEWER"]);
const STAFF_TRANSITIONS = {
  DRAFT: ["IN_PROGRESS"],
  IN_PROGRESS: ["NEEDS_INFO", "READY_FOR_REVIEW", "CLOSED"],
  NEEDS_INFO: ["IN_PROGRESS", "CLOSED"],
  READY_FOR_REVIEW: ["NEEDS_INFO", "CHANGES_REQUESTED", "APPROVED"],
  CHANGES_REQUESTED: ["IN_PROGRESS", "READY_FOR_REVIEW"],
  APPROVED: ["PACKAGE_READY", "CLOSED"],
  PACKAGE_READY: ["CLOSED"],
};
const APPLICANT_TRANSITIONS = {
  DRAFT: ["IN_PROGRESS"],
  IN_PROGRESS: ["READY_FOR_REVIEW"],
  NEEDS_INFO: ["IN_PROGRESS"],
  CHANGES_REQUESTED: ["IN_PROGRESS", "READY_FOR_REVIEW"],
};

function saveBlob(blob, filename) {
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function FormQuestion({ question, value, onChange }) {
  const id = `form-${question.key}`;
  if (question.type === "select") {
    return (
      <label className="portal-field" htmlFor={id}>
        <span>{question.label}{question.required ? " *" : ""}</span>
        <select
          id={id}
          required={question.required}
          value={value ?? ""}
          onChange={(event) => onChange(question.key, event.target.value)}
        >
          <option value="">Choose an option</option>
          {(question.options || []).map((option) => (
            <option key={option} value={option}>{option}</option>
          ))}
        </select>
      </label>
    );
  }
  if (question.type === "boolean") {
    return (
      <label className="portal-field" htmlFor={id}>
        <span>{question.label}{question.required ? " *" : ""}</span>
        <select
          id={id}
          required={question.required}
          value={value === true ? "true" : value === false ? "false" : ""}
          onChange={(event) => onChange(
            question.key,
            event.target.value === "" ? "" : event.target.value === "true"
          )}
        >
          <option value="">Choose an option</option>
          <option value="true">Yes</option>
          <option value="false">No</option>
        </select>
      </label>
    );
  }
  return (
    <label className="portal-field" htmlFor={id}>
      <span>{question.label}{question.required ? " *" : ""}</span>
      <input
        id={id}
        type={question.type === "number" ? "number" : question.type === "date" ? "date" : "text"}
        required={question.required}
        value={value ?? ""}
        onChange={(event) => onChange(
          question.key,
          question.type === "number" && event.target.value !== ""
            ? Number(event.target.value)
            : event.target.value
        )}
      />
    </label>
  );
}

export default function CaseWorkspace({
  caseRecord,
  tenantId,
  tenantRole,
  onCaseUpdated,
}) {
  const [currentCase, setCurrentCase] = useState(caseRecord);
  const [documents, setDocuments] = useState([]);
  const [checklist, setChecklist] = useState([]);
  const [answers, setAnswers] = useState([]);
  const [templates, setTemplates] = useState([]);
  const [assessment, setAssessment] = useState(null);
  const [activities, setActivities] = useState([]);
  const [formDrafts, setFormDrafts] = useState({});
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState("");
  const [rejectingDocument, setRejectingDocument] = useState(null);
  const [rejectionReason, setRejectionReason] = useState("");
  const isStaff = STAFF_ROLES.has(tenantRole);
  const isApplicant = tenantRole === "APPLICANT";

  useEffect(() => {
    setCurrentCase(caseRecord);
  }, [caseRecord]);

  const loadWorkspace = useCallback(async () => {
    if (!caseRecord?.case_id) return;
    setError("");
    const results = await Promise.allSettled([
      api.listCaseDocuments(caseRecord.case_id),
      api.listCaseChecklist(caseRecord.case_id),
      api.listCaseForms(caseRecord.case_id),
      api.listTenantFormTemplates(tenantId, caseRecord.program_id),
      api.getCaseAssessment(caseRecord.case_id),
      api.listCaseActivities(tenantId, caseRecord.case_id),
    ]);
    const requiredFailure = results.slice(0, 4).find((result) => result.status === "rejected");
    if (requiredFailure) {
      setError(requiredFailure.reason?.message || "Could not load this case.");
      return;
    }
    if (
      results[4].status === "rejected" &&
      results[4].reason?.status !== 404
    ) {
      setError(results[4].reason?.message || "Could not load the linked assessment.");
      return;
    }
    if (results[5].status === "rejected") {
      setError(results[5].reason?.message || "Could not load case activity.");
      return;
    }
    setDocuments(results[0].value || []);
    setChecklist(results[1].value || []);
    setAnswers(results[2].value || []);
    setTemplates(results[3].value || []);
    setAssessment(
      results[4].status === "fulfilled" ? results[4].value : null
    );
    setActivities(
      results[5].status === "fulfilled" ? results[5].value || [] : []
    );
    const nextDrafts = {};
    for (const answer of results[2].value || []) {
      nextDrafts[answer.template_id] = answer.answers_json || {};
    }
    setFormDrafts(nextDrafts);
  }, [caseRecord?.case_id, caseRecord?.program_id, tenantId]);

  useEffect(() => {
    loadWorkspace();
  }, [loadWorkspace]);

  const answerByTemplate = useMemo(
    () => new Map(answers.map((answer) => [answer.template_id, answer])),
    [answers]
  );

  async function runAction(action, successMessage) {
    setBusy(action);
    setError("");
    setNotice("");
    try {
      const result = await action();
      if (result?.case_id && result?.tenant_id && result?.program_id) {
        setCurrentCase(result);
        onCaseUpdated?.(result);
      }
      setNotice(successMessage);
      await loadWorkspace();
    } catch (actionError) {
      setError(actionError.message || "The requested action could not be completed.");
    } finally {
      setBusy("");
    }
  }

  function changeFormValue(templateId, key, value) {
    setFormDrafts((existing) => ({
      ...existing,
      [templateId]: { ...(existing[templateId] || {}), [key]: value },
    }));
  }

  async function saveForm(template) {
    const values = formDrafts[template.template_id] || {};
    await runAction(
      () => api.saveCaseForm(
        currentCase.case_id,
        template.template_id,
        Object.fromEntries(
          Object.entries(values).filter(([, value]) => value !== "" && value !== null)
        )
      ),
      "Form draft saved."
    );
  }

  async function submitForm(template) {
    const values = formDrafts[template.template_id] || {};
    await runAction(async () => {
      await api.saveCaseForm(
        currentCase.case_id,
        template.template_id,
        Object.fromEntries(
          Object.entries(values).filter(([, value]) => value !== "" && value !== null)
        )
      );
      return api.submitCaseForm(currentCase.case_id, template.template_id);
    }, "Form submitted for review.");
  }

  async function uploadFile(file, checklistItemId) {
    if (!file) return;
    await runAction(
      () => api.uploadCaseDocument(currentCase.case_id, file, checklistItemId),
      "Document uploaded to the private case workspace."
    );
  }

  async function downloadDocument(document) {
    setBusy(`download-${document.document_id}`);
    setError("");
    try {
      const blob = await api.downloadCaseDocument(
        currentCase.case_id,
        document.document_id
      );
      saveBlob(blob, document.filename);
    } catch (downloadError) {
      setError(downloadError.message || "Could not download this document.");
    } finally {
      setBusy("");
    }
  }

  const transitions = isApplicant
    ? APPLICANT_TRANSITIONS[currentCase.status] || []
    : isStaff
      ? STAFF_TRANSITIONS[currentCase.status] || []
      : [];

  return (
    <section className="portal-panel case-workspace" aria-label="Application case workspace">
      <div className="portal-section-heading">
        <div>
          <span className="portal-eyebrow">APPLICATION CASE</span>
          <h2>{currentCase.title || `Subclass ${currentCase.program_id}`}</h2>
        </div>
        <span className="case-status-pill">{currentCase.status.replaceAll("_", " ")}</span>
      </div>
      <p className="portal-panel-intro">
        Case {currentCase.case_id} · Program {currentCase.program_id}
      </p>
      {error && <p className="portal-form-error" role="alert">{error}</p>}
      {notice && <p className="portal-success-message" role="status">{notice}</p>}

      <div className="case-action-row">
        {transitions.map((status) => (
          <button
            className="portal-secondary-button case-action-button"
            disabled={Boolean(busy)}
            key={status}
            onClick={() => runAction(
              () => api.updateCaseStatus(currentCase.case_id, status),
              `Case moved to ${status.replaceAll("_", " ").toLowerCase()}.`
            )}
            type="button"
          >
            {status.replaceAll("_", " ")}
          </button>
        ))}
        <button
          className="portal-secondary-button case-action-button"
          disabled={Boolean(busy)}
          onClick={() => runAction(async () => {
            const blob = await api.downloadCasePackage(currentCase.case_id);
            saveBlob(blob, `case-${currentCase.case_id}-package.zip`);
          }, "Draft application package downloaded.")}
          type="button"
        >
          Download package
        </button>
      </div>
      <p className="case-disclaimer">
        Downloaded packages are preparation drafts, not official government forms or a visa decision.
        Only documents marked accepted are included as attachments.
      </p>

      <section className="case-workspace-section">
        <div className="portal-section-heading">
          <h3>Eligibility assessment</h3>
          {assessment && <span className="case-status-pill">{assessment.business_status}</span>}
        </div>
        {!currentCase.assessment_id ? (
          <p className="muted">No assessment is linked to this case.</p>
        ) : assessment ? (
          <>
            <p className="muted">
              {assessment.passed_count} passed · {assessment.failed_count} not met ·
              {" "}{assessment.unknown_count} need answers
              {assessment.ruleset_id ? ` · Ruleset ${assessment.ruleset_id}` : ""}
            </p>
            {assessment.gaps?.length > 0 && (
              <ul className="case-list">
                {assessment.gaps.map((gap) => (
                  <li key={gap.gap_id}>
                    <strong>{gap.field}</strong> — {gap.message}
                  </li>
                ))}
              </ul>
            )}
            {assessment.rule_results?.length > 0 && (
              <details className="case-details">
                <summary>Review {assessment.rule_results.length} evaluated rules</summary>
                <ul className="case-list">
                  {assessment.rule_results.map((rule) => (
                    <li key={rule.result_id}>
                      <strong>{rule.status}</strong> · {rule.message || rule.rule_id}
                      {rule.source_url && (
                        <a href={rule.source_url} rel="noreferrer" target="_blank"> Source</a>
                      )}
                    </li>
                  ))}
                </ul>
              </details>
            )}
          </>
        ) : (
          <p className="muted">Assessment details are unavailable for this case.</p>
        )}
      </section>

      <section className="case-workspace-section">
        <div className="portal-section-heading">
          <h3>Document checklist</h3>
          {isStaff && checklist.length === 0 && (
            <button
              className="portal-text-button"
              disabled={Boolean(busy)}
              onClick={() => runAction(
                () => api.generateCaseChecklist(currentCase.case_id, currentCase.assessment_id),
                "Case checklist generated."
              )}
              type="button"
            >
              Generate checklist
            </button>
          )}
        </div>
        {checklist.length === 0 ? (
          <p className="muted">
            {isStaff
              ? "Generate a workflow checklist. Prompts are not official government requirements."
              : "The consultant has not generated a checklist yet."}
          </p>
        ) : (
          <div className="case-checklist">
            {checklist.map((item) => (
              <article className="case-checklist-item" key={item.item_id}>
                <div>
                  <strong>{item.label}</strong>
                  <span className="case-status-pill">{item.status.replaceAll("_", " ")}</span>
                  {item.required && <small>Required in this workflow</small>}
                  {item.description && <p>{item.description}</p>}
                  {item.rejection_reason && <p className="portal-form-error">{item.rejection_reason}</p>}
                </div>
                {isApplicant && item.status !== "ACCEPTED" && (
                  <label className="case-upload-control">
                    <span>{item.status === "REJECTED" ? "Replace document" : "Upload evidence"}</span>
                    <input
                      accept=".pdf,.png,.jpg,.jpeg"
                      disabled={Boolean(busy)}
                      onChange={(event) => {
                        uploadFile(event.target.files?.[0], item.item_id);
                        event.target.value = "";
                      }}
                      type="file"
                    />
                  </label>
                )}
              </article>
            ))}
          </div>
        )}
        {isApplicant && (
          <label className="case-upload-control case-general-upload">
            <span>Upload another supporting document</span>
            <input
              accept=".pdf,.png,.jpg,.jpeg"
              disabled={Boolean(busy)}
              onChange={(event) => {
                uploadFile(event.target.files?.[0], null);
                event.target.value = "";
              }}
              type="file"
            />
          </label>
        )}
        <p className="case-disclaimer">
          Documents are stored privately. PDF, PNG, and JPEG files up to 10 MiB are accepted.
          Malware scanning is shown as not configured unless a scanner is connected.
        </p>
      </section>

      <section className="case-workspace-section">
        <h3>Documents</h3>
        {documents.length === 0 ? (
          <p className="muted">No documents uploaded yet.</p>
        ) : (
          <div className="case-document-list">
            {documents.map((document) => (
              <article className="case-document-row" key={document.document_id}>
                <div>
                  <strong>{document.filename}</strong>
                  <span className="case-status-pill">{document.review_status.replaceAll("_", " ")}</span>
                  <small>
                    Version {document.version} · {Math.ceil(document.size_bytes / 1024)} KiB ·
                    {" "}Scan: {document.scanner_status}
                  </small>
                  {document.rejection_reason && (
                    <p className="portal-form-error">{document.rejection_reason}</p>
                  )}
                  {document.scanner_detail && <small>{document.scanner_detail}</small>}
                </div>
                <div className="case-action-row">
                  <button
                    className="portal-text-button"
                    disabled={Boolean(busy)}
                    onClick={() => downloadDocument(document)}
                    type="button"
                  >
                    Download
                  </button>
                  {isStaff && document.review_status === "PENDING_REVIEW" && (
                    <>
                      <button
                        className="portal-text-button"
                        disabled={Boolean(busy)}
                        onClick={() => runAction(
                          () => api.reviewCaseDocument(
                            currentCase.case_id,
                            document.document_id,
                            "ACCEPTED"
                          ),
                          "Document accepted."
                        )}
                        type="button"
                      >
                        Accept
                      </button>
                      {rejectingDocument === document.document_id ? (
                        <>
                          <label className="portal-field case-rejection-field">
                            <span>Reason for rejection</span>
                            <input
                              value={rejectionReason}
                              onChange={(event) => setRejectionReason(event.target.value)}
                            />
                          </label>
                          <button
                            className="portal-text-button"
                            disabled={Boolean(busy) || !rejectionReason.trim()}
                            onClick={() => runAction(
                              () => api.reviewCaseDocument(
                                currentCase.case_id,
                                document.document_id,
                                "REJECTED",
                                rejectionReason.trim()
                              ),
                              "Document rejected with a reason."
                            ).then(() => {
                              setRejectingDocument(null);
                              setRejectionReason("");
                            })}
                            type="button"
                          >
                            Confirm rejection
                          </button>
                        </>
                      ) : (
                        <button
                          className="portal-text-button"
                          onClick={() => setRejectingDocument(document.document_id)}
                          type="button"
                        >
                          Reject
                        </button>
                      )}
                    </>
                  )}
                </div>
              </article>
            ))}
          </div>
        )}
      </section>

      <section className="case-workspace-section">
        <h3>Application forms</h3>
        {templates.length === 0 ? (
          <p className="muted">No published form template is available for this program yet.</p>
        ) : (
          <div className="case-form-list">
            {templates.map((template) => {
              const answer = answerByTemplate.get(template.template_id);
              const canEdit = isApplicant && (!answer || answer.status === "DRAFT");
              const values = formDrafts[template.template_id] || answer?.answers_json || {};
              return (
                <article className="case-form-card" key={template.template_id}>
                  <div className="portal-section-heading">
                    <div>
                      <strong>{template.title}</strong>
                      <small>{template.form_key} · Version {template.version}</small>
                    </div>
                    <span className="case-status-pill">{answer?.status || "NOT STARTED"}</span>
                  </div>
                  <p className="case-disclaimer">
                    This is a consultancy workflow form, not a verified government form.
                  </p>
                  <div className="portal-form-grid">
                    {(template.schema_json?.questions || []).map((question) => (
                      <FormQuestion
                        key={question.key}
                        question={question}
                        value={values[question.key]}
                        onChange={(key, value) => changeFormValue(
                          template.template_id,
                          key,
                          value
                        )}
                      />
                    ))}
                  </div>
                  {answer?.missing_fields?.length > 0 && (
                    <p className="portal-form-error">
                      Missing: {answer.missing_fields.join(", ")}
                    </p>
                  )}
                  {canEdit && (
                    <div className="case-action-row">
                      <button
                        className="portal-secondary-button case-action-button"
                        disabled={Boolean(busy)}
                        onClick={() => saveForm(template)}
                        type="button"
                      >
                        Save draft
                      </button>
                      <button
                        className="portal-primary-button case-action-button"
                        disabled={Boolean(busy)}
                        onClick={() => submitForm(template)}
                        type="button"
                      >
                        Submit for review
                      </button>
                    </div>
                  )}
                  {isApplicant && answer?.status === "SUBMITTED" && !answer.applicant_confirmed_at && (
                    <button
                      className="portal-secondary-button case-action-button"
                      disabled={Boolean(busy)}
                      onClick={() => runAction(
                        () => api.confirmCaseForm(currentCase.case_id, template.template_id),
                        "Form confirmation recorded."
                      )}
                      type="button"
                    >
                      Confirm these answers
                    </button>
                  )}
                  {isStaff && ["SUBMITTED", "REVIEWED"].includes(answer?.status) && (
                    <div className="case-action-row">
                      {answer.status === "SUBMITTED" && (
                        <button
                          className="portal-secondary-button case-action-button"
                          disabled={Boolean(busy)}
                          onClick={() => runAction(
                            () => api.reviewCaseForm(
                              currentCase.case_id,
                              template.template_id,
                              "REVIEWED"
                            ),
                            "Form marked reviewed."
                          )}
                          type="button"
                        >
                          Mark reviewed
                        </button>
                      )}
                      <button
                        className="portal-primary-button case-action-button"
                        disabled={Boolean(busy) || !answer.applicant_confirmed_at}
                        onClick={() => runAction(
                          () => api.reviewCaseForm(
                            currentCase.case_id,
                            template.template_id,
                            "APPROVED"
                          ),
                          "Applicant-confirmed form approved."
                        )}
                        type="button"
                      >
                        Approve form
                      </button>
                    </div>
                  )}
                  {answer?.review_notes && (
                    <p className="muted">Consultant notes: {answer.review_notes}</p>
                  )}
                </article>
              );
            })}
          </div>
        )}
      </section>

      {isStaff && (
        <section className="case-workspace-section">
          <h3>Case activity</h3>
          {activities.length === 0 ? (
            <p className="muted">No audit events recorded.</p>
          ) : (
            <ol className="case-activity-list">
              {activities.map((activity) => (
                <li key={activity.audit_id}>
                  <strong>{activity.action.replaceAll("_", " ")}</strong>
                  <small>
                    {activity.user_name || activity.user_id} ·
                    {" "}{new Date(activity.created_at).toLocaleString()}
                  </small>
                </li>
              ))}
            </ol>
          )}
        </section>
      )}
    </section>
  );
}
