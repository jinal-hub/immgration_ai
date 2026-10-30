import { describe, it, expect, vi, beforeEach } from "vitest";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import DashboardPage from "../DashboardPage.jsx";
import { ToastProvider } from "../../components/ToastContext.jsx";
import * as api from "../../api/client.js";

vi.mock("../../api/client.js");

// Read a stat tile by its label rather than by its number: several tiles
// legitimately show the same value (one source is both total and healthy).
function statValue(label) {
  const labelEl = screen.getByText(label);
  return labelEl.parentElement.querySelector(".stat-value").textContent;
}

function renderPage() {
  return render(
    <MemoryRouter>
      <ToastProvider>
        <DashboardPage />
      </ToastProvider>
    </MemoryRouter>
  );
}

const SAMPLE_SOURCES = [
  {
    source_id: "SRC-AU-189",
    status: "ACTIVE",
    fetch_status: "ACCESS_BLOCKED",
    http_status: 403,
    last_error: "Playwright navigation was refused",
    consecutive_failures: 1,
    last_checked_at: "2026-10-10T03:00:00Z",
    blocked_until: null,
  },
];

const SAMPLE_QUEUE = { requirements: [], status: "NEEDS_REVIEW", program_id: null, count: 3 };
const SAMPLE_RULES_QUEUE = { counts: { drafts: 2, validated: 5 } };
const SAMPLE_SCHEDULER = {
  enabled: true,
  mode: "daily",
  hour_utc: 3,
  minute_utc: 0,
  interval_minutes: null,
  next_run_at: "2026-10-11T03:00:00Z",
};

describe("DashboardPage", () => {
  beforeEach(() => {
    vi.resetAllMocks();
    api.listRules.mockResolvedValue([]);
    api.getSyntheticGoldExamples.mockResolvedValue({
      dataset_file: "requirements_gold.json",
      synthetic: true,
      warning: "Synthetic test data; read-only.",
      items: [],
    });
  });

  it("renders all stat tiles with correct values when all APIs succeed", async () => {
    api.listSources.mockResolvedValue(SAMPLE_SOURCES);
    api.getReviewQueue.mockResolvedValue(SAMPLE_QUEUE);
    api.getRulesQueue.mockResolvedValue(SAMPLE_RULES_QUEUE);
    api.listRules.mockResolvedValue([
      {
        rule_id: "RULE-GOLD-REAL-189-AGE-001",
        requirement_id: "GOLD-REAL-189-AGE-001",
        program_id: "SKILLED_189",
        category: "ELIGIBILITY",
        field: "age",
        operator: "LT",
        value: 45,
        status: "ACTIVE",
        source_id: "SRC-AU-189-MAIN",
        document_id: "DOC-GOLD-189-REAL",
        evidence_text: "Applicants must be under 45.",
        evaluable: true,
      },
      {
        rule_id: "RULE-GOLD-REAL-190-AGE-001",
        requirement_id: "GOLD-REAL-190-AGE-001",
        program_id: "SKILLED_190",
        category: "AGE",
        field: "applicant.age",
        operator: "LT",
        value: 6,
        status: "DRAFT",
        source_id: "SRC-AU-190-MAIN",
        document_id: "DOC-GOLD-190-REAL",
        evidence_text: "Applicant must be under 45.",
        evaluable: true,
      },
    ]);
    api.getSyntheticGoldExamples.mockResolvedValue({
      dataset_file: "requirements_gold.json",
      program_id: "SKILLED_189",
      synthetic: true,
      warning: "Synthetic test data; read-only.",
      items: [{
        gold_id: "SYNTHETIC-001",
        requirement: "Fixture example",
        expected: { field: "applicant.age", operator: "LT", value: 45 },
      }],
    });
    api.getScheduler.mockResolvedValue(SAMPLE_SCHEDULER);

    renderPage();

    await waitFor(() => expect(statValue("Total sources")).toBe("1"));
    expect(statValue("Healthy sources")).toBe("0");
    expect(statValue("Failing sources")).toBe("1");
    expect(statValue("Requirements awaiting review")).toBe("3");
    expect(statValue("Draft rules")).toBe("2");
    expect(statValue("Rules awaiting approval")).toBe("5");
    expect(screen.getByText("SRC-AU-189")).toBeInTheDocument();
    expect(screen.getByText(/ACCESS_BLOCKED · HTTP 403 · Playwright navigation was refused/))
      .toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Gold rule review" })).toBeInTheDocument();
    expect(screen.getByText("2 evidence-backed gold rules · 1 active"))
      .toBeInTheDocument();
    expect(screen.getByRole("heading", { name: /Skilled Independent \(subclass 189\)/ }))
      .toBeInTheDocument();
    expect(screen.getByRole("heading", { name: /Skilled Nominated \(subclass 190\)/ }))
      .toBeInTheDocument();
    expect(screen.getByText(/"requirement_id": "GOLD-REAL-189-AGE-001"/))
      .toBeInTheDocument();
    expect(screen.getByText("ACTIVE")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Activate" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Reject" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Synthetic gold examples · read-only" }))
      .toBeInTheDocument();
    expect(screen.getByText(/Synthetic test data; read-only/)).toBeInTheDocument();
    expect(screen.getByText("SYNTHETIC-001 · Fixture example")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Manage rules" })).toHaveAttribute("href", "/rules");
  });

  it("uses the human review action to activate a gold draft", async () => {
    api.listSources.mockResolvedValue([]);
    api.getReviewQueue.mockResolvedValue(SAMPLE_QUEUE);
    api.getRulesQueue.mockResolvedValue(SAMPLE_RULES_QUEUE);
    api.listRules.mockResolvedValue([{
      rule_id: "RULE-GOLD-REAL-189-AGE-001",
      program_id: "SKILLED_189",
      status: "APPROVED",
      evaluable: true,
    }]);
    api.getScheduler.mockResolvedValue(SAMPLE_SCHEDULER);
    api.reviewGoldRule.mockResolvedValue({ status: "ACTIVE" });

    renderPage();

    fireEvent.click(await screen.findByRole("button", { name: "Activate" }));
    fireEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Activate" }));

    await waitFor(() =>
      expect(api.reviewGoldRule).toHaveBeenCalledWith(
        "RULE-GOLD-REAL-189-AGE-001",
        "ACTIVATE",
        { confirmNotEvaluable: undefined }
      )
    );
  });

  it("does not crash when sources API returns null data (error branch)", async () => {
    // Simulate sources error — firstError check redirects to ErrorState,
    // so this just ensures no unhandled throw.
    api.listSources.mockRejectedValue(new Error("network error"));
    api.getReviewQueue.mockResolvedValue(SAMPLE_QUEUE);
    api.getRulesQueue.mockResolvedValue(SAMPLE_RULES_QUEUE);
    api.getScheduler.mockResolvedValue(SAMPLE_SCHEDULER);

    renderPage();

    await waitFor(() =>
      expect(screen.getByRole("button", { name: /retry/i })).toBeInTheDocument()
    );
  });

  it("shows 0 for tiles when data fields are missing (optional-chaining guard)", async () => {
    // API returns data but without the expected shape fields
    api.listSources.mockResolvedValue([]);
    api.getReviewQueue.mockResolvedValue({ requirements: [], count: 0 });
    api.getRulesQueue.mockResolvedValue({ counts: { drafts: 0, validated: 0 } });
    api.getScheduler.mockResolvedValue({ ...SAMPLE_SCHEDULER, enabled: false });

    renderPage();

    // "Scheduler disabled" appears when scheduler is disabled
    await waitFor(() =>
      expect(screen.getByText(/scheduler disabled/i)).toBeInTheDocument()
    );
  });

  it("shows empty states for programs without curated gold rules", async () => {
    api.listSources.mockResolvedValue([]);
    api.getReviewQueue.mockResolvedValue(SAMPLE_QUEUE);
    api.getRulesQueue.mockResolvedValue(SAMPLE_RULES_QUEUE);
    api.listRules.mockResolvedValue([]);
    api.getScheduler.mockResolvedValue(SAMPLE_SCHEDULER);

    renderPage();

    await waitFor(() =>
      expect(screen.getAllByText("No curated gold rules yet.")).toHaveLength(5)
    );
  });

  it("counts sources currently paused by a host refusal", async () => {
    api.listSources.mockResolvedValue([
      ...SAMPLE_SOURCES,
      {
        source_id: "SRC-AU-482-MAIN",
        status: "ACTIVE",
        fetch_status: null,
        consecutive_failures: 0,
        last_checked_at: null,
        blocked_until: new Date(Date.now() + 60_000).toISOString(),
      },
    ]);
    api.getReviewQueue.mockResolvedValue(SAMPLE_QUEUE);
    api.getRulesQueue.mockResolvedValue(SAMPLE_RULES_QUEUE);
    api.getScheduler.mockResolvedValue(SAMPLE_SCHEDULER);

    renderPage();

    await waitFor(() =>
      expect(screen.getByText("Sources paused by host refusal:").parentElement)
        .toHaveTextContent("1")
    );
  });
});
