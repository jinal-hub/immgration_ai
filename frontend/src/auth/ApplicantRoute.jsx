import { Navigate, useLocation } from "react-router-dom";
import { useApplicantAuth } from "./ApplicantAuthContext.jsx";

export default function ApplicantRoute({ children }) {
  const { status } = useApplicantAuth();
  const location = useLocation();

  if (status === "loading") return null;
  if (status === "unauthed") {
    return <Navigate to="/portal/login" state={{ from: location }} replace />;
  }
  return children;
}
