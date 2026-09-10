import { useAuth } from "../context/AuthContext.jsx";

const NAV = [
  { id: "dashboard", label: "Dashboard" },
  { id: "search", label: "New search" },
  { id: "org", label: "Organization" },
];

export default function Layout({ view, setView, children }) {
  const { user, logout } = useAuth();

  return (
    <div className="screen">
      <div className="atmosphere" aria-hidden="true" />
      <header className="topbar">
        <span className="brand-sm">Mark</span>
        <nav className="nav">
          {NAV.map((item) => (
            <button
              key={item.id}
              type="button"
              className={`nav-btn ${view === item.id ? "active" : ""}`}
              onClick={() => setView(item.id)}
            >
              {item.label}
            </button>
          ))}
        </nav>
        <div className="user-bar">
          <span>{user.full_name} ({user.account_type})</span>
          <button type="button" className="ghost" onClick={logout}>
            Sign out
          </button>
        </div>
      </header>
      <main className="content">{children}</main>
    </div>
  );
}
