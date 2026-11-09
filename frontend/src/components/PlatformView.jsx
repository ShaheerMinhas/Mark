import { useCallback, useEffect, useState } from "react";
import { api } from "../api/client.js";
import { useAuth } from "../context/AuthContext.jsx";

export default function PlatformView() {
  const { user } = useAuth();
  const [jobsData, setJobsData] = useState(null);
  const [integrations, setIntegrations] = useState(null);
  const [automations, setAutomations] = useState([]);
  const [ruleName, setRuleName] = useState("");
  const [trigger, setTrigger] = useState("email_verified");
  const [fullName, setFullName] = useState(user?.full_name || "");
  const [msg, setMsg] = useState("");

  const load = useCallback(async () => {
    const [j, i, a] = await Promise.all([
      api("/api/platform/jobs").catch(() => null),
      api("/api/platform/integrations").catch(() => null),
      api("/api/platform/automations").catch(() => ({ items: [] })),
    ]);
    setJobsData(j);
    setIntegrations(i);
    setAutomations(a.items || []);
  }, []);

  useEffect(() => { load(); }, [load]);

  async function addAutomation(e) {
    e.preventDefault();
    if (!ruleName.trim()) return;
    await api("/api/platform/automations", {
      method: "POST",
      body: {
        name: ruleName.trim(),
        trigger,
        actions: { set_status: "qualified" },
      },
    });
    setRuleName("");
    load();
  }

  async function saveProfile(e) {
    e.preventDefault();
    await api("/api/auth/me", { method: "PATCH", body: { full_name: fullName } });
    setMsg("Profile updated.");
  }

  const conc = jobsData?.concurrency || {};

  return (
    <section className="view">
      <p className="app-kicker">Operations · Jobs, Integrations & Settings</p>
      <h2>Platform Operations & Health</h2>
      <p className="lede">
        Monitor background workers, search engine circuits, email providers, and workspace automations.
      </p>

      <div className="metric-row">
        <div className="metric"><span>Search Concurrency</span><strong>{conc.search_max_concurrency ?? 12}</strong></div>
        <div className="metric"><span>Enrichment Concurrency</span><strong>{conc.enrichment_max_concurrency ?? 16}</strong></div>
        <div className="metric"><span>Worker Threads</span><strong>{conc.worker_concurrency ?? 3}</strong></div>
        <div className="metric"><span>Batch Size</span><strong>{conc.batch_size ?? 250}</strong></div>
      </div>

      <div className="people-split">
        <div>
          <h3>Integrations & Connectors</h3>
          <div className="people-table-wrap" style={{ marginBottom: "1rem" }}>
            <table className="people-table">
              <thead>
                <tr>
                  <th>Provider / Connector</th>
                  <th>Type</th>
                  <th>Status</th>
                </tr>
              </thead>
              <tbody>
                {(integrations?.email_providers || []).map((p) => (
                  <tr key={p.id}>
                    <td className="people-name">{p.name}</td>
                    <td>Email Enrichment</td>
                    <td>
                      <span className={`status-pill ${p.configured ? "status-pill--completed" : ""}`}>
                        {p.configured ? "Configured" : "API key not set"}
                      </span>
                    </td>
                  </tr>
                ))}
                {(integrations?.crm_connectors || []).map((c) => (
                  <tr key={c.id}>
                    <td className="people-name">{c.name}</td>
                    <td>CRM Sync (Auto-sync OFF)</td>
                    <td>
                      <span className={`status-pill ${c.configured ? "status-pill--completed" : ""}`}>
                        {c.status.replaceAll("_", " ")}
                      </span>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          <h3>Recent Background Jobs</h3>
          <div className="people-table-wrap">
            <table className="people-table">
              <thead>
                <tr>
                  <th>Job</th>
                  <th>Status</th>
                  <th>Queries</th>
                  <th>Candidates</th>
                  <th>Leads / Emails</th>
                </tr>
              </thead>
              <tbody>
                {(jobsData?.search_jobs || []).slice(0, 12).map((j) => (
                  <tr key={j.id}>
                    <td className="people-name">#{j.id} {j.name}</td>
                    <td><span className={`status-pill status-pill--${j.status}`}>{j.status}</span></td>
                    <td>{j.completed_queries}/{j.total_queries}</td>
                    <td>{j.candidates_found}</td>
                    <td>{j.qualified_leads} / {j.emails_found}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>

        <aside className="crm-drawer">
          <form onSubmit={addAutomation} style={{ marginBottom: "1.2rem" }}>
            <p className="app-kicker">Automation Rules</p>
            <input
              className="note-input"
              placeholder="Rule name (e.g. Auto-qualify verified)"
              value={ruleName}
              onChange={(e) => setRuleName(e.target.value)}
            />
            <select className="stage-select" value={trigger} onChange={(e) => setTrigger(e.target.value)} style={{ marginTop: "0.4rem" }}>
              <option value="email_verified">When email is verified</option>
              <option value="search_completed">When search finishes</option>
            </select>
            <button type="submit" className="btn-new-search" style={{ marginTop: "0.4rem" }}>
              Add automation
            </button>
          </form>
          <ul className="mini-list">
            {automations.map((a) => (
              <li key={a.id} className="people-sub"><strong>{a.name}</strong> ({a.trigger})</li>
            ))}
          </ul>

          <form onSubmit={saveProfile} style={{ marginTop: "1rem" }}>
            <p className="app-kicker">Profile Settings</p>
            <label>Full name</label>
            <input className="note-input" value={fullName} onChange={(e) => setFullName(e.target.value)} />
            <button type="submit" className="btn-new-search" style={{ marginTop: "0.4rem" }}>Save profile</button>
            {msg && <p className="meta">{msg}</p>}
          </form>
        </aside>
      </div>
    </section>
  );
}
