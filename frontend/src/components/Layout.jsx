import { useAuth } from "../context/AuthContext.jsx";

const NAV = [
  { id: "dashboard", label: "Dashboard", short: "Home" },
  { id: "search", label: "New search", short: "Search" },
  { id: "org", label: "Organization", short: "Team" },
];

export default function Layout({ view, setView, children }) {
  const { user, logout } = useAuth();

  return (
    <div className={`screen${view === "search" ? " screen--search" : ""}`}>
      <div className="atmosphere" aria-hidden="true" />
      <header className="topbar">
        <span className="brand-sm">Mark</span>
        <nav className="nav" aria-label="Main">
          {NAV.map((item) => (
            <button
              key={item.id}
              type="button"
              className={`nav-btn ${view === item.id ? "active" : ""}`}
              onClick={() => setView(item.id)}
            >
              <span className="nav-label-full">{item.label}</span>
              <span className="nav-label-short">{item.short}</span>
            </button>
          ))}
        </nav>
        <div className="user-bar">
          <span className="user-name" title={user.full_name}>
            {user.full_name}
          </span>
          <span className="user-type">({user.account_type})</span>
          <button type="button" className="ghost btn-signout" onClick={logout}>
            Sign out
          </button>
        </div>
      </header>
      <main className="content">{children}</main>
    </div>
  );
}
