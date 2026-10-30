import { useEffect, useMemo, useState } from "react";
import * as api from "../api/client.js";
import { useApi } from "../hooks/useApi.js";
import ErrorState from "../components/ErrorState.jsx";
import StatusBadge from "../components/StatusBadge.jsx";
import { useApplicantAuth } from "../auth/ApplicantAuthContext.jsx";

const EMPTY_PROFILE = {
  first_name: "",
  last_name: "",
  date_of_birth: "",
  passport_nationality: "",
  current_visa_subclass: "",
  age: "",
  english_level: "",
  points: "",
  occupation: "",
  skills_assessment_status: "",
  health_status: "",
  character_status: "",
  has_regional_nomination: "",
  visa_subclass: "",
  visa_history: "",
  employer_sponsored: "",
  work_experience_years: "",
  in_australia_at_application: "",
  has_health_insurance: "",
  genuine_temporary_entrant: "",
};

const PROFILE_FIELDS = {
  SKILLED: [
    ["age", "Your age", "number"],
    ["points", "Points test score", "number"],
    ["english_level", "English proficiency", "select:english"],
    ["occupation", "Nominated occupation / ANZSCO code", "text"],
    ["work_experience_years", "Relevant work experience (years)", "number"],
    ["skills_assessment_status", "Skills assessment", "select:assessment"],
    ["has_regional_nomination", "State or territory nomination", "select:boolean"],
    ["health_status", "Health assessment", "select:check"],
    ["character_status", "Character assessment", "select:check"],
    ["visa_history", "Australian visa history", "select:history"],
  ],
  WORK: [
    ["age", "Your age", "number"],
    ["english_level", "English proficiency", "select:english"],
    ["occupation", "Occupation / ANZSCO code", "text"],
    ["work_experience_years", "Relevant work experience (years)", "number"],
    ["skills_assessment_status", "Skills assessment", "select:assessment"],
    ["employer_sponsored", "Do you have an employer sponsor?", "select:boolean"],
    ["health_status", "Health assessment", "select:check"],
    ["character_status", "Character assessment", "select:check"],
    ["visa_history", "Australian visa history", "select:history"],
  ],
  STUDENT: [
    ["age", "Your age", "number"],
    ["english_level", "English proficiency", "select:english"],
    ["has_health_insurance", "Do you have health insurance?", "select:boolean"],
    ["genuine_temporary_entrant", "Genuine student requirement", "select:boolean"],
    ["visa_history", "Australian visa history", "select:history"],
  ],
  VISITOR: [
    ["age", "Your age", "number"],
    ["visa_history", "Australian visa history", "select:history"],
    ["in_australia_at_application", "Are you currently in Australia?", "select:boolean"],
    ["health_status", "Health assessment", "select:check"],
    ["character_status", "Character assessment", "select:check"],
  ],
};

const SELECT_OPTIONS = {
  english: [
    ["", "Choose an option"],
    ["vocational", "Vocational"],
    ["competent", "Competent"],
    ["proficient", "Proficient"],
    ["superior", "Superior"],
  ],
  assessment: [
    ["", "Choose an option"],
    ["PENDING", "In progress"],
    ["VALID", "Valid"],
    ["INVALID", "Not valid"],
    ["EXEMPT", "Exempt"],
  ],
  check: [
    ["", "Choose an option"],
    ["UNKNOWN", "Not assessed yet"],
    ["PASSED", "Passed"],
    ["FAILED", "Not passed"],
  ],
  boolean: [
    ["", "Choose an option"],
    ["true", "Yes"],
    ["false", "No"],
  ],
  history: [
    ["", "Choose an option"],
    ["NONE", "No previous Australian visa"],
    ["PRIOR_GRANTED", "Previously granted"],
    ["PRIOR_REFUSED", "Previously refused"],
    ["PRIOR_CANCELLED", "Previously cancelled"],
    ["PRIOR_REMOVED", "Previously removed"],
  ],
};

const PROFILE_SUMMARY_FIELDS = [
  ["date_of_birth", "Date of birth"],
  ["passport_nationality", "Passport nationality"],
  ["current_visa_subclass", "Current visa"],
  ["age", "Age"],
  ["english_level", "English proficiency"],
  ["points", "Points score"],
  ["occupation", "Occupation"],
  ["visa_history", "Australian visa history"],
];

function profileValues(profile) {
  if (!profile) return { ...EMPTY_PROFILE };
  return Object.fromEntries(
    Object.keys(EMPTY_PROFILE).map((field) => [
      field,
      field === "date_of_birth" && profile[field]
        ? String(profile[field]).slice(0, 10)
        : profile[field] === null || profile[field] === undefined
        ? ""
        : String(profile[field]),
    ])
  );
}

function toProfilePayload(form) {
  const payload = {};
  for (const [field, value] of Object.entries(form)) {
    if (value === "") {
      payload[field] = null;
    } else if (field === "date_of_birth") {
      payload[field] = `${value}T00:00:00Z`;
    } else if (["age", "points", "work_experience_years"].includes(field)) {
      payload[field] = Number(value);
    } else if (["has_regional_nomination", "employer_sponsored",
      "in_australia_at_application", "has_health_insurance",
      "genuine_temporary_entrant"].includes(field)) {
      payload[field] = value === "true";
    } else {
      payload[field] = value;
    }
  }
  return payload;
}

function formatDate(value) {
  if (!value) return "Not available";
  return new Date(value).toLocaleString();
}

function ProfileInput({ field, label, type, value, onChange }) {
  if (type.startsWith("select:")) {
    const optionGroup = type.slice("select:".length);
    return (
      <label className="portal-field" htmlFor={`profile-${field}`}>
        <span>{label}</span>
        <select
          id={`profile-${field}`}
          value={value}
          onChange={(event) => onChange(field, event.target.value)}
        >
          {SELECT_OPTIONS[optionGroup].map(([optionValue, optionLabel]) => (
            <option key={optionValue} value={optionValue}>{optionLabel}</option>
          ))}
        </select>
      </label>
    );
  }
  return (
    <label className="portal-field" htmlFor={`profile-${field}`}>
      <span>{label}</span>
      <input
        id={`profile-${field}`}
        type={type}
        min={type === "number" ? 0 : undefined}
        max={field === "age" ? 120 : undefined}
        maxLength={type === "text" ? 32 : undefined}
        value={value}
        onChange={(event) => onChange(field, event.target.value)}
      />
    </label>
  );
}

function gapFieldName(field) {
  const normalized = field?.startsWith("applicant.") ? field.slice(10) : field;
  return {
    points_score: "points",
    required_points: "points",
    skills_assessment: "skills_assessment_status",
    health_check: "health_status",
    character_check: "character_status",
  }[normalized] || normalized;
}

function ReportDetails({ report, onClose, onAnswer }) {
  const [answers, setAnswers] = useState({});
  const [savingGap, setSavingGap] = useState(null);
  const [answerError, setAnswerError] = useState("");

  async function submitAnswer(event, gap) {
    event.preventDefault();
    const field = gapFieldName(gap.field);
    const rawValue = answers[gap.gap_id] ?? gap.answer ?? "";
    if (rawValue === "") {
      setAnswerError("Enter an answer before saving.");
      return;
    }

    let value = rawValue;
    if (["age", "points", "work_experience_years"].includes(field)) {
      value = Number(rawValue);
      if (!Number.isInteger(value)) {
        setAnswerError("Enter a whole number.");
        return;
      }
    } else if ([
      "has_regional_nomination", "employer_sponsored",
      "in_australia_at_application", "has_health_insurance",
      "genuine_temporary_entrant",
    ].includes(field)) {
      value = rawValue === "true";
    }

    setSavingGap(gap.gap_id);
    setAnswerError("");
    try {
      await onAnswer(gap, value);
      setAnswers((current) => ({ ...current, [gap.gap_id]: rawValue }));
    } catch (error) {
      setAnswerError(error.message || "Could not save your answer.");
    } finally {
      setSavingGap(null);
    }
  }

  return (
    <section className="portal-report" aria-label="Eligibility assessment report">
      <div className="portal-section-heading">
        <div>
          <span className="portal-eyebrow">ASSESSMENT REPORT</span>
          <h2>{report.program_id}</h2>
        </div>
        <button className="portal-text-button" onClick={onClose} type="button">Close</button>
      </div>
      <div className="portal-result-summary">
        <StatusBadge value={report.business_status} />
        <span>{report.passed_count} passed</span>
        <span>{report.failed_count} not met</span>
        <span>{report.unknown_count} need answers</span>
      </div>
      {report.rule_results?.length > 0 && (
        <div className="portal-report-list">
          {report.rule_results.map((result) => (
            <article className="portal-report-row" key={result.result_id || result.rule_id}>
              <StatusBadge value={result.status} />
              <div>
                <strong>{result.message || result.field || "Eligibility rule"}</strong>
                {result.reason && <p>{result.reason}</p>}
                {result.evidence_text && (
                  <p className="portal-source-evidence">
                    Official-source evidence: {result.evidence_text}
                  </p>
                )}
                {result.source_url ? (
                  <a
                    className="portal-source-link"
                    href={result.source_url}
                    rel="noreferrer"
                    target="_blank"
                  >
                    {result.source_authority || result.source_id || "View official source"}
                  </a>
                ) : (
                  <span className="portal-source-unavailable">
                    No official-source link is attached to this rule.
                  </span>
                )}
                {result.source_url && result.source_fetch_status && (
                  <small className="portal-source-status">
                    Source fetch: {result.source_fetch_status}
                    {result.source_last_checked_at
                      ? ` · checked ${formatDate(result.source_last_checked_at)}`
                      : ""}
                  </small>
                )}
              </div>
            </article>
          ))}
        </div>
      )}
      {report.gaps?.length > 0 && (
        <div className="portal-gaps">
          <h3>Information to follow up</h3>
          {report.gaps.map((gap) => {
            const field = gapFieldName(gap.field);
            const boolField = [
              "has_regional_nomination", "employer_sponsored",
              "in_australia_at_application", "has_health_insurance",
              "genuine_temporary_entrant",
            ].includes(field);
            const enumGroup = {
              english_level: "english",
              skills_assessment_status: "assessment",
              health_status: "check",
              character_status: "check",
              visa_history: "history",
            }[field];
            const currentValue = answers[gap.gap_id] ??
              (gap.answer === null || gap.answer === undefined ? "" : String(gap.answer));
            const inputId = `gap-answer-${gap.gap_id}`;

            return (
              <form
                className="portal-gap-question"
                key={gap.gap_id}
                onSubmit={(event) => submitAnswer(event, gap)}
              >
                <label className="portal-field" htmlFor={inputId}>
                  <span>{gap.message}</span>
                  {enumGroup ? (
                    <select
                      id={inputId}
                      value={currentValue}
                      onChange={(event) => setAnswers((current) => ({
                        ...current,
                        [gap.gap_id]: event.target.value,
                      }))}
                    >
                      {SELECT_OPTIONS[enumGroup].map(([value, label]) => (
                        <option key={value} value={value}>{label}</option>
                      ))}
                    </select>
                  ) : boolField ? (
                    <select
                      id={inputId}
                      value={currentValue}
                      onChange={(event) => setAnswers((current) => ({
                        ...current,
                        [gap.gap_id]: event.target.value,
                      }))}
                    >
                      <option value="">Choose an option</option>
                      <option value="true">Yes</option>
                      <option value="false">No</option>
                    </select>
                  ) : (
                    <input
                      id={inputId}
                      type={["age", "points", "work_experience_years"].includes(field)
                        ? "number" : "text"}
                      min={["age", "points", "work_experience_years"].includes(field) ? 0 : undefined}
                      max={field === "age" ? 120 : field === "points" ? 200 : undefined}
                      value={currentValue}
                      onChange={(event) => setAnswers((current) => ({
                        ...current,
                        [gap.gap_id]: event.target.value,
                      }))}
                    />
                  )}
                </label>
                {gap.answer !== null && gap.answer !== undefined && (
                  <span className="portal-gap-saved">Previously saved: {String(gap.answer)}</span>
                )}
                <button
                  className="portal-primary-button"
                  disabled={savingGap === gap.gap_id}
                  type="submit"
                >
                  {savingGap === gap.gap_id ? "Saving…" : "Save answer"}
                </button>
              </form>
            );
          })}
          {answerError && <p className="portal-form-error" role="alert">{answerError}</p>}
        </div>
      )}
      <p className="portal-disclaimer">
        This is an informational eligibility check, not a visa application or a decision
        by the Australian Government.
      </p>
    </section>
  );
}

export default function ApplicantPortalPage() {
  const { user } = useApplicantAuth();
  useEffect(() => {
    document.title = "Applicant workspace | Pathway";
  }, []);

  const portalState = useApi(async () => {
    const [programs, profiles] = await Promise.all([
      api.listApplicantPrograms(),
      api.listApplicantProfiles(),
    ]);
    const selections = profiles.length > 0
      ? await api.listApplicantVisaSelections(profiles[0].applicant_id)
      : [];
    const selectedProgramId = selections[0]?.program_id ||
      programs[0]?.program_id || "";
    return { programs, profiles, selectedProgramId };
  }, []);
  const [selectedProgramId, setSelectedProgramId] = useState("");
  const [selectedProfileId, setSelectedProfileId] = useState("");
  const [editingProfile, setEditingProfile] = useState(true);
  const [form, setForm] = useState(() => profileValues(null));
  const [profileOverrides, setProfileOverrides] = useState({});
  const [programSearch, setProgramSearch] = useState("");
  const [programCategory, setProgramCategory] = useState("ALL");
  const [programSelectionError, setProgramSelectionError] = useState("");
  const [history, setHistory] = useState([]);
  const [cases, setCases] = useState([]);
  const [casesLoading, setCasesLoading] = useState(false);
  const [casesError, setCasesError] = useState("");
  const [historyLoading, setHistoryLoading] = useState(false);
  const [historyError, setHistoryError] = useState("");
  const [saveMessage, setSaveMessage] = useState("");
  const [formError, setFormError] = useState("");
  const [busy, setBusy] = useState(false);
  const [report, setReport] = useState(null);
  const [reportError, setReportError] = useState("");
  const [historyVersion, setHistoryVersion] = useState(0);

  const programs = portalState.data?.programs || [];
  const profiles = portalState.data?.profiles || [];
  const selectedProgram = programs.find((program) => program.program_id === selectedProgramId);
  const selectedProfile = profileOverrides[selectedProfileId] ||
    profiles.find((profile) => profile.applicant_id === selectedProfileId);
  const programCategories = [...new Set(programs.map((program) => program.visa_category))]
    .sort();
  const filteredPrograms = programs.filter((program) => {
    const searchable = `${program.name} ${program.program_id} ${program.subclass_code || ""} ${program.country_id}`.toLowerCase();
    return (
      (programCategory === "ALL" || program.visa_category === programCategory) &&
      searchable.includes(programSearch.trim().toLowerCase())
    );
  });
  const dynamicFields = useMemo(
    () => PROFILE_FIELDS[selectedProgram?.visa_category] || [
      ["age", "Your age", "number"],
      ["english_level", "English proficiency", "select:english"],
      ["visa_history", "Australian visa history", "select:history"],
      ["health_status", "Health assessment", "select:check"],
      ["character_status", "Character assessment", "select:check"],
    ],
    [selectedProgram?.visa_category]
  );

  useEffect(() => {
    if (!portalState.data) return;
    if (!selectedProgramId && portalState.data.selectedProgramId) {
      setSelectedProgramId(portalState.data.selectedProgramId);
    }
    if (!selectedProfileId && portalState.data.profiles.length > 0) {
      const profile = portalState.data.profiles[0];
      setSelectedProfileId(profile.applicant_id);
      setForm(profileValues(profile));
      setEditingProfile(false);
    } else if (!selectedProfileId && user?.full_name) {
      const nameParts = user.full_name.trim().split(/\s+/, 2);
      setForm((current) => ({
        ...current,
        first_name: current.first_name || nameParts[0] || "",
        last_name: current.last_name || nameParts[1] || "",
      }));
    }
  }, [portalState.data, selectedProgramId, selectedProfileId, user?.full_name]);

  useEffect(() => {
    if (!selectedProfileId) {
      setHistory([]);
      setHistoryError("");
      return undefined;
    }
    let cancelled = false;
    setHistoryLoading(true);
    setHistoryError("");
    api.listApplicantAssessments(selectedProfileId).then((result) => {
      if (!cancelled) setHistory(result.assessments || []);
    }).catch((error) => {
      if (!cancelled) setHistoryError(error.message || "Could not load your applications.");
    }).finally(() => {
      if (!cancelled) setHistoryLoading(false);
    });
    return () => {
      cancelled = true;
    };
  }, [selectedProfileId, historyVersion]);

  useEffect(() => {
    if (!selectedProfileId) {
      setCases([]);
      setCasesError("");
      return undefined;
    }
    let cancelled = false;
    setCasesLoading(true);
    setCasesError("");
    api.listApplicantCases(selectedProfileId).then((result) => {
      if (!cancelled) setCases(result);
    }).catch((error) => {
      if (!cancelled) {
        setCasesError(error.message || "Could not load your consultancy cases.");
      }
    }).finally(() => {
      if (!cancelled) setCasesLoading(false);
    });
    return () => {
      cancelled = true;
    };
  }, [selectedProfileId]);

  if (portalState.loading) {
    return <div className="portal-loading"><span className="portal-spinner" /> Loading your workspace…</div>;
  }
  if (portalState.error) {
    return <ErrorState error={portalState.error} onRetry={portalState.reload} />;
  }

  function updateField(field, value) {
    setForm((current) => ({ ...current, [field]: value }));
    setSaveMessage("");
    setFormError("");
  }

  function selectProfile(profileId) {
    setSelectedProfileId(profileId);
    const profile = profileOverrides[profileId] ||
      profiles.find((item) => item.applicant_id === profileId);
    setForm(profileValues(profile));
    setEditingProfile(false);
    setSaveMessage("");
    setFormError("");
    setReport(null);
    api.listApplicantVisaSelections(profileId).then((selections) => {
      const selected = selections.find((selection) =>
        programs.some((program) => program.program_id === selection.program_id),
      );
      if (selected) setSelectedProgramId(selected.program_id);
    }).catch((error) => {
      setProgramSelectionError(
        error.message || "Could not load your saved visa pathway.",
      );
    });
  }

  async function saveProfile() {
    const payload = {
      ...toProfilePayload(form),
      visa_subclass: selectedProgram?.subclass_code || null,
    };
    if (!payload.first_name || !payload.last_name) {
      throw new Error("Add your first and last name to save your profile.");
    }
    const saved = selectedProfileId
      ? await api.updateApplicantProfile(selectedProfileId, payload)
      : await api.createApplicantProfile(payload);
    setProfileOverrides((current) => ({
      ...current,
      [saved.applicant_id]: saved,
    }));
    setSelectedProfileId(saved.applicant_id);
    setForm(profileValues(saved));
    setEditingProfile(false);
    setSaveMessage("Your profile is saved.");
    portalState.reload();
    return saved;
  }

  async function handleSave(event) {
    event.preventDefault();
    setBusy(true);
    setFormError("");
    setSaveMessage("");
    try {
      await saveProfile();
    } catch (error) {
      setFormError(error.message || "Could not save your profile.");
    } finally {
      setBusy(false);
    }
  }

  async function startAssessment() {
    if (!selectedProgram || selectedProgram.status !== "ACTIVE" ||
        selectedProgram.active_rule_count < 1) return;
    setBusy(true);
    setFormError("");
    setReportError("");
    setSaveMessage("");
    try {
      const saved = await saveProfile();
      await api.saveApplicantVisaSelection(
        saved.applicant_id,
        selectedProgram.program_id,
      );
      const assessment = await api.createApplicantAssessment(
        saved.applicant_id,
        selectedProgram.program_id
      );
      setReport(assessment);
      setHistoryVersion((version) => version + 1);
    } catch (error) {
      setFormError(error.message || "Could not run your eligibility check.");
    } finally {
      setBusy(false);
    }
  }

  async function selectProgram(programId) {
    setSelectedProgramId(programId);
    setSaveMessage("");
    setProgramSelectionError("");
    if (!selectedProfileId) return;
    try {
      await api.saveApplicantVisaSelection(selectedProfileId, programId);
    } catch (error) {
      setProgramSelectionError(
        error.message || "Could not save your selected visa pathway.",
      );
    }
  }

  async function openReport(assessmentId) {
    setReport(null);
    setReportError("");
    try {
      setReport(await api.getApplicantAssessment(assessmentId));
    } catch (error) {
      setReportError(error.message || "Could not load this report.");
    }
  }

  async function answerGap(gap, value) {
    const saved = await api.answerApplicantGap(report.assessment_id, gap.gap_id, value);
    const answeredReport = {
      ...report,
      gaps: report.gaps.map((item) => item.gap_id === gap.gap_id
        ? { ...item, answer: saved.answer }
        : item),
    };
    setReport(answeredReport);
    setHistoryVersion((version) => version + 1);
    try {
      const refreshed = await api.createApplicantAssessment(
        report.applicant_id,
        report.program_id,
      );
      setReport(refreshed);
      setHistoryVersion((version) => version + 1);
    } catch (error) {
      setReportError(
        `Your answer was saved, but the assessment could not be refreshed: ${error.message}`,
      );
    }
    return saved;
  }

  const completedFields = [
    "first_name",
    "last_name",
    "date_of_birth",
    "passport_nationality",
    "current_visa_subclass",
    ...dynamicFields.map(([field]) => field),
  ].filter((field) => form[field] !== "").length
    + (selectedProgram?.subclass_code || form.visa_subclass ? 1 : 0);
  const totalFields = 6 + dynamicFields.length;

  return (
    <div className="portal-workspace">
      <section className="portal-hero">
        <div className="portal-hero-copy">
          <span className="portal-eyebrow">APPLICANT WORKSPACE</span>
          <h1>Your next chapter, made clearer.</h1>
          <p>Build your profile, choose a visa pathway, and keep your eligibility checks in one place.</p>
        </div>
        <div className="portal-hero-stat">
          <span className="portal-hero-stat-icon" aria-hidden="true">✦</span>
          <span><strong>{history.length}</strong><small>saved checks</small></span>
        </div>
      </section>

      <div className="portal-notice">
        <span aria-hidden="true">ⓘ</span>
        <p>
          This workspace provides preliminary eligibility information only. It does not
          lodge a visa application or replace advice from a registered migration agent.
        </p>
      </div>

      <div className="portal-columns">
        <section className="portal-panel portal-profile-panel">
          <div className="portal-section-heading">
            <div>
              <span className="portal-eyebrow">STEP 1 · YOUR DETAILS</span>
              <h2>Your application profile</h2>
            </div>
            <span className="portal-progress-count">{completedFields}/{totalFields}</span>
          </div>
          <div className="portal-progress-track" aria-label={`${completedFields} of ${totalFields} profile fields completed`}>
            <span style={{ width: `${Math.round((completedFields / totalFields) * 100)}%` }} />
          </div>
          <p className="portal-panel-intro">
            Your details stay private to your account. Leave anything unknown blank; missing
            information will be shown in your assessment rather than guessed.
          </p>

          {profiles.length > 1 && (
            <label className="portal-field" htmlFor="applicant-profile">
              <span>Profile</span>
              <select
                id="applicant-profile"
                value={selectedProfileId}
                onChange={(event) => selectProfile(event.target.value)}
              >
                {profiles.map((profile) => (
                  <option key={profile.applicant_id} value={profile.applicant_id}>
                    {[profile.first_name, profile.last_name].filter(Boolean).join(" ") || "Applicant profile"}
                  </option>
                ))}
              </select>
            </label>
          )}

          {selectedProfile && !editingProfile ? (
            <div className="portal-profile-summary">
              <div className="portal-profile-summary-heading">
                <div>
                  <strong>{[selectedProfile.first_name, selectedProfile.last_name].filter(Boolean).join(" ")}</strong>
                  <span>Profile saved to your account</span>
                </div>
                <button
                  className="portal-text-button"
                  onClick={() => setEditingProfile(true)}
                  type="button"
                >
                  Edit profile
                </button>
              </div>
              <dl className="portal-profile-summary-grid">
                {PROFILE_SUMMARY_FIELDS.map(([field, label]) => {
                  const value = selectedProfile[field];
                  const displayValue = field === "date_of_birth" && value
                    ? String(value).slice(0, 10)
                    : value === null || value === undefined || value === ""
                    ? "Not provided"
                    : String(value);
                  return (
                    <div key={field}>
                      <dt>{label}</dt>
                      <dd>{displayValue}</dd>
                    </div>
                  );
                })}
              </dl>
            </div>
          ) : <form onSubmit={handleSave}>
            <div className="portal-form-grid">
              <label className="portal-field" htmlFor="profile-first_name">
                <span>First name</span>
                <input
                  autoComplete="given-name"
                  id="profile-first_name"
                  maxLength={128}
                  required
                  value={form.first_name}
                  onChange={(event) => updateField("first_name", event.target.value)}
                />
              </label>
              <label className="portal-field" htmlFor="profile-last_name">
                <span>Last name</span>
                <input
                  autoComplete="family-name"
                  id="profile-last_name"
                  maxLength={128}
                  required
                  value={form.last_name}
                  onChange={(event) => updateField("last_name", event.target.value)}
                />
              </label>
              <label className="portal-field" htmlFor="profile-account_email">
                <span>Account email</span>
                <input
                  autoComplete="email"
                  id="profile-account_email"
                  readOnly
                  value={user?.email || ""}
                />
              </label>
              <label className="portal-field" htmlFor="profile-date_of_birth">
                <span>Date of birth</span>
                <input
                  autoComplete="bday"
                  id="profile-date_of_birth"
                  max={new Date().toISOString().slice(0, 10)}
                  type="date"
                  value={form.date_of_birth}
                  onChange={(event) => updateField("date_of_birth", event.target.value)}
                />
              </label>
              <label className="portal-field" htmlFor="profile-passport_nationality">
                <span>Passport nationality</span>
                <input
                  autoComplete="country"
                  id="profile-passport_nationality"
                  maxLength={8}
                  placeholder="e.g. IND"
                  value={form.passport_nationality}
                  onChange={(event) => updateField("passport_nationality", event.target.value.toUpperCase())}
                />
              </label>
              <label className="portal-field" htmlFor="profile-current_visa_subclass">
                <span>Current Australian visa subclass (if any)</span>
                <input
                  id="profile-current_visa_subclass"
                  maxLength={8}
                  value={form.current_visa_subclass}
                  onChange={(event) => updateField("current_visa_subclass", event.target.value)}
                />
              </label>
              <label className="portal-field" htmlFor="profile-target_visa_subclass">
                <span>Target visa subclass</span>
                <input
                  id="profile-target_visa_subclass"
                  readOnly
                  value={selectedProgram?.subclass_code || form.visa_subclass}
                />
              </label>
              {dynamicFields.map(([field, label, type]) => (
                <ProfileInput
                  key={field}
                  field={field}
                  label={label}
                  type={type}
                  value={form[field]}
                  onChange={updateField}
                />
              ))}
            </div>
            {formError && <p className="portal-form-error" role="alert">{formError}</p>}
            {saveMessage && <p className="portal-success-message" role="status">{saveMessage}</p>}
            <button className="portal-secondary-button" disabled={busy} type="submit">
              {busy ? "Saving…" : selectedProfile ? "Save profile changes" : "Save my profile"}
            </button>
            {selectedProfile && (
              <button
                className="portal-text-button portal-cancel-edit"
                disabled={busy}
                onClick={() => {
                  setForm(profileValues(selectedProfile));
                  setEditingProfile(false);
                  setFormError("");
                }}
                type="button"
              >
                Cancel changes
              </button>
            )}
          </form>}
        </section>

        <aside className="portal-side-column">
          <section className="portal-panel portal-pathway-panel">
            <span className="portal-eyebrow">STEP 2 · YOUR PATHWAY</span>
            <h2>Which visa are you exploring?</h2>
            <p className="portal-panel-intro">
              Browse every listed pathway. Eligibility checks are available only for active programs with published rules.
            </p>
            <label className="portal-field" htmlFor="visa-program-search">
              <span>Search visa programs</span>
              <input
                id="visa-program-search"
                type="search"
                placeholder="Search program, subclass, or country"
                value={programSearch}
                onChange={(event) => setProgramSearch(event.target.value)}
              />
            </label>
            <label className="portal-field portal-program-filter" htmlFor="visa-program-category">
              <span>Visa category</span>
              <select
                id="visa-program-category"
                value={programCategory}
                onChange={(event) => setProgramCategory(event.target.value)}
              >
                <option value="ALL">All categories</option>
                {programCategories.map((category) => (
                  <option key={category} value={category}>{category}</option>
                ))}
              </select>
            </label>
            <div className="portal-program-list">
              {filteredPrograms.map((program) => (
                <button
                  aria-pressed={selectedProgramId === program.program_id}
                  className={`portal-program-card${selectedProgramId === program.program_id ? " is-selected" : ""}`}
                  key={program.program_id}
                  onClick={() => selectProgram(program.program_id)}
                  type="button"
                >
                  <span className="portal-program-icon" aria-hidden="true">
                    {program.visa_category === "STUDENT" ? "✎" :
                      program.visa_category === "VISITOR" ? "↗" :
                        program.visa_category === "WORK" ? "⌘" : "◇"}
                  </span>
                  <span className="portal-program-copy">
                    <strong>{program.name}</strong>
                    <small>
                      {program.country_id} · {program.visa_category}
                      {program.subclass_code ? ` · Subclass ${program.subclass_code}` : ""}
                    </small>
                  </span>
                  <span className={`portal-program-status ${program.status === "ACTIVE" ? "is-active" : ""}`}>
                    {program.status === "ACTIVE" ? "Active" : "Reference"}
                  </span>
                  <span className="portal-radio" aria-hidden="true" />
                </button>
              ))}
              {filteredPrograms.length === 0 && (
                <p className="portal-empty-state">No programs match these filters.</p>
              )}
            </div>
            {programSelectionError && (
              <p className="portal-form-error" role="alert">{programSelectionError}</p>
            )}
            {selectedProgram && (
              <p className="portal-program-availability">
                {selectedProgram.status !== "ACTIVE"
                  ? "This program is listed for reference and is not open for eligibility assessment."
                  : selectedProgram.active_rule_count < 1
                  ? "This active program does not have published eligibility rules yet."
                  : `${selectedProgram.active_rule_count} published eligibility checks`}
              </p>
            )}
            {selectedProgram && (
              <div className="portal-program-sources" aria-label="Official visa sources">
                <h3>Official sources</h3>
                {selectedProgram.sources?.length ? (
                  <ul>
                    {selectedProgram.sources.map((source) => (
                      <li key={source.source_id}>
                        <a
                          href={source.url}
                          rel="noreferrer"
                          target="_blank"
                        >
                          {source.authority}
                        </a>
                        <span>
                          {source.fetch_status || source.status}
                          {source.last_checked_at
                            ? ` · checked ${formatDate(source.last_checked_at)}`
                            : " · not checked yet"}
                        </span>
                      </li>
                    ))}
                  </ul>
                ) : (
                  <p>No official sources are registered for this pathway yet.</p>
                )}
              </div>
            )}
            {selectedProgram && (
              selectedProgram.status !== "ACTIVE" || selectedProgram.active_rule_count < 1
            ) && (
              <p className="portal-program-unavailable">
                You can save your profile and selected pathway. An eligibility check is not available for this program yet.
              </p>
            )}
            <button
              className="portal-primary-button portal-assess-button"
              disabled={
                busy || !selectedProgram || selectedProgram.status !== "ACTIVE" ||
                selectedProgram.active_rule_count < 1 ||
                !selectedProfileId && (!form.first_name || !form.last_name)
              }
              onClick={startAssessment}
              type="button"
            >
              {busy ? "Checking your saved profile…" : "Check eligibility"}
              {!busy && <span aria-hidden="true">→</span>}
            </button>
            <p className="portal-assess-note">
              {selectedProgram?.status === "ACTIVE" && selectedProgram.active_rule_count > 0
                ? `${selectedProgram.active_rule_count} active checks · Uses your saved profile`
                : "Choose a pathway to continue"}
            </p>
          </section>

          <section className="portal-trust-card">
            <span aria-hidden="true">♧</span>
            <div>
              <strong>Your information, your account</strong>
              <p>Your profile and reports are private and accessible only after you sign in.</p>
            </div>
          </section>
        </aside>
      </div>

      <section className="portal-panel portal-applications">
        <div className="portal-section-heading">
          <div>
            <span className="portal-eyebrow">YOUR PROGRESS</span>
            <h2>Eligibility checks</h2>
          </div>
          <span className="portal-history-count">{history.length} saved</span>
        </div>
        <p className="portal-panel-intro">
          Each check saves a snapshot of your profile and the rules used at that time.
        </p>
        {historyLoading ? (
          <p className="portal-muted">Loading your saved checks…</p>
        ) : historyError ? (
          <p className="portal-form-error" role="alert">{historyError}</p>
        ) : history.length === 0 ? (
          <div className="portal-empty-state">
            <span className="portal-empty-icon" aria-hidden="true">◇</span>
            <strong>Your journey starts here</strong>
            <p>            Save your profile and run an eligibility check to see your progress here.</p>
          </div>
        ) : (
          <div className="portal-history-list">
            {history.map((item) => (
              <article className="portal-history-item" key={item.assessment_id}>
                <span className="portal-history-icon" aria-hidden="true">↗</span>
                <div className="portal-history-main">
                  <strong>{programs.find((program) => program.program_id === item.program_id)?.name || item.program_id}</strong>
                  <small>{formatDate(item.evaluated_at)} · {item.total_rules} checks</small>
                </div>
                <div className="portal-history-result">
                  <StatusBadge value={item.business_status} />
                  <button
                    className="portal-text-button"
                    onClick={() => openReport(item.assessment_id)}
                    type="button"
                  >
                    View report <span aria-hidden="true">→</span>
                  </button>
                </div>
              </article>
            ))}
          </div>
        )}
        {reportError && <p className="portal-form-error" role="alert">{reportError}</p>}
      </section>

      {report && (
        <ReportDetails
          report={report}
          onAnswer={answerGap}
          onClose={() => setReport(null)}
        />
      )}

      <section className="portal-panel portal-applications">
        <div className="portal-section-heading">
          <div>
            <span className="portal-eyebrow">CONSULTANCY WORKFLOW</span>
            <h2>Your application cases</h2>
          </div>
          <span className="portal-history-count">{cases.length} cases</span>
        </div>
        <p className="portal-panel-intro">
          Cases shared with a consultancy appear here. Your access follows your active tenant membership.
        </p>
        {casesLoading ? (
          <p className="portal-muted">Loading your cases…</p>
        ) : casesError ? (
          <p className="portal-form-error" role="alert">{casesError}</p>
        ) : cases.length === 0 ? (
          <div className="portal-empty-state">
            <strong>No consultancy cases yet</strong>
            <p>A consultancy must invite your account before a case can be opened.</p>
          </div>
        ) : (
          <div className="portal-history-list">
            {cases.map((applicationCase) => (
              <article className="portal-history-item" key={applicationCase.case_id}>
                <span className="portal-history-icon" aria-hidden="true">◇</span>
                <div className="portal-history-main">
                  <strong>{applicationCase.title || applicationCase.program_id}</strong>
                  <small>
                    {applicationCase.tenant_name || "Consultancy"} · {applicationCase.program_id}
                    {" · "}{formatDate(applicationCase.created_at)}
                  </small>
                </div>
                <div className="portal-history-result">
                  <StatusBadge value={applicationCase.status} />
                </div>
              </article>
            ))}
          </div>
        )}
      </section>

      <span className="portal-signed-in">Signed in as {user?.email}</span>
    </div>
  );
}
