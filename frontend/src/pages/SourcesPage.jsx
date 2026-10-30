import { useMemo, useState } from "react";
import { Link } from "react-router-dom";
import * as api from "../api/client.js";
import { useApi } from "../hooks/useApi.js";
import { useSyncRunner } from "../hooks/useSyncRunner.js";
import DataTable from "../components/DataTable.jsx";
import StatusBadge from "../components/StatusBadge.jsx";
import ErrorState from "../components/ErrorState.jsx";
import { TableSkeleton } from "../components/Skeleton.jsx";
import AddSourceForm from "./AddSourceForm.jsx";
import { useToast } from "../components/ToastContext.jsx";
import SyncBanner from "../components/SyncBanner.jsx";
import DiscoveredSourcesTab from "./DiscoveredSourcesTab.jsx";

function formatDateTime(iso) {
  if (!iso) return "—";
  try {
    return new Date(iso).toLocaleString();
  } catch {
    return iso;
  }
}

// True while the source's HOST is in a refusal cooldown: it answered one of
// our requests with 403/429, so sync skips every source on it until then.
// Evaluated at render time so an already-expired pause never disables a button.
function isPaused(source, now = Date.now()) {
  if (!source.blocked_until) return false;
  const until = Date.parse(source.blocked_until);
  return Number.isFinite(until) && until > now;
}

export default function SourcesPage() {
  const [tab, setTab] = useState("sources"); // "sources" | "discovered"
  const [categoryFilter, setCategoryFilter] = useState("");
  const [programFilter, setProgramFilter] = useState("");
  const sourcesState = useApi(() => api.listSources(), []);
  const { running, elapsedSeconds, runOne, runAll } = useSyncRunner(() => sourcesState.reload());
  const [showAddForm, setShowAddForm] = useState(false);
  const { showToast } = useToast();

  const knownPrograms = useMemo(() => {
    if (!sourcesState.data) return [];
    const seen = new Map();
    for (const s of sourcesState.data) {
      if (!seen.has(s.program_id)) {
        seen.set(s.program_id, { program_id: s.program_id, country_id: s.country_id, visa_category: s.visa_category });
      }
    }
    return Array.from(seen.values()).sort((a, b) => a.program_id.localeCompare(b.program_id));
  }, [sourcesState.data]);

  const knownCategories = useMemo(
    () => Array.from(new Set(knownPrograms.map((p) => p.visa_category))).sort(),
    [knownPrograms]
  );
  const programsForCategory = useMemo(
    () =>
      knownPrograms.filter(
        (p) => !categoryFilter || p.visa_category === categoryFilter
      ),
    [knownPrograms, categoryFilter]
  );
  const visibleSources = useMemo(
    () =>
      (sourcesState.data || []).filter(
        (s) =>
          (!categoryFilter || s.visa_category === categoryFilter) &&
          (!programFilter || s.program_id === programFilter)
      ),
    [sourcesState.data, categoryFilter, programFilter]
  );

  async function toggleEnabled(source) {
    const nextStatus = source.status === "DISABLED" ? "ACTIVE" : "DISABLED";
    try {
      await api.updateSource(source.source_id, { status: nextStatus });
      sourcesState.reload();
    } catch (err) {
      showToast(err.message || "Could not update source");
    }
  }

  const columns = [
    {
      key: "source_id",
      header: "Source",
      render: (s) => (
        <Link to={`/sources/${encodeURIComponent(s.source_id)}`}>{s.source_id}</Link>
      ),
    },
    { key: "program_id", header: "Program", render: (s) => `${s.program_id} (${s.visa_category})` },
    {
      key: "url",
      header: "URL",
      render: (s) => (
        <a href={s.url} target="_blank" rel="noreferrer" style={{ wordBreak: "break-all" }}>
          {s.url}
        </a>
      ),
    },
    { key: "status", header: "Status", render: (s) => <StatusBadge value={s.status} /> },
    {
      key: "last_checked_at",
      header: "Last checked",
      render: (s) =>
        s.last_checked_at ? (
          formatDateTime(s.last_checked_at)
        ) : isPaused(s) ? (
          <span title="No fetch was attempted because another source on this host was refused.">
            Not checked (host paused)
          </span>
        ) : (
          "—"
        ),
    },
    {
      key: "fetch_status",
      header: "Last result",
      render: (s) => (
        <div>
          {s.fetch_status ? (
            <StatusBadge value={s.fetch_status} />
          ) : isPaused(s) ? (
            <span title="The host cooldown prevented this source from being fetched.">
              <StatusBadge value="INACTIVE" label="Skipped (host paused)" />
            </span>
          ) : (
            <StatusBadge value={null} />
          )}
          {s.http_status && <div className="muted">HTTP {s.http_status}</div>}
          {s.last_error && (
            <div
              className="muted"
              title={s.last_error_type || undefined}
              style={{ maxWidth: "280px", overflowWrap: "anywhere" }}
            >
              {s.last_error}
            </div>
          )}
        </div>
      ),
    },
    { key: "consecutive_failures", header: "Failures", render: (s) => s.consecutive_failures ?? 0 },
    {
      key: "blocked_until",
      header: "Paused until",
      render: (s) => (isPaused(s) ? formatDateTime(s.blocked_until) : "—"),
    },
    {
      key: "actions",
      header: "",
      render: (s) => {
        const paused = isPaused(s);
        return (
          <div className="flex">
            <button
              className="btn btn-sm"
              onClick={() => runOne(s.source_id)}
              disabled={running || paused}
              title={
                paused
                  ? `This host refused our last request. Sync resumes after ${formatDateTime(s.blocked_until)}.`
                  : undefined
              }
            >
              {paused ? "Paused" : "Run now"}
            </button>
            <button className="btn btn-sm" onClick={() => toggleEnabled(s)} disabled={running}>
              {s.status === "DISABLED" ? "Enable" : "Disable"}
            </button>
          </div>
        );
      },
    },
  ];

  return (
    <div>
      <div className="flex-between">
        <h1>Sources</h1>
        {tab === "sources" && (
          <div className="flex">
            <button className="btn" onClick={runAll} disabled={running}>
              Run all sources
            </button>
            <button className="btn btn-primary" onClick={() => setShowAddForm(true)}>
              Add source
            </button>
          </div>
        )}
      </div>

      {/* Tab navigation */}
      <nav className="tab-nav" aria-label="Sources tabs">
        <button
          id="tab-sources"
          className={"tab-btn" + (tab === "sources" ? " tab-btn-active" : "")}
          onClick={() => setTab("sources")}
          aria-selected={tab === "sources"}
          role="tab"
        >
          Active sources
        </button>
        <button
          id="tab-discovered"
          className={"tab-btn" + (tab === "discovered" ? " tab-btn-active" : "")}
          onClick={() => setTab("discovered")}
          aria-selected={tab === "discovered"}
          role="tab"
        >
          Discovered sources
        </button>
      </nav>

      {tab === "sources" && (
        <>
          {running && <SyncBanner elapsedSeconds={elapsedSeconds} />}
          {sourcesState.loading ? (
            <TableSkeleton rows={6} cols={9} />
          ) : sourcesState.error ? (
            <ErrorState error={sourcesState.error} onRetry={sourcesState.reload} />
          ) : (
            <>
              <div className="form-row">
                <div className="field">
                  <label htmlFor="sources-category">Visa category</label>
                  <select
                    id="sources-category"
                    value={categoryFilter}
                    onChange={(e) => {
                      setCategoryFilter(e.target.value);
                      setProgramFilter("");
                    }}
                  >
                    <option value="">All categories</option>
                    {knownCategories.map((category) => (
                      <option key={category} value={category}>
                        {category}
                      </option>
                    ))}
                  </select>
                </div>
                <div className="field">
                  <label htmlFor="sources-program">Visa program</label>
                  <select
                    id="sources-program"
                    value={programFilter}
                    onChange={(e) => setProgramFilter(e.target.value)}
                  >
                    <option value="">All programs</option>
                    {programsForCategory.map((program) => (
                      <option key={program.program_id} value={program.program_id}>
                        {program.program_id}
                      </option>
                    ))}
                  </select>
                </div>
              </div>
              <DataTable
                columns={columns}
                rows={visibleSources}
                rowKey={(s) => s.source_id}
                emptyTitle="No sources match"
                emptyMessage="Try a different visa category or program."
              />
            </>
          )}
        </>
      )}

      {tab === "discovered" && <DiscoveredSourcesTab />}

      {showAddForm && (
        <AddSourceForm
          knownPrograms={knownPrograms}
          onClose={() => setShowAddForm(false)}
          onCreated={() => {
            setShowAddForm(false);
            sourcesState.reload();
          }}
        />
      )}
    </div>
  );
}
