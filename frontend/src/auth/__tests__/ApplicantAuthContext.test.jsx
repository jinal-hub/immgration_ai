import { describe, it, expect, vi, beforeEach } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { useState } from "react";
import { ApplicantAuthProvider, useApplicantAuth } from "../ApplicantAuthContext.jsx";
import * as api from "../../api/client.js";

vi.mock("../../api/client.js");

function Probe() {
  const { status, user, login, register, logout } = useApplicantAuth();
  const [error, setError] = useState("");
  return (
    <div>
      <span data-testid="status">{status}</span>
      <span data-testid="email">{user?.email || ""}</span>
      {error && <span role="alert">{error}</span>}
      <button onClick={() => login("user@example.com", "password123").catch((err) => setError(err.message))}>Login</button>
      <button onClick={() => register("Applicant User", "new@example.com", "password123").catch((err) => setError(err.message))}>Register</button>
      <button onClick={logout}>Logout</button>
    </div>
  );
}

function renderProvider() {
  return render(
    <ApplicantAuthProvider>
      <Probe />
    </ApplicantAuthProvider>
  );
}

describe("ApplicantAuthContext", () => {
  beforeEach(() => {
    vi.resetAllMocks();
    api.getToken.mockReturnValue(null);
    api.setUnauthorizedHandler.mockImplementation(() => {});
  });

  it("uses a separate applicant session and rejects admin accounts", async () => {
    renderProvider();
    await waitFor(() => expect(screen.getByTestId("status")).toHaveTextContent("unauthed"));
    api.loginApplicant.mockResolvedValue({
      access_token: "applicant-jwt",
      role: "applicant",
    });
    api.getMe.mockResolvedValue({ role: "applicant", email: "user@example.com" });

    fireEvent.click(screen.getByRole("button", { name: "Login" }));

    await waitFor(() => expect(screen.getByTestId("status")).toHaveTextContent("authed"));
    expect(api.setToken).toHaveBeenCalledWith("applicant-jwt", "applicant");
    expect(api.getMe).toHaveBeenCalledWith("applicant");
    expect(screen.getByTestId("email")).toHaveTextContent("user@example.com");
    expect(api.setUnauthorizedHandler).toHaveBeenCalledWith(expect.any(Function), "applicant");
  });

  it("registers applicant accounts and does not persist admin tokens", async () => {
    renderProvider();
    await waitFor(() => expect(screen.getByTestId("status")).toHaveTextContent("unauthed"));
    api.registerApplicant.mockResolvedValue({
      access_token: "new-applicant-jwt",
      role: "applicant",
    });
    api.getMe.mockResolvedValue({ role: "applicant", email: "new@example.com" });

    fireEvent.click(screen.getByRole("button", { name: "Register" }));

    await waitFor(() =>
      expect(api.registerApplicant).toHaveBeenCalledWith(
        "new@example.com",
        "password123",
        "Applicant User"
      )
    );
    expect(api.setToken).toHaveBeenCalledWith("new-applicant-jwt", "applicant");
  });

  it("keeps an admin sign-in out of the applicant session", async () => {
    renderProvider();
    await waitFor(() => expect(screen.getByTestId("status")).toHaveTextContent("unauthed"));
    api.loginApplicant.mockResolvedValue({
      access_token: "admin-jwt",
      role: "admin",
    });

    fireEvent.click(screen.getByRole("button", { name: "Login" }));

    await waitFor(() => expect(screen.getByTestId("status")).toHaveTextContent("unauthed"));
    expect(await screen.findByRole("alert")).toHaveTextContent("This sign-in is for applicant accounts");
    expect(api.setToken).not.toHaveBeenCalled();
  });
});
