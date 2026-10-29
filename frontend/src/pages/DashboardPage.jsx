import { useState } from "react";
import { Link } from "react-router-dom";
import * as api from "../api/client.js";
import { useApi } from "../hooks/useApi.js";
import { useToast } from "../components/ToastContext.jsx";
import ErrorState from "../components/ErrorState.jsx";
import { LineSkeleton } from "../components/Skeleton.jsx";
import StatusBadge from "../components/StatusBadge.jsx";
import ConfirmDialog from "../components/ConfirmDialog.jsx";

const BLOCKED_FETCH_STATUSES = new Set([
  "ACCESS_BLOCKED",
  "NETWORK_RESTRICTED",
  "REQUIRES_BROWSER",
  "REQUIRES_MANUAL_DOWNLOAD",
]);

const GOLD_PROGRAMS = [
  { id: "SKILLED_189", name: "Skilled Independent (subclass 189)" },
  { id: "SKILLED_190", name: "Skilled Nominated (subclass 190)" },
  { id: "STUDENT_500", name: "Student visa (subclass 500)" },
  { id: "VISITOR_600", name: "Visitor visa (subclass 600)" },
  { id: "WORK_482", name: "Work visa (subclass 482)" },
];

function summarizeSources(sources) {
  let healthy = 0;
  let failing = 0;
  let paused = 0;
  let latestCheck = null;
  for (const s of sources) {
    const isFailing =
      s.status === "BROKEN" ||
      s.status === "NEEDS_MANUAL_CAPTURE" ||
      BLOCKED_FETCH_STATUSES.has(s.fetch_status) ||
      (s.consecutive_failures || 0) > 0;
    if (isFailing) failing += 1;
    else if (s.status === "ACTIVE") healthy += 1;
    if (s.blocked_until && Date.parse(s.blocked_until) > Date.now()) {
      paused += 1;
    }
    if (
      s.last_checked_at &&
      (!latestCheck || Date.parse(s.last_checked_at) > Date.parse(latestCheck.last_checked_at))
    ) {
      latestCheck = s;
    }
  }
  return { total: sources.length, healthy, failing, paused, latestCheck };
}

function StatTile({ label, value, to }) {
  const body = (
    <div className="stat-tile">
      <div className="stat-value">{value}</div>
      <div className="stat-label">{label}</div>
    </div>
  );
  return to ? <Link to={to} style={{ textDecoration: "none", color: "inherit" }}>{body}</Link> : body;
}

function formatDateTime(iso) {
  if (!iso) return "—";
  try {
    return new Date(iso).toLocaleString();
  } catch {
    return iso;
  }
}

export default function DashboardPage() {
  const [pendingGoldReview, setPendingGoldReview] = useState(null);
  const { showToast } = useToast();
  const sourcesState = useApi(() => api.listSources(), []);
  const reviewState = useApi(() => api.getReviewQueue({ status: "NEEDS_REVIEW" }), []);
  const rulesQueueState = useApi(() => api.getRulesQueue(), []);
  const allRulesState = useApi(() => api.listRules(), []);
  const syntheticGoldState = useApi(() => api.getSyntheticGoldExamples(), []);
  const schedulerState = useApi(() => api.getScheduler(), []);

  const anyLoading =
    sourcesState.loading || reviewState.loading || rulesQueueState.loading ||
    allRulesState.loading || syntheticGoldState.loading || schedulerState.loading;
  const firstError =
    sourcesState.error || reviewState.error || rulesQueueState.error ||
    allRulesState.error || syntheticGoldState.error || schedulerState.error;

  if (firstError) {
    return (
      <ErrorState
        error={firstError}
        onRetry={() => {
          sourcesState.reload();
          reviewState.reload();
          rulesQueueState.reload();
          allRulesState.reload();
          syntheticGoldState.reload();
          schedulerState.reload();
        }}
      />
    );
  }

  const summary = sourcesState.data ? summarizeSources(sourcesState.data) : null;
  const rules = allRulesState.data || [];
  const goldRules = rules.filter((rule) => rule.rule_id?.startsWith("RULE-GOLD-"));
  const activeGoldRuleCount = goldRules.filter((rule) => rule.status === "ACTIVE").length;
  const groupedRules = GOLD_PROGRAMS.map((program) => ({
    ...program,
    rules: goldRules.filter((rule) => rule.program_id === program.id),
  }));
  const otherGoldPrograms = [...new Set(
    goldRules
      .map((rule) => rule.program_id)
      .filter((programId) => programId && !GOLD_PROGRAMS.some((program) => program.id === programId))
  )].sort();
  for (const programId of otherGoldPrograms) {
    groupedRules.push({
      id: programId,
      name: programId,
      rules: goldRules.filter((rule) => rule.program_id === programId),
    });
  }

  function requestGoldReview(rule, decision) {
    setPendingGoldReview({
      rule,
      decision,
      requireText: decision === "ACTIVATE" && rule.evaluable === false ? "ACTIVATE" : undefined,
    });
  }

  async function applyGoldReview() {
    if (!pendingGoldReview) return;
    const { rule, decision, requireText } = pendingGoldReview;
    await api.reviewGoldRule(rule.rule_id, decision, { confirmNotEvaluable: requireText });
    setPendingGoldReview(null);
    showToast(decision === "ACTIVATE" ? `${rule.rule_id} activated.` : `${rule.rule_id} rejected.`);
    allRulesState.reload();
  }

  return (
    <div>
      <h1>Dashboard</h1>
      <div className="card-grid">
        <StatTile label="Total sources" value={anyLoading ? <LineSkeleton width="40px" /> : (summary?.total ?? 0)} to="/sources" />
        <StatTile label="Healthy sources" value={anyLoading ? <LineSkeleton width="40px" /> : (summary?.healthy ?? 0)} to="/sources" />
        <StatTile label="Failing sources" value={anyLoading ? <LineSkeleton width="40px" /> : (summary?.failing ?? 0)} to="/sources" />
        <StatTile
          label="Requirements awaiting review"
          value={anyLoading ? <LineSkeleton width="40px" /> : (reviewState.data?.count ?? 0)}
          to="/review"
        />
        <StatTile
          label="Draft rules"
          value={anyLoading ? <LineSkeleton width="40px" /> : (rulesQueueState.data?.counts?.drafts ?? 0)}
          to="/rules"
        />
        <StatTile
          label="Rules awaiting approval"
          value={anyLoading ? <LineSkeleton width="40px" /> : (rulesQueueState.data?.counts?.validated ?? 0)}
          to="/rules"
        />
      </div>

      <div className="card">
        <h2>Sync status</h2>
        <p>
          <span className="muted">Sources paused by host refusal: </span>
          {anyLoading ? <LineSkeleton width="40px" /> : (summary?.paused ?? 0)}
        </p>
        <p>
          <span className="muted">Last checked source: </span>
          {anyLoading ? (
            <LineSkeleton width="160px" />
          ) : summary?.latestCheck ? (
            <>
              <Link to={`/sources/${encodeURIComponent(summary.latestCheck.source_id)}`}>
                {summary.latestCheck.source_id}
              </Link>
              {" · "}
              {formatDateTime(summary.latestCheck.last_checked_at)}
            </>
          ) : (
            "—"
          )}
        </p>
        <p>
          <span className="muted">Last result: </span>
          {anyLoading ? (
            <LineSkeleton width="160px" />
          ) : summary?.latestCheck ? (
            <>
              {summary.latestCheck.fetch_status || summary.latestCheck.status || "Unknown"}
              {summary.latestCheck.http_status
                ? ` · HTTP ${summary.latestCheck.http_status}`
                : ""}
              {summary.latestCheck.last_error
                ? ` · ${summary.latestCheck.last_error}`
                : ""}
            </>
          ) : (
            "—"
          )}
        </p>
        <p>
          <span className="muted">Next scheduled run: </span>
          {anyLoading ? (
            <LineSkeleton width="160px" />
          ) : schedulerState.data?.enabled ? (
            formatDateTime(schedulerState.data.next_run_at)
          ) : (
            "Scheduler disabled"
          )}
        </p>
      </div>

      <div className="card">
        <div className="flex-between">
          <div>
            <h2>Gold rule review</h2>
            <p className="muted">
              {goldRules.length} evidence-backed gold rules · {activeGoldRuleCount} active
            </p>
          </div>
          <Link className="btn btn-sm" to="/rules">Manage rules</Link>
        </div>
        <p className="muted">
          Review each captured rule as JSON. Activate or reject are human decisions; only
          positive annotations with verified source evidence appear here.
        </p>
        {groupedRules.map((program) => (
          <section key={program.id} aria-labelledby={`gold-program-${program.id}`}>
            <h3 id={`gold-program-${program.id}`}>
              {program.name} <span className="muted">({program.rules.length})</span>
            </h3>
            {program.rules.length === 0 ? (
              <p className="muted">No curated gold rules yet.</p>
            ) : (
              <div className="gold-rule-list">
                {program.rules.map((rule) => (
                  <article className="gold-rule-card" key={rule.rule_id}>
                    <div className="card-header">
                      <strong>{rule.rule_id}</strong>
                      <StatusBadge value={rule.status} />
                      {rule.status === "DRAFT" && (
                        <div className="flex">
                          <button
                            className="btn btn-sm btn-primary"
                            onClick={() => requestGoldReview(rule, "ACTIVATE")}
                          >
                            Activate
                          </button>
                          <button
                            className="btn btn-sm btn-danger"
                            onClick={() => requestGoldReview(rule, "REJECT")}
                          >
                            Reject
                          </button>
                        </div>
                      )}
                    </div>
                    <pre className="gold-rule-json">
                      <code>{JSON.stringify(rule, null, 2)}</code>
                    </pre>
                  </article>
                ))}
              </div>
            )}
          </section>
        ))}
      </div>

      <div className="card">
        <h2>Synthetic gold examples · read-only</h2>
        <p className="field-error">{syntheticGoldState.data?.warning}</p>
        {(syntheticGoldState.data?.items || []).map((item) => (
          <details className="synthetic-gold-item" key={item.gold_id}>
            <summary>{item.gold_id} · {item.requirement || item.category || item.content_kind}</summary>
            <pre className="gold-rule-json">
              <code>{JSON.stringify(item, null, 2)}</code>
            </pre>
          </details>
        ))}
      </div>

      {pendingGoldReview && (
        <ConfirmDialog
          title={`${pendingGoldReview.decision === "ACTIVATE" ? "Activate" : "Reject"} ${pendingGoldReview.rule.rule_id}?`}
          message={
            pendingGoldReview.decision === "REJECT"
              ? "Reject this curated draft rule? It will remain visible with REJECTED status and will not be used in assessments."
              : pendingGoldReview.requireText
                ? "No applicant field can answer this rule; it will always return UNKNOWN. Type ACTIVATE to explicitly accept that limitation."
                : "This rule will become active for assessments and supersede any active rule with the same program, category, and field."
          }
          confirmLabel={pendingGoldReview.decision === "ACTIVATE" ? "Activate" : "Reject"}
          danger={pendingGoldReview.decision === "REJECT"}
          requireText={pendingGoldReview.requireText}
          onConfirm={applyGoldReview}
          onCancel={() => setPendingGoldReview(null)}
        />
      )}
    </div>
  );
}
