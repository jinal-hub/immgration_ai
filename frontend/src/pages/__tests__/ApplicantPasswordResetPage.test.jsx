import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import ApplicantPasswordResetPage from "../ApplicantPasswordResetPage.jsx";
import * as api from "../../api/client.js";

vi.mock("../../api/client.js");

function renderPage(mode, path = "/portal/forgot-password") {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <ApplicantPasswordResetPage mode={mode} />
    </MemoryRouter>
  );
}

describe("ApplicantPasswordResetPage", () => {
  beforeEach(() => {
    vi.resetAllMocks();
  });

  it("requests a password-reset email and reports the API response", async () => {
    api.requestApplicantPasswordReset.mockResolvedValue({
      message: "If an active account exists for that email, a password-reset link has been sent.",
    });
    renderPage("request");

    fireEvent.change(screen.getByLabelText("Email address"), {
      target: { value: "person@example.com" },
    });
    fireEvent.click(screen.getByRole("button", { name: /Send reset link/ }));

    await waitFor(() => expect(api.requestApplicantPasswordReset).toHaveBeenCalledWith(
      "person@example.com"
    ));
    expect(await screen.findByRole("status")).toHaveTextContent(/password-reset link has been sent/);
  });

  it("confirms a new password with the reset token", async () => {
    api.confirmApplicantPasswordReset.mockResolvedValue({
      message: "Your password has been reset. You can now sign in.",
    });
    renderPage("reset", "/portal/reset-password?token=sample-reset-token");

    fireEvent.change(screen.getByLabelText("New password"), {
      target: { value: "a-new-password" },
    });
    fireEvent.change(screen.getByLabelText("Confirm new password"), {
      target: { value: "a-new-password" },
    });
    fireEvent.click(screen.getByRole("button", { name: /Save new password/ }));

    await waitFor(() => expect(api.confirmApplicantPasswordReset).toHaveBeenCalledWith(
      "sample-reset-token",
      "a-new-password"
    ));
    expect(await screen.findByRole("status")).toHaveTextContent(/password has been reset/);
  });

  it("rejects mismatched passwords without sending the reset request", async () => {
    renderPage("reset", "/portal/reset-password?token=sample-reset-token");

    fireEvent.change(screen.getByLabelText("New password"), {
      target: { value: "a-new-password" },
    });
    fireEvent.change(screen.getByLabelText("Confirm new password"), {
      target: { value: "different-password" },
    });
    fireEvent.click(screen.getByRole("button", { name: /Save new password/ }));

    expect(await screen.findByRole("alert")).toHaveTextContent("Passwords do not match.");
    expect(api.confirmApplicantPasswordReset).not.toHaveBeenCalled();
  });
});
