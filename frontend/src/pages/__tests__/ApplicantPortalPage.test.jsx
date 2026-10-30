import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { ApplicantAuthProvider } from "../../auth/ApplicantAuthContext.jsx";
import ApplicantPortalPage from "../ApplicantPortalPage.jsx";
import * as api from "../../api/client.js";

vi.mock("../../api/client.js");

function renderPortal() {
  return render(
    <MemoryRouter>
      <ApplicantAuthProvider>
        <ApplicantPortalPage />
      </ApplicantAuthProvider>
    </MemoryRouter>
  );
}

const SKILLED_PROGRAM = {
  program_id: "SKILLED_189",
  visa_category: "SKILLED",
  subclass_code: "189",
  name: "Skilled Independent visa (subclass 189)",
  country_id: "AU",
  status: "ACTIVE",
  active_rule_count: 2,
  sources: [{
    source_id: "SRC-AU-189-MAIN",
    authority: "Department of Home Affairs",
    url: "https://immi.homeaffairs.gov.au/visas/skilled-independent-189",
    status: "ACTIVE",
    fetch_status: "OK",
    last_checked_at: "2026-10-29T12:00:00Z",
  }],
};

describe("ApplicantPortalPage", () => {
  beforeEach(() => {
    vi.resetAllMocks();
    api.getToken.mockReturnValue(null);
    api.setUnauthorizedHandler.mockImplementation(() => {});
    api.listApplicantPrograms.mockResolvedValue([SKILLED_PROGRAM]);
    api.listApplicantProfiles.mockResolvedValue([]);
    api.listApplicantVisaSelections.mockResolvedValue([]);
    api.saveApplicantVisaSelection.mockResolvedValue({});
    api.listApplicantCases.mockResolvedValue([]);
    api.listApplicantAssessments.mockResolvedValue({ assessments: [], total: 0 });
  });

  it("saves a profile before creating an eligibility assessment", async () => {
    api.createApplicantProfile.mockResolvedValue({
      applicant_id: "applicant-1",
      first_name: "Asha",
      last_name: "Shah",
      age: 31,
      english_level: "competent",
      points: 70,
    });
    api.createApplicantAssessment.mockResolvedValue({
      assessment_id: "assessment-1",
      applicant_id: "applicant-1",
      program_id: "SKILLED_189",
      business_status: "INCOMPLETE",
      overall_status: "UNKNOWN",
      passed_count: 1,
      failed_count: 0,
      unknown_count: 1,
      rule_results: [],
      gaps: [],
    });

    renderPortal();

    fireEvent.change(await screen.findByLabelText("First name"), {
      target: { value: "Asha" },
    });
    fireEvent.change(screen.getByLabelText("Last name"), {
      target: { value: "Shah" },
    });
    fireEvent.change(screen.getByLabelText("Date of birth"), {
      target: { value: "1994-06-08" },
    });
    fireEvent.change(screen.getByLabelText("Passport nationality"), {
      target: { value: "ind" },
    });
    fireEvent.change(screen.getByLabelText("Current Australian visa subclass (if any)"), {
      target: { value: "500" },
    });
    fireEvent.change(screen.getByLabelText("Your age"), {
      target: { value: "31" },
    });
    fireEvent.change(screen.getByLabelText("English proficiency"), {
      target: { value: "competent" },
    });
    fireEvent.change(screen.getByLabelText("Points test score"), {
      target: { value: "70" },
    });
    fireEvent.click(screen.getByRole("button", { name: /Check eligibility/ }));

    await waitFor(() =>
      expect(api.createApplicantProfile).toHaveBeenCalledWith(expect.objectContaining({
        first_name: "Asha",
        last_name: "Shah",
        date_of_birth: "1994-06-08T00:00:00Z",
        passport_nationality: "IND",
        current_visa_subclass: "500",
        visa_subclass: "189",
        age: 31,
        points: 70,
      }))
    );
    await waitFor(() =>
      expect(api.createApplicantAssessment).toHaveBeenCalledWith(
        "applicant-1",
        "SKILLED_189"
      )
    );
    expect(api.saveApplicantVisaSelection).toHaveBeenCalledWith(
      "applicant-1",
      "SKILLED_189",
    );
    expect(await screen.findByText("ASSESSMENT REPORT")).toBeInTheDocument();
    expect(screen.getByText("Needs more information")).toBeInTheDocument();
    expect(screen.getByText(/does not lodge a visa application/)).toBeInTheDocument();
  });

  it("saves answers to missing-information questions and refreshes the assessment", async () => {
    const profile = {
      applicant_id: "applicant-1",
      first_name: "Asha",
      last_name: "Shah",
      age: null,
    };
    api.listApplicantProfiles.mockResolvedValue([profile]);
    api.updateApplicantProfile.mockResolvedValue(profile);
    api.createApplicantAssessment
      .mockResolvedValueOnce({
        assessment_id: "assessment-1",
        applicant_id: "applicant-1",
        program_id: "SKILLED_189",
        business_status: "INCOMPLETE",
        overall_status: "UNKNOWN",
        passed_count: 0,
        failed_count: 0,
        unknown_count: 1,
        rule_results: [],
        gaps: [{
          gap_id: "gap-1",
          field: "age",
          message: "What is your age?",
          answer: null,
        }],
      })
      .mockResolvedValueOnce({
        assessment_id: "assessment-2",
        applicant_id: "applicant-1",
        program_id: "SKILLED_189",
        business_status: "ELIGIBLE",
        overall_status: "PASS",
        passed_count: 1,
        failed_count: 0,
        unknown_count: 0,
        rule_results: [],
        gaps: [],
      });
    api.answerApplicantGap.mockResolvedValue({
      gap_id: "gap-1",
      field: "age",
      answer: 30,
    });

    renderPortal();
    fireEvent.click(await screen.findByRole("button", { name: /Check eligibility/ }));

    const ageAnswer = await screen.findByLabelText("What is your age?");
    fireEvent.change(ageAnswer, { target: { value: "30" } });
    fireEvent.click(screen.getByRole("button", { name: "Save answer" }));

    await waitFor(() => {
      expect(api.answerApplicantGap).toHaveBeenCalledWith(
        "assessment-1",
        "gap-1",
        30,
      );
      expect(api.createApplicantAssessment).toHaveBeenCalledTimes(2);
    });
    expect(await screen.findByText("Eligible")).toBeInTheDocument();
  });

  it("shows a clear disabled state when a program has no active rules", async () => {
    api.listApplicantPrograms.mockResolvedValue([{
      ...SKILLED_PROGRAM,
      active_rule_count: 0,
    }]);

    renderPortal();

    expect(await screen.findByText(/does not have published eligibility rules yet/))
      .toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Check eligibility/ })).toBeDisabled();
  });

  it("shows official sources and source evidence in saved eligibility reports", async () => {
    api.listApplicantProfiles.mockResolvedValue([{
      applicant_id: "applicant-1",
      first_name: "Asha",
      last_name: "Shah",
    }]);
    api.listApplicantAssessments.mockResolvedValue({
      assessments: [{
        assessment_id: "assessment-1",
        program_id: "SKILLED_189",
        business_status: "ELIGIBLE",
        total_rules: 1,
        evaluated_at: "2026-10-29T12:00:00Z",
      }],
      total: 1,
    });
    api.getApplicantAssessment.mockResolvedValue({
      assessment_id: "assessment-1",
      program_id: "SKILLED_189",
      business_status: "ELIGIBLE",
      passed_count: 1,
      failed_count: 0,
      unknown_count: 0,
      rule_results: [{
        result_id: "result-1",
        rule_id: "RULE-189-AGE",
        status: "PASS",
        message: "Under 45",
        evidence_text: "Applicants must be under 45.",
        source_id: "SRC-AU-189-MAIN",
        source_url: "https://immi.homeaffairs.gov.au/visas/skilled-independent-189",
        source_authority: "Department of Home Affairs",
      }],
      gaps: [],
    });

    renderPortal();

    expect(await screen.findByRole("link", {
      name: "Department of Home Affairs",
    })).toHaveAttribute(
      "href",
      "https://immi.homeaffairs.gov.au/visas/skilled-independent-189",
    );
    fireEvent.click(await screen.findByRole("button", { name: /View report/ }));
    expect(await screen.findByText("Official-source evidence: Applicants must be under 45."))
      .toBeInTheDocument();
    expect(screen.getAllByRole("link", {
      name: "Department of Home Affairs",
    })).toHaveLength(2);
  });

  it("prefills an existing account name when its profile has not been saved yet", async () => {
    api.getToken.mockReturnValue("existing-applicant-token");
    api.getMe.mockResolvedValue({
      role: "applicant",
      email: "asha@example.com",
      full_name: "Asha Shah",
    });

    renderPortal();

    expect(await screen.findByLabelText("First name")).toHaveValue("Asha");
    expect(screen.getByLabelText("Last name")).toHaveValue("Shah");
    expect(screen.getByLabelText("Account email")).toHaveValue("asha@example.com");
  });

  it("shows a saved profile and lets the applicant update it", async () => {
    const profile = {
      applicant_id: "applicant-1",
      first_name: "Asha",
      last_name: "Shah",
      passport_nationality: "IND",
      age: 31,
    };
    api.listApplicantProfiles.mockResolvedValue([profile]);
    api.listApplicantVisaSelections.mockResolvedValue([]);
    api.updateApplicantProfile.mockResolvedValue({
      ...profile,
      first_name: "Asha R.",
    });

    renderPortal();

    expect(await screen.findByText("Profile saved to your account")).toBeInTheDocument();
    expect(screen.getByText("Asha Shah")).toBeInTheDocument();
    expect(screen.getByText("IND")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Edit profile" }));
    const firstName = await screen.findByLabelText("First name");
    fireEvent.change(firstName, { target: { value: "Asha R." } });
    fireEvent.click(screen.getByRole("button", { name: "Save profile changes" }));

    await waitFor(() => expect(api.updateApplicantProfile).toHaveBeenCalledWith(
      "applicant-1",
      expect.objectContaining({ first_name: "Asha R." }),
    ));
    expect(await screen.findByText("Asha R. Shah")).toBeInTheDocument();
  });

  it("shows reference programs but does not allow eligibility assessment for them", async () => {
    api.listApplicantPrograms.mockResolvedValue([
      SKILLED_PROGRAM,
      {
        program_id: "REFERENCE_820",
        visa_category: "WORK",
        country_id: "AU",
        subclass_code: "820",
        name: "Reference-only pathway",
        status: "REFERENCE_ONLY",
        active_rule_count: 0,
      },
    ]);

    renderPortal();

    fireEvent.click(await screen.findByRole("button", {
      name: /Reference-only pathway AU · WORK · Subclass 820 Reference/,
    }));
    expect(screen.getByText(/listed for reference/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Check eligibility/ })).toBeDisabled();
  });

  it("shows registration errors without losing the entered details", async () => {
    api.createApplicantProfile.mockRejectedValue(new Error("Could not reach the server."));

    renderPortal();

    fireEvent.change(await screen.findByLabelText("First name"), {
      target: { value: "Asha" },
    });
    fireEvent.change(screen.getByLabelText("Last name"), {
      target: { value: "Shah" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save my profile" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("Could not reach the server.");
    expect(screen.getByLabelText("First name")).toHaveValue("Asha");
  });
});
