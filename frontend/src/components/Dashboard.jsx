import { useCallback, useEffect, useState } from "react";
import { api, downloadExport } from "../api/client.js";

export default function Dashboard({ onOpenSearch, onNewSearch }) {
  const [searches, setSearches] = useState([]);
  const [overview, setOverview] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const [searchList, stats] = await Promise.all([
        api("/api/searches"),
        api("/api/platform/dashboard").catch(() => null),
      ]);
      setSearches(searchList);
      setOverview(stats);
    } catch (err) {
      setSearches([]);
      setError(err.message || "Could not load saved searches");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { load(); }, [load]);

  async function rerunSearch(id) {
    try {
      const res = await api(`/api/searches/${id}/duplicate`, { method: "POST" });
      if (res.search?.id) onOpenSearch(res.search.id);
    } catch (err) {
      setError(err.message);
    }
  }

  async function deleteSearch(id) {
    try {
      await api(`/api/searches/${id}`, { method: "DELETE" });
      load();
    } catch (err) {
      setError(err.message);
    }
  }

  const totalDiscovered = overview?.total_leads_discovered ?? searches.reduce((n, s) => n + (s.lead_count || 0), 0);

  return (
    <section className="view">
      <div className="view-head">
        <div>
          <p className="app-kicker">Dashboard</p>
          <h2>Lead Intelligence Overview</h2>
          <p className="lede">
            Live metrics from your discovery database, email enrichment waterfall, and selective CRM groups.
          </p>
        </div>
        <button type="button" className="btn-new-search" onClick={onNewSearch}>
          New search
        </button>
      </div>
      {error && <p className="error-msg">{error}</p>}

      <div className="metric-row">
        <div className="metric"><span>Discovered leads</span><strong>{totalDiscovered}</strong></div>
        <div className="metric"><span>Unique people</span><strong>{overview?.unique_people ?? totalDiscovered}</strong></div>
        <div className="metric"><span>Confirmed emails</span><strong>{overview?.confirmed_emails ?? 0}</strong></div>
        <div className="metric"><span>CRM contacts</span><strong>{overview?.crm_contacts ?? 0}</strong></div>
      </div>

      <div className="metric-row" style={{ marginTop: "-0.4rem" }}>
        <div className="metric"><span>Saved searches</span><strong>{searches.length}</strong></div>
        <div className="metric"><span>Active jobs</span><strong>{overview?.active_searches ?? 0}</strong></div>
        <div className="metric"><span>Pending enrichment</span><strong>{overview?.pending_email_enrichment ?? 0}</strong></div>
        <div className="metric"><span>CRM groups</span><strong>{overview?.crm_groups ?? 0}</strong></div>
      </div>

      <h3 style={{ margin: "1.2rem 0 0.6rem" }}>Search History & Saved Searches</h3>
      <ul className="search-list">
        {loading && <li className="empty">Loading…</li>}
        {!loading && !searches.length && (
          <li className="empty">
            <p>No saved searches yet.</p>
            <button type="button" className="btn-new-search btn-new-search--inline" onClick={onNewSearch}>
              Run your first search
            </button>
          </li>
        )}
        {searches.map((s) => (
          <li key={s.id} className="search-item">
            <div>
              <strong>{s.name}</strong>
              <p>
                {s.prompt.slice(0, 100)}
                {s.prompt.length > 100 ? "…" : ""}
              </p>
              <span className="meta">
                <span className={`status-pill status-pill--${s.status}`}>{s.status.replaceAll("_", " ")}</span>
                {s.lead_count} leads · {s.emails_found || 0} emails · {new Date(s.created_at).toLocaleDateString()}
              </span>
            </div>
            <div className="search-actions">
              <button type="button" className="ghost" onClick={() => onOpenSearch(s.id)}>
                Open
              </button>
              <button type="button" className="ghost" onClick={() => rerunSearch(s.id)}>
                Rerun
              </button>
              <button
                type="button"
                className="ghost"
                disabled={s.status !== "completed"}
                onClick={() => downloadExport(s.id, "csv")}
              >
                CSV
              </button>
              <button
                type="button"
                className="ghost"
                disabled={s.status !== "completed"}
                onClick={() => downloadExport(s.id, "xml")}
              >
                XML
              </button>
              <button type="button" className="ghost" onClick={() => deleteSearch(s.id)}>
                Delete
              </button>
            </div>
          </li>
        ))}
      </ul>
    </section>
  );
}
