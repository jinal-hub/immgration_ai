import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import RulesPage from "../RulesPage.jsx";
import { ToastProvider } from "../../components/ToastContext.jsx";
import * as api from "../../api/client.js";

vi.mock("../../api/client.js");

function rule(overrides = {}) {
  return {
    rule_id: "RULE-1",
    program_id: "SKILLED_189",
    category: "ELIGIBILITY",
    field: "age",
    operator: "between",
    value: [18, 45],
    unit: "years",
    mandatory: true,
    critical: false,
    evidence_text: "You must be between 18 and 45.",
    status: "APPROVED",
    evaluable: true,
    ...overrides,
  };
}

function renderPage() {
  return render(
    <MemoryRouter>
      <ToastProvider>
        <RulesPage />
      </ToastProvider>
    </MemoryRouter>
  );
}

describe("RulesPage", () => {
  beforeEach(() => {
    vi.resetAllMocks();
  });

  it("badges the rules no applicant fact can answer, and only those", async () => {
    api.listRules.mockResolvedValue([
      rule(),
      rule({
        rule_id: "RULE-2",
        field: "ominbudsman_outcome",
        evidence_text: "The Minister must be satisfied.",
        evaluable: false,
      }),
    ]);

    renderPage();

    await waitFor(() =>
      expect(screen.getByText("Not evaluable - no applicant field")).toBeInTheDocument()
    );
    expect(screen.getAllByText("Not evaluable - no applicant field")).toHaveLength(1);
  });

  it("will not activate a not-evaluable rule until ACTIVATE is typed", async () => {
    const user = userEvent.setup();
    api.listRules.mockResolvedValue([
      rule({ rule_id: "RULE-2", field: "ominbudsman_outcome", evaluable: false }),
    ]);
    api.activateRule.mockResolvedValue({});

    renderPage();
    await user.click(await screen.findByRole("button", { name: "Activate" }));

    const dialog = screen.getByRole("dialog");
    const confirm = within(dialog).getByRole("button", { name: "Activate" });
    expect(confirm).toBeDisabled();
    expect(within(dialog).getByText(/always return UNKNOWN/i)).toBeInTheDocument();

    await user.type(within(dialog).getByLabelText("Type ACTIVATE to confirm"), "ACTIVATE");
    expect(confirm).toBeEnabled();
    await user.click(confirm);

    await waitFor(() =>
      expect(api.activateRule).toHaveBeenCalledWith("RULE-2", {
        confirmNotEvaluable: "ACTIVATE",
      })
    );
  });

  it("does not require the typed confirmation for an evaluable rule", async () => {
    const user = userEvent.setup();
    api.listRules.mockResolvedValue([rule()]);
    api.activateRule.mockResolvedValue({});

    renderPage();
    await user.click(await screen.findByRole("button", { name: "Activate" }));

    const dialog = screen.getByRole("dialog");
    const confirm = within(dialog).getByRole("button", { name: "Activate" });
    expect(confirm).toBeEnabled();
    expect(within(dialog).queryByLabelText("Type ACTIVATE to confirm")).not.toBeInTheDocument();

    await user.click(confirm);
    await waitFor(() => expect(api.activateRule).toHaveBeenCalledTimes(1));
    expect(api.activateRule.mock.calls[0][0]).toBe("RULE-1");
  });

  it("tests a single rule before activating it", async () => {
    const user = userEvent.setup();
    api.listRules.mockResolvedValue([rule()]);
    api.evaluateRules.mockResolvedValue({
      program_id: "SKILLED_189",
      overall_status: "PASS",
      business_status: "ELIGIBLE",
      total_rules: 1,
      passed: 1,
      failed: 0,
      unknown: 0,
      not_applicable: 0,
      ruleset_hash: "abcdef1234567890",
      evaluated_at: "2026-10-10T03:00:00+00:00",
      results: [],
      gaps: [],
    });

    renderPage();
    await user.click(await screen.findByRole("button", { name: "Test" }));
    await user.click(screen.getByRole("button", { name: "Run check" }));

    await waitFor(() =>
      expect(api.evaluateRules).toHaveBeenCalledWith(
        expect.objectContaining({ program_id: "SKILLED_189", rule_ids: ["RULE-1"] })
      )
    );
    expect(await screen.findByText("Eligible")).toBeInTheDocument();
  });

  it("filters rules by visa category and then by program", async () => {
    const user = userEvent.setup();
    api.listRules.mockResolvedValue([
      rule({ rule_id: "SKILLED-RULE", program_id: "SKILLED_189", evidence_text: "Skilled evidence" }),
      rule({ rule_id: "STUDENT-RULE", program_id: "STUDENT_500", evidence_text: "Student evidence" }),
    ]);

    renderPage();
    await screen.findByText("Skilled evidence");

    await user.selectOptions(screen.getByLabelText("Visa category"), "STUDENT");
    expect(screen.getByText("Student evidence")).toBeInTheDocument();
    expect(screen.queryByText("Skilled evidence")).not.toBeInTheDocument();

    await user.selectOptions(screen.getByLabelText("Program"), "STUDENT_500");
    expect(screen.getByText("Student evidence")).toBeInTheDocument();
  });
});
