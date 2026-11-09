import { useCallback, useEffect, useState } from "react";
import { api } from "../api/client.js";

export default function OrganizationView() {
  const [orgs, setOrgs] = useState([]);
  const [activeOrgId, setActiveOrgId] = useState(null);
  const [members, setMembers] = useState([]);
  const [auditLogs, setAuditLogs] = useState([]);
  const [wsName, setWsName] = useState("");
  const [email, setEmail] = useState("");
  const [role, setRole] = useState("member");
  const [message, setMessage] = useState("");

  const load = useCallback(async () => {
    try {
      const data = await api("/api/organizations");
      setOrgs(data);
      const firstId = activeOrgId || data[0]?.id || null;
      setActiveOrgId(firstId);
      if (firstId) {
        const [mRes, aRes] = await Promise.all([
          api(`/api/organizations/${firstId}/members`).catch(() => ({ items: [] })),
          api(`/api/organizations/${firstId}/audit-logs`).catch(() => ({ items: [] })),
        ]);
        setMembers(mRes.items || []);
        setAuditLogs(aRes.items || []);
      }
    } catch {
      setOrgs([]);
    }
  }, [activeOrgId]);

  useEffect(() => { load(); }, [load]);

  async function createWorkspace(e) {
    e.preventDefault();
    if (!wsName.trim()) return;
    setMessage("");
    try {
      const created = await api("/api/organizations", {
        method: "POST",
        body: { name: wsName.trim() },
      });
      setWsName("");
      setActiveOrgId(created.id);
      setMessage(`Workspace "${created.name}" created.`);
      load();
    } catch (err) {
      setMessage(err.message);
    }
  }

  async function handleInvite(e) {
    e.preventDefault();
    if (!activeOrgId) return;
    setMessage("");
    try {
      await api(`/api/organizations/${activeOrgId}/members`, {
        method: "POST",
        body: { email, role },
      });
      setEmail("");
      setMessage("Member invited.");
      load();
    } catch (err) {
      setMessage(err.message);
    }
  }

  async function changeRole(userId, nextRole) {
    if (!activeOrgId) return;
    try {
      await api(`/api/organizations/${activeOrgId}/members/${userId}`, {
        method: "PATCH",
        body: { role: nextRole },
      });
      load();
    } catch (err) {
      setMessage(err.message);
    }
  }

  return (
    <section className="view">
      <p className="app-kicker">Workspaces & RBAC</p>
      <h2>Team & Workspaces</h2>
      <p className="lede">
        Create isolated workspaces, invite team members, and assign Owner, Admin, Manager, Member, or Viewer permissions.
      </p>

      {message && <p className="meta">{message}</p>}

      <div className="people-split">
        <div>
          <ul className="org-list">
            {!orgs.length && (
              <li className="empty">No team workspace yet — create one on the right.</li>
            )}
            {orgs.map((o) => (
              <li
                key={o.id}
                className={`org-item ${activeOrgId === o.id ? "is-selected" : ""}`}
                onClick={() => setActiveOrgId(o.id)}
                style={{ cursor: "pointer" }}
              >
                <strong>{o.name}</strong> ({o.slug}) · {o.member_count} members
              </li>
            ))}
          </ul>

          {activeOrgId && (
            <div className="people-table-wrap" style={{ marginTop: "1rem" }}>
              <table className="people-table">
                <thead>
                  <tr>
                    <th>Member</th>
                    <th>Email</th>
                    <th>Role</th>
                  </tr>
                </thead>
                <tbody>
                  {members.map((m) => (
                    <tr key={m.id}>
                      <td className="people-name">{m.full_name}</td>
                      <td>{m.email}</td>
                      <td>
                        <select
                          className="stage-select"
                          value={m.role}
                          onChange={(e) => changeRole(m.user_id, e.target.value)}
                        >
                          <option value="owner">Owner</option>
                          <option value="admin">Admin</option>
                          <option value="manager">Manager</option>
                          <option value="member">Member</option>
                          <option value="viewer">Viewer</option>
                        </select>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>

        <aside className="crm-drawer">
          <form onSubmit={createWorkspace} style={{ marginBottom: "1.2rem" }}>
            <p className="app-kicker">Create Workspace</p>
            <input
              className="note-input"
              placeholder="Workspace name…"
              value={wsName}
              onChange={(e) => setWsName(e.target.value)}
              required
            />
            <button type="submit" className="btn-new-search" style={{ marginTop: "0.45rem" }}>
              Create workspace
            </button>
          </form>

          {activeOrgId && (
            <form onSubmit={handleInvite}>
              <p className="app-kicker">Invite Member</p>
              <label htmlFor="invite-email">Email</label>
              <input
                id="invite-email"
                className="note-input"
                type="email"
                required
                value={email}
                onChange={(e) => setEmail(e.target.value)}
              />
              <label htmlFor="invite-role">Role</label>
              <select id="invite-role" className="stage-select" value={role} onChange={(e) => setRole(e.target.value)}>
                <option value="member">Member</option>
                <option value="manager">Manager</option>
                <option value="admin">Admin</option>
                <option value="viewer">Viewer (Read-only)</option>
              </select>
              <button type="submit" className="btn-new-search" style={{ marginTop: "0.5rem" }}>
                Send invite
              </button>
            </form>
          )}

          {auditLogs.length > 0 && (
            <div style={{ marginTop: "1rem" }}>
              <p className="app-kicker">Audit Log</p>
              <ul className="mini-list">
                {auditLogs.slice(0, 6).map((a) => (
                  <li key={a.id} className="people-sub">{a.action}</li>
                ))}
              </ul>
            </div>
          )}
        </aside>
      </div>
    </section>
  );
}
