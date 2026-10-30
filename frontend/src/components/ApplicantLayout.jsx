import { Link, Outlet, useNavigate } from "react-router-dom";
import { useApplicantAuth } from "../auth/ApplicantAuthContext.jsx";

export default function ApplicantLayout() {
  const { user, logout } = useApplicantAuth();
  const navigate = useNavigate();

  function signOut() {
    logout();
    navigate("/portal/login", { replace: true });
  }

  return (
    <div className="portal-shell">
      <header className="portal-topbar">
        <Link className="portal-brand" to="/portal">
          <span className="portal-brand-mark">IK</span>
          <span>Pathway</span>
        </Link>
        <nav className="portal-nav" aria-label="Applicant and consultancy navigation">
          <Link to="/portal">Profile</Link>
          <Link to="/portal/cases">My applications</Link>
          <Link to="/consultancy">Consultancy</Link>
        </nav>
        <div className="portal-user">
          <div className="portal-avatar" aria-hidden="true">
            {(user?.full_name || user?.email || "A").slice(0, 1).toUpperCase()}
          </div>
          <span className="portal-user-name">{user?.full_name || user?.email}</span>
          <button className="portal-signout" onClick={signOut} type="button">Sign out</button>
        </div>
      </header>
      <main className="portal-main">
        <Outlet />
      </main>
      <footer className="portal-footer">
        <span>Pathway · Australian visa eligibility</span>
        <span>Private applicant workspace</span>
      </footer>
    </div>
  );
}
