import { useState } from "react";
import * as api from "../api/client.js";

export default function TenantInvitationAcceptPanel({ onAccepted }) {
  const [token, setToken] = useState("");
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function submit(event) {
    event.preventDefault();
    setBusy(true);
    setError("");
    setMessage("");
    try {
      const membership = await api.acceptTenantInvitation(token.trim());
      setToken("");
      setMessage(`Joined consultancy as ${membership.role.toLowerCase()}.`);
      await onAccepted?.();
    } catch (requestError) {
      setError(requestError.message || "Could not accept this invitation.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="portal-panel">
      <span className="portal-eyebrow">JOIN A CONSULTANCY</span>
      <h2>Accept an invitation</h2>
      <p className="portal-panel-intro">
        Sign in using the invited email address, then enter the one-time invitation token.
      </p>
      <form className="case-invitation-form" onSubmit={submit}>
        <label className="portal-field" htmlFor="tenant-invitation-token">
          <span>Invitation token</span>
          <input
            autoComplete="off"
            id="tenant-invitation-token"
            maxLength={256}
            required
            value={token}
            onChange={(event) => setToken(event.target.value)}
          />
        </label>
        <button className="portal-primary-button case-action-button" disabled={busy} type="submit">
          {busy ? "Joining…" : "Accept invitation"}
        </button>
      </form>
      {error && <p className="portal-form-error" role="alert">{error}</p>}
      {message && <p className="portal-success-message" role="status">{message}</p>}
    </section>
  );
}
