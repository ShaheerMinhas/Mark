import { useCallback, useEffect, useState } from "react";
import { api } from "../api/client.js";

export default function OrganizationView() {
  const [orgs, setOrgs] = useState([]);
  const [email, setEmail] = useState("");
  const [role, setRole] = useState("member");
  const [message, setMessage] = useState("");

  const load = useCallback(async () => {
    try {
      const data = await api("/api/organizations");
      setOrgs(data);
    } catch {
      setOrgs([]);
    }
  }, []);

  useEffect(() => { load(); }, [load]);

  async function handleInvite(e) {
    e.preventDefault();
    if (!orgs.length) return;
    setMessage("");
    try {
      await api(`/api/organizations/${orgs[0].id}/members`, {
        method: "POST",
        body: { email, role },
      });
      setEmail("");
      setMessage("Member added.");
      load();
    } catch (err) {
      setMessage(err.message);
    }
  }

  return (
    <section className="view">
      <h2>Organization</h2>
      <p className="lede">Manage team access for organizational accounts.</p>
      <ul className="org-list">
        {!orgs.length && (
          <li className="empty">Individual account — no organization.</li>
        )}
        {orgs.map((o) => (
          <li key={o.id} className="org-item">
            <strong>{o.name}</strong> · {o.member_count} members
          </li>
        ))}
      </ul>
      {orgs.length > 0 && (
        <form className="panel" onSubmit={handleInvite}>
          <h3>Invite member</h3>
          <div className="field">
            <label htmlFor="invite-email">Email</label>
            <input
              id="invite-email"
              type="email"
              required
              value={email}
              onChange={(e) => setEmail(e.target.value)}
            />
          </div>
          <div className="field">
            <label htmlFor="invite-role">Role</label>
            <select id="invite-role" value={role} onChange={(e) => setRole(e.target.value)}>
              <option value="member">Member</option>
              <option value="admin">Admin</option>
            </select>
          </div>
          <button type="submit">Send invite</button>
          {message && <p className="meta">{message}</p>}
        </form>
      )}
    </section>
  );
}
