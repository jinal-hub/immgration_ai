import { createContext, useCallback, useContext, useEffect, useState } from "react";
import * as api from "../api/client.js";

const ApplicantAuthContext = createContext(null);

export function ApplicantAuthProvider({ children }) {
  const [status, setStatus] = useState("loading");
  const [user, setUser] = useState(null);

  const logout = useCallback(() => {
    api.clearToken("applicant");
    setUser(null);
    setStatus("unauthed");
  }, []);

  useEffect(() => {
    api.setUnauthorizedHandler(logout, "applicant");
  }, [logout]);

  useEffect(() => {
    if (!api.getToken("applicant")) {
      setStatus("unauthed");
      return;
    }
    api.getMe("applicant").then((me) => {
      if (me.role !== "applicant") {
        logout();
        return;
      }
      setUser(me);
      setStatus("authed");
    }).catch(() => {
      logout();
    });
  }, [logout]);

  const authenticate = useCallback(async (result) => {
    if (result.role !== "applicant") {
      throw new Error("This sign-in is for applicant accounts. Use the admin sign-in instead.");
    }
    api.setToken(result.access_token, "applicant");
    try {
      const me = await api.getMe("applicant");
      if (me.role !== "applicant") {
        throw new Error("This sign-in is for applicant accounts.");
      }
      setUser(me);
      setStatus("authed");
      return me;
    } catch (error) {
      api.clearToken("applicant");
      throw error;
    }
  }, []);

  const login = useCallback(async (email, password) => {
    const result = await api.loginApplicant(email, password);
    return authenticate(result);
  }, [authenticate]);

  const register = useCallback(async (fullName, email, password) => {
    const result = await api.registerApplicant(email, password, fullName);
    return authenticate(result);
  }, [authenticate]);

  return (
    <ApplicantAuthContext.Provider value={{ status, user, login, register, logout }}>
      {children}
    </ApplicantAuthContext.Provider>
  );
}

export function useApplicantAuth() {
  const context = useContext(ApplicantAuthContext);
  if (!context) {
    throw new Error("useApplicantAuth must be used within ApplicantAuthProvider");
  }
  return context;
}
