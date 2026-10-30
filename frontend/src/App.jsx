import { Navigate, Route, Routes } from "react-router-dom";
import { AuthProvider } from "./auth/AuthContext.jsx";
import AdminRoute from "./auth/AdminRoute.jsx";
import { ApplicantAuthProvider } from "./auth/ApplicantAuthContext.jsx";
import ApplicantRoute from "./auth/ApplicantRoute.jsx";
import { ToastProvider } from "./components/ToastContext.jsx";
import Layout from "./components/Layout.jsx";
import ApplicantLayout from "./components/ApplicantLayout.jsx";
import LoginPage from "./pages/LoginPage.jsx";
import ApplicantAccessPage from "./pages/ApplicantAccessPage.jsx";
import ApplicantPasswordResetPage from "./pages/ApplicantPasswordResetPage.jsx";
import ApplicantPortalPage from "./pages/ApplicantPortalPage.jsx";
import ApplicantCasesPage from "./pages/ApplicantCasesPage.jsx";
import ConsultancyPortalPage from "./pages/ConsultancyPortalPage.jsx";
import DashboardPage from "./pages/DashboardPage.jsx";
import SourcesPage from "./pages/SourcesPage.jsx";
import SourceDetailPage from "./pages/SourceDetailPage.jsx";
import ReviewPage from "./pages/ReviewPage.jsx";
import RulesPage from "./pages/RulesPage.jsx";
import DocumentsPage from "./pages/DocumentsPage.jsx";
import ApplicantsPage from "./pages/ApplicantsPage.jsx";
import ApplicantDetailPage from "./pages/ApplicantDetailPage.jsx";
import SchedulerPage from "./pages/SchedulerPage.jsx";

export default function App() {
  return (
    <ToastProvider>
      <AuthProvider>
        <ApplicantAuthProvider>
          <Routes>
            <Route path="/login" element={<LoginPage />} />
            <Route path="/portal/login" element={<ApplicantAccessPage mode="login" />} />
            <Route path="/portal/register" element={<ApplicantAccessPage mode="register" />} />
            <Route
              path="/portal/forgot-password"
              element={<ApplicantPasswordResetPage mode="request" />}
            />
            <Route
              path="/portal/reset-password"
              element={<ApplicantPasswordResetPage mode="reset" />}
            />
            <Route
              path="/portal"
              element={
                <ApplicantRoute>
                  <ApplicantLayout />
                </ApplicantRoute>
              }
            >
              <Route index element={<ApplicantPortalPage />} />
              <Route path="cases" element={<ApplicantCasesPage />} />
            </Route>
            <Route
              path="/consultancy"
              element={
                <ApplicantRoute>
                  <ApplicantLayout />
                </ApplicantRoute>
              }
            >
              <Route index element={<ConsultancyPortalPage />} />
            </Route>
            <Route
              path="/"
              element={
                <AdminRoute>
                  <Layout />
                </AdminRoute>
              }
            >
              <Route index element={<DashboardPage />} />
              <Route path="sources" element={<SourcesPage />} />
              <Route path="sources/:sourceId" element={<SourceDetailPage />} />
              <Route path="review" element={<ReviewPage />} />
              <Route path="rules" element={<RulesPage />} />
              <Route path="documents" element={<DocumentsPage />} />
              <Route path="applicants" element={<ApplicantsPage />} />
              <Route path="applicants/:applicantId" element={<ApplicantDetailPage />} />
              <Route path="scheduler" element={<SchedulerPage />} />
            </Route>
            <Route path="*" element={<Navigate to="/" replace />} />
          </Routes>
        </ApplicantAuthProvider>
      </AuthProvider>
    </ToastProvider>
  );
}
