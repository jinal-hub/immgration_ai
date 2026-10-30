import { useParams } from "react-router-dom";
import * as api from "../api/client.js";
import { useApi } from "../hooks/useApi.js";
import StatusBadge from "../components/StatusBadge.jsx";
import DataTable from "../components/DataTable.jsx";
import ErrorState from "../components/ErrorState.jsx";

function formatDateTime(iso) {
  if (!iso) return "—";
  try {
    return new Date(iso).toLocaleString();
  } catch {
    return iso;
  }
}

export default function ApplicantDetailPage() {
  const { applicantId } = useParams();
  const state = useApi(() => api.getApplicant(applicantId), [applicantId]);

  if (state.loading) return <p className="muted">Loading…</p>;
  if (state.error) return <ErrorState error={state.error} onRetry={state.reload} />;

  const a = state.data;
  const columns = [
    { key: "program_id", header: "Program" },
    { key: "business_status", header: "Result", render: (r) => <StatusBadge value={r.business_status} /> },
    { key: "ruleset_hash", header: "Ruleset hash", render: (r) => <span className="faint">{r.ruleset_hash?.slice(0, 12) ?? "—"}</span> },
    { key: "evaluated_at", header: "Date", render: (r) => formatDateTime(r.evaluated_at) },
  ];

  return (
    <div>
      <h1>{[a.first_name, a.last_name].filter(Boolean).join(" ") || a.applicant_id}</h1>
      {!a.profile_exists && (
        <p className="portal-notice">
          This applicant has created an account but has not submitted their application profile yet.
        </p>
      )}
      <div className="card">
        <div className="card-grid">
          <div>
            <div className="muted">Owner email</div>
            <div>{a.owner_email}</div>
          </div>
          <div>
            <div className="muted">Date of birth</div>
            <div>{a.date_of_birth ? formatDateTime(a.date_of_birth) : "—"}</div>
          </div>
          <div>
            <div className="muted">Passport nationality</div>
            <div>{a.passport_nationality ?? "—"}</div>
          </div>
          <div>
            <div className="muted">Current visa subclass</div>
            <div>{a.current_visa_subclass ?? "—"}</div>
          </div>
          <div>
            <div className="muted">Age</div>
            <div>{a.age ?? "—"}</div>
          </div>
          <div>
            <div className="muted">English level</div>
            <div>{a.english_level ?? "—"}</div>
          </div>
          <div>
            <div className="muted">Points</div>
            <div>{a.points ?? "—"}</div>
          </div>
          <div>
            <div className="muted">Occupation</div>
            <div>{a.occupation ?? "—"}</div>
          </div>
          <div>
            <div className="muted">Visa subclass</div>
            <div>{a.visa_subclass ?? "—"}</div>
          </div>
          <div>
            <div className="muted">Skills assessment</div>
            <div>{a.skills_assessment_status ?? "—"}</div>
          </div>
          <div>
            <div className="muted">Health status</div>
            <div>{a.health_status ?? "—"}</div>
          </div>
          <div>
            <div className="muted">Character status</div>
            <div>{a.character_status ?? "—"}</div>
          </div>
          <div>
            <div className="muted">Visa history</div>
            <div>{a.visa_history ?? "—"}</div>
          </div>
          <div>
            <div className="muted">Work experience</div>
            <div>{a.work_experience_years === null ? "—" : `${a.work_experience_years} years`}</div>
          </div>
          <div>
            <div className="muted">Employer sponsored</div>
            <div>{a.employer_sponsored === null ? "—" : a.employer_sponsored ? "Yes" : "No"}</div>
          </div>
          <div>
            <div className="muted">Regional nomination</div>
            <div>{a.has_regional_nomination === null ? "—" : a.has_regional_nomination ? "Yes" : "No"}</div>
          </div>
          <div>
            <div className="muted">In Australia at application</div>
            <div>{a.in_australia_at_application === null ? "—" : a.in_australia_at_application ? "Yes" : "No"}</div>
          </div>
          <div>
            <div className="muted">Health insurance</div>
            <div>{a.has_health_insurance === null ? "—" : a.has_health_insurance ? "Yes" : "No"}</div>
          </div>
          <div>
            <div className="muted">Genuine student</div>
            <div>{a.genuine_temporary_entrant === null ? "—" : a.genuine_temporary_entrant ? "Yes" : "No"}</div>
          </div>
        </div>
      </div>

      <h2>Consultancy access</h2>
      {a.tenant_memberships?.length ? (
        <div className="card">
          <div className="card-grid">
            {a.tenant_memberships.map((membership) => (
              <div key={membership.tenant_id}>
                <div className="muted">{membership.tenant_name}</div>
                <div>{membership.role} · {membership.status}</div>
              </div>
            ))}
          </div>
        </div>
      ) : (
        <p className="muted">This applicant is not linked to a consultancy tenant.</p>
      )}

      <h2>Consultancy cases</h2>
      {a.cases?.length ? (
        <div className="card">
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Case</th>
                  <th>Consultancy</th>
                  <th>Program</th>
                  <th>Status</th>
                  <th>Assigned consultant</th>
                </tr>
              </thead>
              <tbody>
                {a.cases.map((applicationCase) => (
                  <tr key={applicationCase.case_id}>
                    <td>{applicationCase.title || applicationCase.case_id}</td>
                    <td>{applicationCase.tenant_name}</td>
                    <td>{applicationCase.program_id}</td>
                    <td>{applicationCase.status}</td>
                    <td>{applicationCase.assigned_consultant_id || "Unassigned"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      ) : (
        <p className="muted">No consultancy cases have been opened.</p>
      )}

      <h2>Assessments</h2>
      <DataTable
        columns={columns}
        rows={a.assessments}
        rowKey={(r) => r.assessment_id}
        emptyTitle="No assessments yet"
        emptyMessage="This applicant hasn't run an eligibility assessment yet."
      />
    </div>
  );
}
