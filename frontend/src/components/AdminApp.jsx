import { useEffect, useState } from "react";
import {
  ADMIN_AUTH_EXPIRED_EVENT,
  adminApi,
  clearAdminSession,
  getAdminToken,
  setAdminToken,
} from "../api/client.js";
import AdminConsole from "./AdminConsole.jsx";

export default function AdminApp() {
  const [operator, setOperator] = useState(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    if (!getAdminToken()) {
      setLoading(false);
      return;
    }
    adminApi("/api/auth/admin/me")
      .then(setOperator)
      .catch(() => setOperator(null))
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => {
    function onExpired() {
      setOperator(null);
    }
    window.addEventListener(ADMIN_AUTH_EXPIRED_EVENT, onExpired);
    return () => window.removeEventListener(ADMIN_AUTH_EXPIRED_EVENT, onExpired);
  }, []);

  function signOut() {
    const token = getAdminToken();
    clearAdminSession();
    setOperator(null);
    if (token) {
      fetch("/api/auth/admin/logout", {
        method: "POST",
        headers: { Authorization: `Bearer ${token}` },
      }).catch(() => {});
    }
  }

  if (loading) {
    return (
      <div className="screen">
        <div className="empty" style={{ margin: "4rem auto", maxWidth: 320 }}>Loading…</div>
      </div>
    );
  }

  if (!operator) return <AdminLogin onSuccess={setOperator} />;

  return (
    <div className="app-shell screen">
      <aside className="app-side">
        <div className="app-brand">
          <span className="app-brand-mark">M</span>
          <span>
            Mark
            <small>Operator</small>
          </span>
        </div>
        <div className="app-user">
          <strong title={operator.full_name}>{operator.full_name}</strong>
          <span>{operator.email}</span>
          <button type="button" className="app-signout" onClick={signOut}>Sign out</button>
        </div>
      </aside>
      <main className="content">
        <AdminConsole />
      </main>
    </div>
  );
}

function AdminLogin({ onSuccess }) {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [pending, setPending] = useState(false);

  async function submit(event) {
    event.preventDefault();
    setError("");
    setPending(true);
    try {
      const data = await adminApi("/api/auth/admin/login", {
        method: "POST",
        body: { email, password, next: "/admin" },
      });
      setAdminToken(data.access_token);
      onSuccess(data.operator);
    } catch (err) {
      setError(err.message);
    } finally {
      setPending(false);
    }
  }

  return (
    <div className="screen">
      <div className="atmosphere" aria-hidden="true" />
      <div className="auth-card">
        <a className="auth-back" href="/">← Customer site</a>
        <h1 className="brand">Mark</h1>
        <p className="auth-sub">Operator sign-in</p>
        <p className="lede">Customer accounts do not open this console. Use the operator account that was granted separately.</p>
        <form className="auth-form" onSubmit={submit}>
          <input
            type="email"
            placeholder="Email"
            autoComplete="username"
            required
            value={email}
            onChange={(event) => setEmail(event.target.value)}
          />
          <input
            type="password"
            placeholder="Password"
            autoComplete="current-password"
            required
            value={password}
            onChange={(event) => setPassword(event.target.value)}
          />
          <button type="submit" disabled={pending}>{pending ? "Signing in…" : "Sign in"}</button>
        </form>
        {error && <p className="error-msg">{error}</p>}
      </div>
    </div>
  );
}
