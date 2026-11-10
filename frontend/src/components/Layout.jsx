import { useAuth } from "../context/AuthContext.jsx";

const NAV = [
  { group: "Prospect", items: [
    { id: "dashboard", label: "Dashboard" },
    { id: "search", label: "Discovery" },
  ]},
  { group: "Engage", items: [
    { id: "people", label: "My Leads" },
    { id: "crm", label: "CRM & Groups" },
  ]},
  { group: "Workspace", items: [
    { id: "org", label: "Team & Roles" },
    { id: "platform", label: "Operations" },
  ]},
];

export default function Layout({ view, setView, children }) {
  const { user, logout } = useAuth();
  const nav = user?.is_platform_admin
    ? [...NAV, { group: "Operator", items: [{ id: "admin", label: "Admin" }] }]
    : NAV;

  return (
    <div className={`app-shell screen${view === "search" ? " screen--search" : ""}`}>
      <aside className="app-side">
        <div className="app-brand">
          <span className="app-brand-mark">M</span>
          <span>
            Mark
            <small>People · Pipeline</small>
          </span>
        </div>
        <nav className="app-nav" aria-label="Main">
          {nav.map((section) => (
            <div key={section.group} className="app-nav-group">
              <p className="app-nav-label">{section.group}</p>
              {section.items.map((item) => (
                <button
                  key={item.id}
                  type="button"
                  className={`app-nav-btn ${view === item.id ? "active" : ""}`}
                  onClick={() => setView(item.id)}
                >
                  {item.label}
                </button>
              ))}
            </div>
          ))}
        </nav>
        <div className="app-user">
          <strong title={user.full_name}>{user.full_name}</strong>
          <span>{user.account_type}</span>
          <button type="button" className="app-signout" onClick={logout}>
            Sign out
          </button>
        </div>
      </aside>
      <main className="content">{children}</main>
    </div>
  );
}
