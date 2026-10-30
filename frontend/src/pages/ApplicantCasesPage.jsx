import { useCallback, useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { useApplicantAuth } from "../auth/ApplicantAuthContext.jsx";
import CaseWorkspace from "../components/CaseWorkspace.jsx";
import TenantInvitationAcceptPanel from "../components/TenantInvitationAcceptPanel.jsx";
import * as api from "../api/client.js";

export default function ApplicantCasesPage() {
  const { user } = useApplicantAuth();
  const [profiles, setProfiles] = useState([]);
  const [tenants, setTenants] = useState([]);
  const [programs, setPrograms] = useState([]);
  const [assessments, setAssessments] = useState([]);
  const [cases, setCases] = useState([]);
  const [selectedCaseId, setSelectedCaseId] = useState("");
  const [tenantId, setTenantId] = useState("");
  const [programId, setProgramId] = useState("");
  const [assessmentId, setAssessmentId] = useState("");
  const [title, setTitle] = useState("");
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  const profile = profiles[0] || null;
  const selectedCase = cases.find((item) => item.case_id === selectedCaseId) || null;
  const applicantTenants = useMemo(
    () => tenants.filter((tenant) => tenant.role === "APPLICANT"),
    [tenants]
  );
  const activePrograms = useMemo(
    () => programs.filter((program) => program.status === "ACTIVE"),
    [programs]
  );
  const matchingAssessments = useMemo(
    () => assessments.filter((assessment) => assessment.program_id === programId),
    [assessments, programId]
  );
  const selectedTenant = tenants.find((tenant) => tenant.tenant_id === selectedCase?.tenant_id);

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const [nextProfiles, nextTenants, nextPrograms] = await Promise.all([
        api.listApplicantProfiles(),
        api.listTenants(),
        api.listApplicantPrograms(),
      ]);
      setProfiles(nextProfiles || []);
      setTenants(nextTenants || []);
      setPrograms(nextPrograms || []);
      const availableApplicantTenants = (nextTenants || []).filter(
        (tenant) => tenant.role === "APPLICANT"
      );
      setTenantId((current) => (
        availableApplicantTenants.some((tenant) => tenant.tenant_id === current)
          ? current
          : availableApplicantTenants[0]?.tenant_id || ""
      ));
      if (nextProfiles?.[0]) {
        const [nextCases, assessmentResponse] = await Promise.all([
          api.listApplicantCases(nextProfiles[0].applicant_id),
          api.listApplicantAssessments(nextProfiles[0].applicant_id),
        ]);
        setCases(nextCases || []);
        setAssessments(assessmentResponse?.assessments || []);
        setSelectedCaseId((current) => (
          (nextCases || []).some((item) => item.case_id === current)
            ? current
            : (nextCases || [])[0]?.case_id || ""
        ));
      } else {
        setCases([]);
        setAssessments([]);
        setSelectedCaseId("");
      }
    } catch (loadError) {
      setError(loadError.message || "Could not load applicant cases.");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  async function createCase(event) {
    event.preventDefault();
    if (!profile || !tenantId || !programId) return;
    setBusy(true);
    setError("");
    setNotice("");
    try {
      const created = await api.createTenantCase(tenantId, {
        applicant_id: profile.applicant_id,
        program_id: programId,
        assessment_id: assessmentId || undefined,
        title: title.trim() || undefined,
      });
      setCases((existing) => [created, ...existing]);
      setSelectedCaseId(created.case_id);
      setTitle("");
      setNotice("Application case created.");
    } catch (createError) {
      setError(createError.message || "Could not create an application case.");
    } finally {
      setBusy(false);
    }
  }

  function updateCase(updated) {
    setCases((existing) => existing.map((item) => (
      item.case_id === updated.case_id ? { ...item, ...updated } : item
    )));
  }

  if (loading) {
    return <p className="portal-loading" role="status">Loading your application workspace…</p>;
  }

  return (
    <div className="applicant-cases-page">
      <section className="portal-hero case-hero">
        <div className="portal-hero-copy">
          <span className="portal-eyebrow">YOUR APPLICATIONS</span>
          <h1>Move from eligibility to an organized case.</h1>
          <p>
            Track case status, answer dynamic forms, submit documents, and review consultant feedback.
            Eligibility results remain separate from application readiness.
          </p>
        </div>
        <div className="portal-hero-stat">
          <span className="portal-hero-stat-icon" aria-hidden="true">▤</span>
          <div><strong>{cases.length}</strong><small>Application cases</small></div>
        </div>
      </section>

      {error && <p className="portal-form-error" role="alert">{error}</p>}
      {notice && <p className="portal-success-message" role="status">{notice}</p>}

      {!profile && (
        <section className="portal-panel">
          <h2>Create your profile first</h2>
          <p className="portal-panel-intro">
            Application cases use your saved profile and assessment history.
          </p>
          <Link className="portal-primary-button case-action-button" to="/portal">Complete profile</Link>
        </section>
      )}

      <TenantInvitationAcceptPanel onAccepted={load} />

      {profile && (
        <>
          <section className="portal-panel">
            <div className="portal-section-heading">
              <div>
                <span className="portal-eyebrow">START A CASE</span>
                <h2>Create an application case</h2>
              </div>
            </div>
            {applicantTenants.length === 0 ? (
              <p className="portal-panel-intro">
                You need an active applicant membership in a consultancy to create a case.
                Accept an invitation above or ask your consultancy to invite this account.
              </p>
            ) : activePrograms.length === 0 ? (
              <p className="portal-panel-intro">No active visa programs are currently available.</p>
            ) : (
              <form className="case-create-form" onSubmit={createCase}>
                <label className="portal-field" htmlFor="case-tenant">
                  <span>Consultancy</span>
                  <select
                    id="case-tenant"
                    required
                    value={tenantId}
                    onChange={(event) => setTenantId(event.target.value)}
                  >
                    {applicantTenants.map((tenant) => (
                      <option key={tenant.tenant_id} value={tenant.tenant_id}>{tenant.name}</option>
                    ))}
                  </select>
                </label>
                <label className="portal-field" htmlFor="case-program">
                  <span>Visa program</span>
                  <select
                    id="case-program"
                    required
                    value={programId}
                    onChange={(event) => {
                      setProgramId(event.target.value);
                      setAssessmentId("");
                    }}
                  >
                    <option value="">Choose an active program</option>
                    {activePrograms.map((program) => (
                      <option key={program.program_id} value={program.program_id}>
                        {program.name} · {program.program_id}
                      </option>
                    ))}
                  </select>
                </label>
                <label className="portal-field" htmlFor="case-assessment">
                  <span>Link an eligibility assessment (optional)</span>
                  <select
                    id="case-assessment"
                    value={assessmentId}
                    onChange={(event) => setAssessmentId(event.target.value)}
                  >
                    <option value="">No linked assessment</option>
                    {matchingAssessments.map((assessment) => (
                      <option key={assessment.assessment_id} value={assessment.assessment_id}>
                        {assessment.business_status} · {new Date(assessment.evaluated_at).toLocaleDateString()}
                      </option>
                    ))}
                  </select>
                </label>
                <label className="portal-field" htmlFor="case-title">
                  <span>Case name (optional)</span>
                  <input
                    id="case-title"
                    maxLength={200}
                    value={title}
                    onChange={(event) => setTitle(event.target.value)}
                  />
                </label>
                <button
                  className="portal-primary-button case-action-button"
                  disabled={busy || !tenantId || !programId}
                  type="submit"
                >
                  {busy ? "Creating…" : "Create application case"}
                </button>
              </form>
            )}
          </section>

          <section className="portal-panel">
            <div className="portal-section-heading">
              <div>
                <span className="portal-eyebrow">CASE HISTORY</span>
                <h2>Your application cases</h2>
              </div>
            </div>
            {cases.length === 0 ? (
              <p className="portal-empty-state">
                No application cases yet. Join a consultancy and start one when you are ready.
              </p>
            ) : (
              <div className="case-card-list">
                {cases.map((item) => (
                  <button
                    aria-pressed={selectedCaseId === item.case_id}
                    className={`case-card${selectedCaseId === item.case_id ? " is-selected" : ""}`}
                    key={item.case_id}
                    onClick={() => setSelectedCaseId(item.case_id)}
                    type="button"
                  >
                    <span>
                      <strong>{item.title || item.program_id}</strong>
                      <small>{item.tenant_name || tenants.find((tenant) => tenant.tenant_id === item.tenant_id)?.name}</small>
                      <small>Created {new Date(item.created_at).toLocaleDateString()}</small>
                    </span>
                    <span className="case-status-pill">{item.status.replaceAll("_", " ")}</span>
                  </button>
                ))}
              </div>
            )}
          </section>
        </>
      )}

      {selectedCase && selectedTenant && (
        <CaseWorkspace
          caseRecord={selectedCase}
          onCaseUpdated={updateCase}
          tenantId={selectedTenant.tenant_id}
          tenantRole={selectedTenant.role}
        />
      )}
      {!selectedCase && cases.length > 0 && (
        <p className="portal-panel-intro">Select a case above to open its workspace.</p>
      )}
      {user?.role === "applicant" && (
        <p className="case-footer-link">
          Are you a consultant or agency administrator? <Link to="/consultancy">Open consultancy portal</Link>.
        </p>
      )}
    </div>
  );
}
