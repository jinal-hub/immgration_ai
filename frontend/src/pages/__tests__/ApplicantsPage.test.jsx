import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import ApplicantsPage from "../ApplicantsPage.jsx";
import * as api from "../../api/client.js";

vi.mock("../../api/client.js");

describe("ApplicantsPage", () => {
  beforeEach(() => {
    vi.resetAllMocks();
    api.listApplicants.mockResolvedValue({
      applicants: [{
        applicant_id: "account-1",
        user_id: "account-1",
        owner_email: "new-applicant@example.com",
        profile_exists: false,
        first_name: "New",
        last_name: "Applicant",
      }],
      total: 1,
      limit: 25,
      offset: 0,
    });
  });

  it("shows accounts that have not submitted their application profile", async () => {
    render(
      <MemoryRouter>
        <ApplicantsPage />
      </MemoryRouter>
    );

    expect(await screen.findByText("new-applicant@example.com")).toBeInTheDocument();
    expect(screen.getByText("Not started")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "New Applicant" })).toHaveAttribute(
      "href",
      "/applicants/account-1"
    );
    expect(screen.getByRole("button", { name: "Refresh" })).toBeInTheDocument();
  });
});
