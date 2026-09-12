import { useState } from "react";
import { useAuth } from "../context/AuthContext.jsx";

export default function AuthScreen({ initialTab = "login", onBack }) {
  const { login, register } = useAuth();
  const [tab, setTab] = useState(initialTab);
  const [error, setError] = useState("");
  const [accountType, setAccountType] = useState("individual");

  const [loginForm, setLoginForm] = useState({ email: "", password: "" });
  const [regForm, setRegForm] = useState({
    full_name: "",
    email: "",
    password: "",
    organization_name: "",
  });

  async function handleLogin(e) {
    e.preventDefault();
    setError("");
    try {
      await login(loginForm.email, loginForm.password);
    } catch (err) {
      setError(err.message);
    }
  }

  async function handleRegister(e) {
    e.preventDefault();
    setError("");
    try {
      await register({
        email: regForm.email,
        password: regForm.password,
        full_name: regForm.full_name,
        account_type: accountType,
        organization_name: accountType === "organization" ? regForm.organization_name : null,
      });
    } catch (err) {
      setError(err.message);
    }
  }

  return (
    <div className="screen">
      <div className="atmosphere" aria-hidden="true" />
      <div className="auth-card">
        {onBack && (
          <button type="button" className="auth-back" onClick={onBack}>
            ← Back to home
          </button>
        )}
        <h1 className="brand">Mark</h1>
        <p className="auth-sub">Lead discovery for individuals &amp; teams</p>

        <div className="tabs">
          <button
            type="button"
            className={`tab ${tab === "login" ? "active" : ""}`}
            onClick={() => { setTab("login"); setError(""); }}
          >
            Sign in
          </button>
          <button
            type="button"
            className={`tab ${tab === "register" ? "active" : ""}`}
            onClick={() => { setTab("register"); setError(""); }}
          >
            Register
          </button>
        </div>

        {tab === "login" ? (
          <form className="auth-form" onSubmit={handleLogin}>
            <input
              type="email"
              placeholder="Email"
              required
              value={loginForm.email}
              onChange={(e) => setLoginForm({ ...loginForm, email: e.target.value })}
            />
            <input
              type="password"
              placeholder="Password"
              required
              value={loginForm.password}
              onChange={(e) => setLoginForm({ ...loginForm, password: e.target.value })}
            />
            <button type="submit">Sign in</button>
          </form>
        ) : (
          <form className="auth-form" onSubmit={handleRegister}>
            <input
              type="text"
              placeholder="Full name"
              required
              value={regForm.full_name}
              onChange={(e) => setRegForm({ ...regForm, full_name: e.target.value })}
            />
            <input
              type="email"
              placeholder="Email"
              required
              value={regForm.email}
              onChange={(e) => setRegForm({ ...regForm, email: e.target.value })}
            />
            <input
              type="password"
              placeholder="Password (8+ chars)"
              minLength={8}
              required
              value={regForm.password}
              onChange={(e) => setRegForm({ ...regForm, password: e.target.value })}
            />
            <div className="radio-row">
              <span>Account type</span>
              <label>
                <input
                  type="radio"
                  name="acct"
                  value="individual"
                  checked={accountType === "individual"}
                  onChange={() => setAccountType("individual")}
                />{" "}
                Individual
              </label>
              <label>
                <input
                  type="radio"
                  name="acct"
                  value="organization"
                  checked={accountType === "organization"}
                  onChange={() => setAccountType("organization")}
                />{" "}
                Organization
              </label>
            </div>
            {accountType === "organization" && (
              <input
                type="text"
                placeholder="Organization name"
                required
                value={regForm.organization_name}
                onChange={(e) => setRegForm({ ...regForm, organization_name: e.target.value })}
              />
            )}
            <button type="submit">Create account</button>
          </form>
        )}

        {error && <p className="error-msg">{error}</p>}
      </div>
    </div>
  );
}
