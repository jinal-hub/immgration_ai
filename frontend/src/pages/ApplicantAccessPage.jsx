import { useEffect, useState } from "react";
import { Link, Navigate, useLocation, useNavigate } from "react-router-dom";
import { useApplicantAuth } from "../auth/ApplicantAuthContext.jsx";

export default function ApplicantAccessPage({ mode }) {
  const isRegister = mode === "register";
  const { status, login, register } = useApplicantAuth();
  const location = useLocation();
  const navigate = useNavigate();
  const [fullName, setFullName] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    document.title = `${isRegister ? "Create account" : "Sign in"} | Pathway`;
  }, [isRegister]);

  if (status === "authed") return <Navigate to="/portal" replace />;

  async function handleSubmit(event) {
    event.preventDefault();
    setError("");
    if (isRegister && password !== confirmPassword) {
      setError("Passwords do not match.");
      return;
    }

    setBusy(true);
    try {
      if (isRegister) await register(fullName, email, password);
      else await login(email, password);
      navigate(location.state?.from?.pathname || "/portal", { replace: true });
    } catch (requestError) {
      setError(requestError.message || (isRegister ? "Could not create account." : "Sign-in failed."));
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
          <span className="portal-eyebrow">YOUR AUSTRALIA JOURNEY</span>
          <h1>Make your next step a more informed one.</h1>
          <p>Keep your profile and visa eligibility checks together in one private workspace.</p>
        </div>
        <div className="portal-aside-note">
          <span aria-hidden="true">✦</span>
          <p>Clear answers start with the details that make your situation yours.</p>
        </div>
      </section>

      <section className="portal-access-main">
        <div className="portal-access-card">
          <span className="portal-eyebrow">{isRegister ? "GET STARTED" : "WELCOME BACK"}</span>
          <h2>{isRegister ? "Create your account" : "Sign in to your account"}</h2>
          <p className="portal-subtitle">
            {isRegister
              ? "Build your profile and get a personalized eligibility check."
              : "Pick up where you left off with your saved profile and checks."}
          </p>

          <form onSubmit={handleSubmit}>
            {isRegister && (
              <div className="portal-field">
                <label htmlFor="applicant-name">Full name</label>
                <input
                  autoComplete="name"
                  id="applicant-name"
                  maxLength={128}
                  onChange={(event) => setFullName(event.target.value)}
                  required
                  value={fullName}
                />
              </div>
            )}
            <div className="portal-field">
              <label htmlFor="applicant-email">Email address</label>
              <input
                autoComplete="email"
                id="applicant-email"
                onChange={(event) => setEmail(event.target.value)}
                required
                type="email"
                value={email}
              />
            </div>
            <div className="portal-field">
              <label htmlFor="applicant-password">Password</label>
              <input
                autoComplete={isRegister ? "new-password" : "current-password"}
                id="applicant-password"
                minLength={isRegister ? 8 : undefined}
                onChange={(event) => setPassword(event.target.value)}
                required
                type="password"
                value={password}
              />
              {isRegister && <span className="portal-field-hint">Use at least 8 characters.</span>}
            </div>
            {isRegister && (
              <div className="portal-field">
                <label htmlFor="applicant-confirm-password">Confirm password</label>
                <input
                  autoComplete="new-password"
                  id="applicant-confirm-password"
                  onChange={(event) => setConfirmPassword(event.target.value)}
                  required
                  type="password"
                  value={confirmPassword}
                />
              </div>
            )}

            {error && <p className="portal-form-error" role="alert">{error}</p>}
            <button className="portal-primary-button" disabled={busy} type="submit">
              {busy ? "Please wait…" : isRegister ? "Create account" : "Sign in"}
              {!busy && <span aria-hidden="true">→</span>}
            </button>
          </form>

          <p className="portal-access-switch">
            {isRegister ? "Already have an account?" : "New to Pathway?"}{" "}
            <Link to={isRegister ? "/portal/login" : "/portal/register"}>
              {isRegister ? "Sign in" : "Create an account"}
            </Link>
          </p>
          {!isRegister && (
            <p className="portal-access-switch">
              <Link to="/portal/forgot-password">Forgot your password?</Link>
            </p>
          )}
        </div>
        <Link className="portal-admin-link" to="/login">Administrator sign in</Link>
      </section>
    </main>
  );
}
