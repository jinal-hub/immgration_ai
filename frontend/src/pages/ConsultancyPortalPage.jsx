import { useCallback, useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { useApplicantAuth } from "../auth/ApplicantAuthContext.jsx";
import CaseWorkspace from "../components/CaseWorkspace.jsx";
import TenantInvitationAcceptPanel from "../components/TenantInvitationAcceptPanel.jsx";
import * as api from "../api/client.js";

const EMPTY_FORM_SCHEMA = JSON.stringify({
  questions: [
    { key: "full_name", label: "Full legal name", type: "text", required: true },
    { key: "date_of_birth", label: "Date of birth", type: "date", required: true },
  ],
}, null, 2);

function slugify(value) {
  return value
    .toLowerCase()
    .trim()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-|-$/g, "")
    .slice(0, 120);
}

export default function ConsultancyPortalPage() {
  const { user } = useApplicantAuth();
  const [tenants, setTenants] = useState([]);
  const [selectedTenantId, setSelectedTenantId] = useState("");
  const selectedTenant = tenants.find((tenant) => tenant.tenant_id === selectedTenantId);
  const [dashboard, setDashboard] = useState(null);
  const [cases, setCases] = useState([]);
  const [unassignedCases, setUnassignedCases] = useState([]);
  const [applicants, setApplicants] = useState([]);
  const [members, setMembers] = useState([]);
  const [invitations, setInvitations] = useState([]);
  const [templates, setTemplates] = useState([]);
  const [programs, setPrograms] = useState([]);
  const [selectedCaseId, setSelectedCaseId] = useState("");
  const [tenantName, setTenantName] = useState("");
  const [inviteEmail, setInviteEmail] = useState("");
  const [inviteRole, setInviteRole] = useState("CONSULTANT");
  const [createdInvitation, setCreatedInvitation] = useState(null);
  const [newApplicantId, setNewApplicantId] = useState("");
  const [newProgramId, setNewProgramId] = useState("");
  const [newCaseTitle, setNewCaseTitle] = useState("");
  const [templateKey, setTemplateKey] = useState("");
  const [templateTitle, setTemplateTitle] = useState("");
  const [templateVersion, setTemplateVersion] = useState("1");
  const [templateProgram, setTemplateProgram] = useState("");
  const [templateSchema, setTemplateSchema] = useState(EMPTY_FORM_SCHEMA);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  const selectedCase = cases.find((item) => item.case_id === selectedCaseId) || null;
  const assignedPeople = useMemo(
    () => members.filter((member) => ["OWNER", "CONSULTANT", "REVIEWER"].includes(member.role)),
    [members]
  );
  const applicantById = useMemo(
    () => new Map(applicants.map((applicant) => [applicant.applicant_id, applicant])),
    [applicants]
  );

  const loadTenants = useCallback(async () => {
    try {
      const result = await api.listTenants();
      setTenants(result || []);
      setSelectedTenantId((current) => (
        (result || []).some((tenant) => tenant.tenant_id === current)
          ? current
          : (result || [])[0]?.tenant_id || ""
      ));
      return result || [];
    } catch (loadError) {
      setError(loadError.message || "Could not load consultancy memberships.");
      return null;
    }
  }, []);

  useEffect(() => {
    let active = true;
    Promise.all([api.listApplicantPrograms(), loadTenants()])
      .then(([nextPrograms]) => {
        if (active) setPrograms(nextPrograms || []);
      })
      .catch((loadError) => {
        if (active) setError(loadError.message || "Could not load consultancy portal.");
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => { active = false; };
  }, [loadTenants]);

  const loadTenantData = useCallback(async () => {
    if (!selectedTenant) {
      setDashboard(null);
      setCases([]);
      setUnassignedCases([]);
      setApplicants([]);
      setMembers([]);
      setInvitations([]);
      setTemplates([]);
      setSelectedCaseId("");
      return;
    }
    if (selectedTenant.role === "APPLICANT") {
      setError("Applicant memberships use the applicant case workspace.");
      return;
    }
    setError("");
    setLoading(true);
    try {
      const requests = [
        api.getTenantDashboard(selectedTenant.tenant_id),
        api.listTenantCases(selectedTenant.tenant_id),
        api.listTenantApplicants(selectedTenant.tenant_id),
        api.listTenantMembers(selectedTenant.tenant_id),
        api.listTenantFormTemplates(selectedTenant.tenant_id),
      ];
      if (selectedTenant.role === "CONSULTANT") {
        requests.push(api.listUnassignedTenantCases(selectedTenant.tenant_id));
      }
      if (selectedTenant.role === "OWNER") {
        requests.push(api.listTenantInvitations(selectedTenant.tenant_id));
      }
      const results = await Promise.all(requests);
      const [nextDashboard, nextCases, nextApplicants, nextMembers, nextTemplates] = results;
      const nextUnassigned = selectedTenant.role === "CONSULTANT" ? results[5] : [];
      const nextInvitations = selectedTenant.role === "OWNER"
        ? results[selectedTenant.role === "CONSULTANT" ? 6 : 5]
        : [];
      setDashboard(nextDashboard);
      setCases(nextCases || []);
      setApplicants(nextApplicants || []);
      setMembers(nextMembers || []);
      setTemplates(nextTemplates || []);
      setUnassignedCases(nextUnassigned || []);
      setInvitations(nextInvitations || []);
      setSelectedCaseId((current) => (
        (nextCases || []).some((item) => item.case_id === current)
          ? current
          : (nextCases || [])[0]?.case_id || ""
      ));
      setNewApplicantId((current) => (
        (nextApplicants || []).some((item) => item.applicant_id === current)
          ? current
          : (nextApplicants || [])[0]?.applicant_id || ""
      ));
    } catch (loadError) {
      setError(loadError.message || "Could not load tenant cases and workload.");
    } finally {
      setLoading(false);
    }
  }, [selectedTenant]);

  useEffect(() => {
    loadTenantData();
  }, [loadTenantData]);

  async function createTenant(event) {
    event.preventDefault();
    const slug = slugify(tenantName);
    if (slug.length < 3) {
      setError("Enter a consultancy name that produces a slug of at least 3 characters.");
      return;
    }
    setBusy("tenant");
    setError("");
    try {
      const created = await api.createTenant({ name: tenantName.trim(), slug });
      setTenants((current) => [...current, created]);
      setSelectedTenantId(created.tenant_id);
      setTenantName("");
      setNotice("Consultancy workspace created. You are its tenant owner.");
    } catch (createError) {
      setError(createError.message || "Could not create consultancy.");
    } finally {
      setBusy("");
    }
  }

  async function createInvitation(event) {
    event.preventDefault();
    setBusy("invitation");
    setError("");
    setNotice("");
    setCreatedInvitation(null);
    try {
      const invitation = await api.createTenantInvitation(
        selectedTenant.tenant_id,
        inviteEmail,
        inviteRole
      );
      setCreatedInvitation(invitation);
      setInviteEmail("");
      setNotice("Invitation created. Copy the token now; it is only shown once.");
      await loadTenantData();
    } catch (inviteError) {
      setError(inviteError.message || "Could not create invitation.");
    } finally {
      setBusy("");
    }
  }

  async function createCase(event) {
    event.preventDefault();
    setBusy("case");
    setError("");
    setNotice("");
    try {
      const created = await api.createTenantCase(selectedTenant.tenant_id, {
        applicant_id: newApplicantId,
        program_id: newProgramId,
        title: newCaseTitle.trim() || undefined,
      });
      setCases((current) => [created, ...current]);
      setSelectedCaseId(created.case_id);
      setNewCaseTitle("");
      setNotice("Application case created.");
    } catch (createError) {
      setError(createError.message || "Could not create application case.");
    } finally {
      setBusy("");
    }
  }

  async function assignCase(caseId, consultantId) {
    setBusy(`assign-${caseId}`);
    setError("");
    setNotice("");
    try {
      const updated = await api.assignTenantCase(
        selectedTenant.tenant_id,
        caseId,
        consultantId
      );
      setCases((current) => current.map((item) => (
        item.case_id === caseId ? { ...item, ...updated } : item
      )));
      setNotice("Case assignment updated.");
      await loadTenantData();
    } catch (assignError) {
      setError(assignError.message || "Could not assign this case.");
    } finally {
      setBusy("");
    }
  }

  async function claimCase(caseId) {
    try {
      const response = await api.getMe("applicant");
      await assignCase(caseId, response.user_id);
    } catch (claimError) {
      setError(claimError.message || "Could not claim this case.");
    }
  }

  async function createTemplate(event) {
    event.preventDefault();
    setBusy("template");
    setError("");
    setNotice("");
    try {
      const schema = JSON.parse(templateSchema);
      await api.createTenantFormTemplate(selectedTenant.tenant_id, {
        form_key: templateKey.trim(),
        version: Number(templateVersion),
        title: templateTitle.trim(),
        program_id: templateProgram || undefined,
        schema_json: schema,
      });
      setTemplateKey("");
      setTemplateTitle("");
      setTemplateVersion("1");
      setNotice("Versioned form template published.");
      await loadTenantData();
    } catch (templateError) {
      setError(templateError.message || "Check the form JSON and try again.");
    } finally {
      setBusy("");
    }
  }

  function updateCase(updated) {
    setCases((current) => current.map((item) => (
      item.case_id === updated.case_id ? { ...item, ...updated } : item
    )));
  }

  const activePrograms = programs.filter((program) => program.status === "ACTIVE");
  if (loading) {
    return <p className="portal-loading" role="status">Loading consultancy workspace…</p>;
  }

  return (
    <div className="consultancy-portal-page">
      <section className="portal-hero case-hero">
        <div className="portal-hero-copy">
          <span className="portal-eyebrow">CONSULTANCY WORKSPACE</span>
          <h1>Coordinate applicant cases with clear ownership.</h1>
          <p>
            Tenant memberships, assigned workloads, assessments, documents, forms, and review history
            stay within the consultancy workspace.
          </p>
        </div>
        <div className="portal-hero-stat">
          <span className="portal-hero-stat-icon" aria-hidden="true">◫</span>
          <div><strong>{dashboard?.case_count ?? cases.length}</strong><small>Visible cases</small></div>
        </div>
      </section>

      {error && <p className="portal-form-error" role="alert">{error}</p>}
      {notice && <p className="portal-success-message" role="status">{notice}</p>}
      <TenantInvitationAcceptPanel onAccepted={loadTenants} />

      {tenants.length > 0 && (
        <section className="portal-panel">
          <div className="portal-section-heading">
            <div>
              <span className="portal-eyebrow">TENANT</span>
              <h2>Select consultancy</h2>
            </div>
            <span className="case-status-pill">{selectedTenant?.role || "No role"}</span>
          </div>
          <label className="portal-field" htmlFor="consultancy-tenant-select">
            <span>Workspace</span>
            <select
              id="consultancy-tenant-select"
              value={selectedTenantId}
              onChange={(event) => setSelectedTenantId(event.target.value)}
            >
              {tenants.map((tenant) => (
                <option key={tenant.tenant_id} value={tenant.tenant_id}>
                  {tenant.name} · {tenant.role}
                </option>
              ))}
            </select>
          </label>
          <form className="case-create-form case-new-tenant-form" onSubmit={createTenant}>
            <label className="portal-field" htmlFor="consultancy-name-existing">
              <span>Create another consultancy workspace</span>
              <input
                id="consultancy-name-existing"
                maxLength={200}
                required
                value={tenantName}
                onChange={(event) => setTenantName(event.target.value)}
              />
            </label>
            <button className="portal-secondary-button case-action-button" disabled={busy === "tenant"} type="submit">
              {busy === "tenant" ? "Creating…" : "Create another tenant"}
            </button>
          </form>
          {selectedTenant?.role === "APPLICANT" && (
            <p className="portal-panel-intro">
              This membership is applicant-scoped. <Link to="/portal/cases">Open applicant cases</Link>.
            </p>
          )}
        </section>
      )}

      {tenants.length === 0 && (
        <section className="portal-panel">
          <span className="portal-eyebrow">TENANT LIFECYCLE</span>
          <h2>Create a consultancy workspace</h2>
          <p className="portal-panel-intro">
            Creating a tenant gives this account the owner role only in that tenant. Platform administrator
            access remains separate.
          </p>
          <form className="case-create-form" onSubmit={createTenant}>
            <label className="portal-field" htmlFor="consultancy-name">
              <span>Consultancy name</span>
              <input
                id="consultancy-name"
                maxLength={200}
                required
                value={tenantName}
                onChange={(event) => setTenantName(event.target.value)}
              />
            </label>
            <button className="portal-primary-button case-action-button" disabled={busy === "tenant"} type="submit">
              {busy === "tenant" ? "Creating…" : "Create tenant"}
            </button>
          </form>
        </section>
      )}

      {selectedTenant && selectedTenant.role !== "APPLICANT" && (
        <>
          <section className="portal-panel">
            <span className="portal-eyebrow">WORKLOAD</span>
            <h2>Case dashboard</h2>
            <div className="case-metric-grid">
              <div><strong>{dashboard?.case_count ?? 0}</strong><span>Visible cases</span></div>
              {Object.entries(dashboard?.by_status || {}).map(([status, count]) => (
                <div key={status}><strong>{count}</strong><span>{status.replaceAll("_", " ")}</span></div>
              ))}
            </div>
            {dashboard?.consultant_workload?.length > 0 && (
              <div className="case-workload-list">
                <h3>Consultant workload</h3>
                {dashboard.consultant_workload.map((row) => {
                  const person = members.find((member) => member.user_id === row.consultant_id);
                  return (
                    <p key={row.consultant_id}>
                      {person?.full_name || person?.email || row.consultant_id}
                      <span>{row.case_count} cases</span>
                    </p>
                  );
                })}
              </div>
            )}
          </section>

          {selectedTenant.role === "OWNER" && (
            <section className="portal-panel">
              <span className="portal-eyebrow">TEAM ACCESS</span>
              <h2>Invite a consultant or applicant</h2>
              <form className="case-create-form" onSubmit={createInvitation}>
                <label className="portal-field" htmlFor="invite-email">
                  <span>Email address</span>
                  <input
                    autoComplete="email"
                    id="invite-email"
                    required
                    type="email"
                    value={inviteEmail}
                    onChange={(event) => setInviteEmail(event.target.value)}
                  />
                </label>
                <label className="portal-field" htmlFor="invite-role">
                  <span>Tenant role</span>
                  <select
                    id="invite-role"
                    value={inviteRole}
                    onChange={(event) => setInviteRole(event.target.value)}
                  >
                    <option value="CONSULTANT">Consultant</option>
                    <option value="REVIEWER">Reviewer</option>
                    <option value="APPLICANT">Applicant</option>
                  </select>
                </label>
                <button className="portal-primary-button case-action-button" disabled={busy === "invitation"} type="submit">
                  {busy === "invitation" ? "Creating…" : "Create invitation"}
                </button>
              </form>
              {createdInvitation?.invitation_token && (
                <div className="case-invitation-token" role="status">
                  <strong>Copy this one-time token now</strong>
                  <code>{createdInvitation.invitation_token}</code>
                  <small>It is not stored in readable form and will not be shown again.</small>
                </div>
              )}
              {invitations.length > 0 && (
                <div className="case-invitation-list">
                  <h3>Invitation history</h3>
                  {invitations.map((invitation) => (
                    <p key={invitation.invitation_id}>
                      {invitation.email} · {invitation.role} · {invitation.status}
                    </p>
                  ))}
                </div>
              )}
              {members.length > 0 && (
                <div className="case-member-list">
                  <h3>Active members</h3>
                  {members.map((member) => (
                    <p key={member.membership_id}>
                      {member.full_name || member.email}<span>{member.role}</span>
                    </p>
                  ))}
                </div>
              )}
            </section>
          )}

          {(selectedTenant.role === "OWNER" || selectedTenant.role === "CONSULTANT") && (
            <section className="portal-panel">
              <span className="portal-eyebrow">CASE MANAGEMENT</span>
              <h2>Create a case for an assigned applicant</h2>
              {applicants.length === 0 || activePrograms.length === 0 ? (
                <p className="portal-panel-intro">
                  {applicants.length === 0
                    ? "Invite an applicant to this tenant before opening a case."
                    : "No active immigration programs are available."}
                </p>
              ) : (
                <form className="case-create-form" onSubmit={createCase}>
                  <label className="portal-field" htmlFor="case-applicant">
                    <span>Applicant</span>
                    <select
                      id="case-applicant"
                      value={newApplicantId}
                      onChange={(event) => setNewApplicantId(event.target.value)}
                    >
                      {applicants.map((applicant) => (
                        <option key={applicant.applicant_id} value={applicant.applicant_id}>
                          {applicant.full_name || `${applicant.first_name || ""} ${applicant.last_name || ""}`.trim() || applicant.email}
                        </option>
                      ))}
                    </select>
                  </label>
                  <label className="portal-field" htmlFor="case-program-staff">
                    <span>Visa program</span>
                    <select
                      id="case-program-staff"
                      required
                      value={newProgramId}
                      onChange={(event) => setNewProgramId(event.target.value)}
                    >
                      <option value="">Choose active program</option>
                      {activePrograms.map((program) => (
                        <option key={program.program_id} value={program.program_id}>
                          {program.name} · {program.program_id}
                        </option>
                      ))}
                    </select>
                  </label>
                  <label className="portal-field" htmlFor="case-title-staff">
                    <span>Case title (optional)</span>
                    <input
                      id="case-title-staff"
                      maxLength={200}
                      value={newCaseTitle}
                      onChange={(event) => setNewCaseTitle(event.target.value)}
                    />
                  </label>
                  <button
                    className="portal-primary-button case-action-button"
                    disabled={busy === "case" || !newApplicantId || !newProgramId}
                    type="submit"
                  >
                    {busy === "case" ? "Creating…" : "Create application case"}
                  </button>
                </form>
              )}
            </section>
          )}

          {selectedTenant.role === "OWNER" && (
            <section className="portal-panel">
              <span className="portal-eyebrow">VERSIONED FORM SCHEMAS</span>
              <h2>Publish a dynamic application form</h2>
              <p className="portal-panel-intro">
                These are consultancy workflow templates. They must not be represented as official government forms
                unless independently verified.
              </p>
              <form className="case-create-form" onSubmit={createTemplate}>
                <label className="portal-field" htmlFor="template-key">
                  <span>Form key</span>
                  <input id="template-key" required value={templateKey} onChange={(event) => setTemplateKey(event.target.value)} />
                </label>
                <label className="portal-field" htmlFor="template-title">
                  <span>Title</span>
                  <input id="template-title" required value={templateTitle} onChange={(event) => setTemplateTitle(event.target.value)} />
                </label>
                <label className="portal-field" htmlFor="template-version">
                  <span>Version</span>
                  <input
                    id="template-version"
                    min="1"
                    required
                    type="number"
                    value={templateVersion}
                    onChange={(event) => setTemplateVersion(event.target.value)}
                  />
                </label>
                <label className="portal-field" htmlFor="template-program">
                  <span>Program (optional)</span>
                  <select id="template-program" value={templateProgram} onChange={(event) => setTemplateProgram(event.target.value)}>
                    <option value="">Available to all programs</option>
                    {activePrograms.map((program) => (
                      <option key={program.program_id} value={program.program_id}>{program.name}</option>
                    ))}
                  </select>
                </label>
                <label className="portal-field case-schema-field" htmlFor="template-schema">
                  <span>Question schema JSON</span>
                  <textarea
                    id="template-schema"
                    required
                    rows={9}
                    value={templateSchema}
                    onChange={(event) => setTemplateSchema(event.target.value)}
                  />
                </label>
                <button className="portal-primary-button case-action-button" disabled={busy === "template"} type="submit">
                  {busy === "template" ? "Publishing…" : "Publish form template"}
                </button>
              </form>
              {templates.length > 0 && (
                <div className="case-template-list">
                  <h3>Published templates</h3>
                  {templates.map((template) => (
                    <p key={template.template_id}>
                      {template.title} · v{template.version} · {template.program_id || "All programs"}
                    </p>
                  ))}
                </div>
              )}
            </section>
          )}

          <section className="portal-panel">
            <div className="portal-section-heading">
              <div>
                <span className="portal-eyebrow">CASE QUEUE</span>
                <h2>Application cases</h2>
              </div>
              <span className="portal-progress-count">{cases.length} visible</span>
            </div>
            {cases.length === 0 ? (
              <p className="portal-empty-state">No cases are currently visible to this tenant role.</p>
            ) : (
              <div className="case-card-list">
                {cases.map((item) => {
                  const applicant = applicantById.get(item.applicant_id);
                  const assigned = members.find((member) => member.user_id === item.assigned_consultant_id);
                  return (
                    <article className={`case-card case-card-with-actions${selectedCaseId === item.case_id ? " is-selected" : ""}`} key={item.case_id}>
                      <button
                        aria-pressed={selectedCaseId === item.case_id}
                        className="case-card-main"
                        onClick={() => setSelectedCaseId(item.case_id)}
                        type="button"
                      >
                        <span>
                          <strong>{item.title || item.program_id}</strong>
                          <small>
                            {applicant?.full_name || applicant?.email || "Applicant details hidden until assigned"}
                            {" · "}{item.program_id}
                          </small>
                          <small>Assigned: {assigned?.full_name || assigned?.email || "Unassigned"}</small>
                        </span>
                        <span className="case-status-pill">{item.status.replaceAll("_", " ")}</span>
                      </button>
                      {selectedTenant.role === "OWNER" && (
                        <label className="portal-field case-assign-control" htmlFor={`assign-${item.case_id}`}>
                          <span>Assign case</span>
                          <select
                            disabled={busy === `assign-${item.case_id}`}
                            id={`assign-${item.case_id}`}
                            value={item.assigned_consultant_id || ""}
                            onChange={(event) => {
                              if (event.target.value) assignCase(item.case_id, event.target.value);
                            }}
                          >
                            <option disabled value="">
                              {item.assigned_consultant_id ? "Choose an assignee" : "Currently unassigned"}
                            </option>
                            {assignedPeople.map((member) => (
                              <option key={member.user_id} value={member.user_id}>
                                {member.full_name || member.email} · {member.role}
                              </option>
                            ))}
                          </select>
                        </label>
                      )}
                    </article>
                  );
                })}
              </div>
            )}
          </section>

          {selectedTenant.role === "CONSULTANT" && (
            <section className="portal-panel">
              <span className="portal-eyebrow">AVAILABLE WORK</span>
              <h2>Unassigned case queue</h2>
              <p className="portal-panel-intro">
                This list omits applicant identifiers until you claim a case.
              </p>
              {unassignedCases.length === 0 ? (
                <p className="muted">No unassigned cases are available.</p>
              ) : (
                <div className="case-card-list">
                  {unassignedCases.map((item) => (
                    <article className="case-card" key={item.case_id}>
                      <span>
                        <strong>{item.title || item.program_id}</strong>
                        <small>{item.program_id} · {item.status}</small>
                      </span>
                      <button
                        className="portal-primary-button case-action-button"
                        disabled={busy === `assign-${item.case_id}`}
                        onClick={() => claimCase(item.case_id)}
                        type="button"
                      >
                        Claim case
                      </button>
                    </article>
                  ))}
                </div>
              )}
            </section>
          )}
          {selectedCase && (
            <CaseWorkspace
              caseRecord={selectedCase}
              onCaseUpdated={updateCase}
              tenantId={selectedTenant.tenant_id}
              tenantRole={selectedTenant.role}
            />
          )}
        </>
      )}
      <p className="case-footer-link">
        Signed in as {user?.full_name || user?.email}. Platform administrators use the separate admin sign-in.
      </p>
    </div>
  );
}
