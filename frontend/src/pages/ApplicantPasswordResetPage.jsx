import { useEffect, useState } from "react";
import { Link, useLocation } from "react-router-dom";
import * as api from "../api/client.js";

export default function ApplicantPasswordResetPage({ mode }) {
  const isReset = mode === "reset";
  const location = useLocation();
  const token = new URLSearchParams(location.search).get("token") || "";
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    document.title = `${isReset ? "Reset password" : "Forgot password"} | Pathway`;
  }, [isReset]);

  async function handleSubmit(event) {
    event.preventDefault();
    setError("");
    setMessage("");
    if (isReset && !token) {
      setError("This reset link is incomplete. Request a new password-reset email.");
      return;
    }
    if (isReset && password !== confirmPassword) {
      setError("Passwords do not match.");
      return;
    }

    setBusy(true);
    try {
      const result = isReset
        ? await api.confirmApplicantPasswordReset(token, password)
        : await api.requestApplicantPasswordReset(email);
      setMessage(result.message);
    } catch (requestError) {
      setError(requestError.message || "Could not complete the password reset.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="portal-access">
      <section className="portal-access-aside">
        <Link className="portal-brand" to="/portal/login">
          <span className="portal-brand-mark">IK</span>
          <span>Pathway</span>
        </Link>
        <div className="portal-aside-copy">
          <span className="portal-eyebrow">ACCOUNT RECOVERY</span>
          <h1>Back to your pathway.</h1>
          <p>Reset your password securely and continue with your saved applicant workspace.</p>
        </div>
      </section>

      <section className="portal-access-main">
        <div className="portal-access-card">
          <span className="portal-eyebrow">{isReset ? "CHOOSE A NEW PASSWORD" : "ACCOUNT RECOVERY"}</span>
          <h2>{isReset ? "Reset your password" : "Forgot your password?"}</h2>
          <p className="portal-subtitle">
            {isReset
              ? "Choose a new password for your applicant account."
              : "Enter your account email and we’ll send a reset link if it matches an active account."}
          </p>
          <form onSubmit={handleSubmit}>
            {isReset ? (
              <>
                <div className="portal-field">
                  <label htmlFor="reset-password">New password</label>
                  <input
                    autoComplete="new-password"
                    id="reset-password"
                    minLength={8}
                    onChange={(event) => setPassword(event.target.value)}
                    required
                    type="password"
                    value={password}
                  />
                  <span className="portal-field-hint">Use at least 8 characters.</span>
                </div>
                <div className="portal-field">
                  <label htmlFor="reset-confirm-password">Confirm new password</label>
                  <input
                    autoComplete="new-password"
                    id="reset-confirm-password"
                    onChange={(event) => setConfirmPassword(event.target.value)}
                    required
                    type="password"
                    value={confirmPassword}
                  />
                </div>
              </>
            ) : (
              <div className="portal-field">
                <label htmlFor="reset-email">Email address</label>
                <input
                  autoComplete="email"
                  id="reset-email"
                  onChange={(event) => setEmail(event.target.value)}
                  required
                  type="email"
                  value={email}
                />
              </div>
            )}
            {error && <p className="portal-form-error" role="alert">{error}</p>}
            {message && <p className="portal-success-message" role="status">{message}</p>}
            <button className="portal-primary-button" disabled={busy} type="submit">
              {busy ? "Please wait…" : isReset ? "Save new password" : "Send reset link"}
              {!busy && <span aria-hidden="true">→</span>}
            </button>
          </form>
          <p className="portal-access-switch">
            <Link to="/portal/login">Back to sign in</Link>
          </p>
        </div>
        <Link className="portal-admin-link" to="/login">Administrator sign in</Link>
      </section>
    </main>
  );
}
