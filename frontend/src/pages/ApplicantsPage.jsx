import { useState } from "react";
import { Link } from "react-router-dom";
import * as api from "../api/client.js";
import { useApi } from "../hooks/useApi.js";
import DataTable from "../components/DataTable.jsx";
import ErrorState from "../components/ErrorState.jsx";
import { TableSkeleton } from "../components/Skeleton.jsx";

const PAGE_SIZE = 25;

export default function ApplicantsPage() {
  const [offset, setOffset] = useState(0);
  const applicantsState = useApi(() => api.listApplicants({ limit: PAGE_SIZE, offset }), [offset]);

  if (applicantsState.loading) {
    return (
      <div>
        <h1>Applicants</h1>
        <TableSkeleton rows={6} cols={5} />
      </div>
    );
  }
  if (applicantsState.error) {
    return <ErrorState error={applicantsState.error} onRetry={applicantsState.reload} />;
  }

  const { applicants, total } = applicantsState.data;
  const columns = [
    {
      key: "name",
      header: "Name",
      render: (a) => (
        <Link to={`/applicants/${encodeURIComponent(a.applicant_id)}`}>
          {[a.first_name, a.last_name].filter(Boolean).join(" ") || a.applicant_id}
        </Link>
      ),
    },
    { key: "owner_email", header: "Owner email" },
    { key: "profile", header: "Profile", render: (a) => (
      !a.profile_exists ? "Not started" :
        [a.first_name, a.last_name].filter(Boolean).length === 2 ? "Started" : "Needs details"
    ) },
    { key: "age", header: "Age" },
    { key: "points", header: "Points" },
    { key: "visa_subclass", header: "Target visa" },
    {
      key: "tenants",
      header: "Consultancies",
      render: (a) => a.tenant_memberships?.length
        ? a.tenant_memberships.map((membership) => membership.tenant_name).join(", ")
        : "None",
    },
  ];

  return (
    <div>
      <div className="flex-between">
        <h1>Applicants</h1>
        <button className="btn btn-sm" onClick={applicantsState.reload} type="button">
          Refresh
        </button>
      </div>
      <DataTable
        columns={columns}
        rows={applicants}
        rowKey={(a) => a.applicant_id}
        emptyTitle="No applicants yet"
        emptyMessage="Nobody has registered an applicant profile yet."
      />
      <div className="flex-between" style={{ marginTop: "var(--space-3)" }}>
        <span className="muted">
          {total === 0 ? 0 : offset + 1}–{Math.min(offset + PAGE_SIZE, total)} of {total}
        </span>
        <div className="flex">
          <button className="btn btn-sm" disabled={offset === 0} onClick={() => setOffset((o) => Math.max(0, o - PAGE_SIZE))}>
            Previous
          </button>
          <button
            className="btn btn-sm"
            disabled={offset + PAGE_SIZE >= total}
            onClick={() => setOffset((o) => o + PAGE_SIZE)}
          >
            Next
          </button>
        </div>
      </div>
    </div>
  );
}
