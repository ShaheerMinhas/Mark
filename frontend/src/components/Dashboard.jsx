import { useCallback, useEffect, useState } from "react";
import { api, downloadExport } from "../api/client.js";

const SEARCH_STATUS_LABELS = {
  pending: "Queued",
  running: "Running",
  discovering: "Searching",
  filtering: "Scoring",
  enriching: "Finding emails",
  completed: "Finished",
  partially_completed: "Partial",
  failed: "Failed",
  cancelled: "Stopped",
};

function searchStatusLabel(status) {
  return SEARCH_STATUS_LABELS[status] || String(status || "").replaceAll("_", " ");
}

function pillStatus(search) {
  const label = search.activity_label || "";
  if (label.startsWith("Email lookup") || label === "Finding emails") return "enriching";
  return search.detailed_status || search.status;
}

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
          <h2>Overview</h2>
          <p className="lede">Searches, people, and emails in this workspace.</p>
        </div>
        <button type="button" className="btn-new-search" onClick={onNewSearch}>
          New search
        </button>
      </div>
      {error && <p className="error-msg">{error}</p>}

      {loading ? (
        <div className="empty">Loading…</div>
      ) : (
      <>
      <div className="metric-row">
        <div className="metric"><span>Leads</span><strong>{totalDiscovered}</strong></div>
        <div className="metric"><span>Emails</span><strong>{overview?.confirmed_emails ?? 0}</strong></div>
        <div className="metric"><span>Searches</span><strong>{searches.length}</strong></div>
        <div className="metric"><span>Running</span><strong>{overview?.active_searches ?? 0}</strong></div>
      </div>

      <h3 className="section-title">Searches</h3>
      <ul className="search-list">
        {!searches.length && (
          <li className="empty">
            <p>No searches yet.</p>
            <button type="button" className="btn-new-search btn-new-search--inline" onClick={onNewSearch}>
              New search
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
                <span className={`status-pill status-pill--${pillStatus(s)}`}>{s.activity_label || searchStatusLabel(s.detailed_status || s.status)}</span>
                {s.lead_count} leads · {s.emails_found || 0} emails · {new Date(s.created_at).toLocaleDateString()}
              </span>
            </div>
            <div className="search-actions">
              <button type="button" className="btn-new-search" onClick={() => onOpenSearch(s.id)}>
                Open
              </button>
              <button type="button" className="btn-quiet" onClick={() => rerunSearch(s.id)}>
                Rerun
              </button>
              <button
                type="button"
                className="btn-quiet"
                disabled={s.status !== "completed"}
                onClick={() => downloadExport(s.id, "csv")}
              >
                CSV
              </button>
              <button
                type="button"
                className="btn-quiet"
                disabled={s.status !== "completed"}
                onClick={() => downloadExport(s.id, "xml")}
              >
                XML
              </button>
              <button type="button" className="btn-quiet is-danger" onClick={() => deleteSearch(s.id)}>
                Delete
              </button>
            </div>
          </li>
        ))}
      </ul>
      </>
      )}
    </section>
  );
}
