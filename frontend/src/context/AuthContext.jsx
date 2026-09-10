import { createContext, useCallback, useContext, useEffect, useState } from "react";
import { api, getToken, setToken } from "../api/client.js";

const USER_KEY = "mark_user";
const AuthContext = createContext(null);

export function AuthProvider({ children }) {
  const [user, setUser] = useState(() => {
    try {
      return JSON.parse(localStorage.getItem(USER_KEY) || "null");
    } catch {
      return null;
    }
  });
  const [loading, setLoading] = useState(!!getToken());

  useEffect(() => {
    if (!getToken()) {
      setLoading(false);
      return;
    }
    api("/api/auth/me")
      .then((u) => {
        setUser(u);
        localStorage.setItem(USER_KEY, JSON.stringify(u));
      })
      .catch(() => {
        setToken(null);
        setUser(null);
        localStorage.removeItem(USER_KEY);
      })
      .finally(() => setLoading(false));
  }, []);

  const login = useCallback(async (email, password) => {
    const data = await api("/api/auth/login", {
      method: "POST",
      body: { email, password },
    });
    setToken(data.access_token);
    setUser(data.user);
    localStorage.setItem(USER_KEY, JSON.stringify(data.user));
    return data.user;
  }, []);

  const register = useCallback(async (payload) => {
    const data = await api("/api/auth/register", {
      method: "POST",
      body: payload,
    });
    setToken(data.access_token);
    setUser(data.user);
    localStorage.setItem(USER_KEY, JSON.stringify(data.user));
    return data.user;
  }, []);

  const logout = useCallback(() => {
    setToken(null);
    setUser(null);
    localStorage.removeItem(USER_KEY);
  }, []);

  return (
    <AuthContext.Provider value={{ user, loading, login, register, logout }}>
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth() {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used within AuthProvider");
  return ctx;
}
