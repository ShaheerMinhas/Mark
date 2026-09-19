import { useCallback, useEffect, useState } from "react";
import { api, downloadExport } from "../api/client.js";

export default function Dashboard({ onOpenSearch, onNewSearch }) {
  const [searches, setSearches] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const data = await api("/api/searches");
      setSearches(data);
    } catch (err) {
      setSearches([]);
      setError(err.message || "Could not load saved searches");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { load(); }, [load]);

  return (
    <section className="view">
      <div className="view-head">
        <div>
          <h2>Saved searches</h2>
          <p className="lede">Your lead lists are stored on-site. Export anytime as CSV or XML.</p>
        </div>
        <button type="button" className="btn-new-search" onClick={onNewSearch}>
          New search
        </button>
      </div>
      {error && <p className="error-msg">{error}</p>}
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
                {s.lead_count} leads · {s.status} · {s.mode} · {new Date(s.created_at).toLocaleDateString()}
              </span>
            </div>
            <div className="search-actions">
              <button type="button" className="ghost" onClick={() => onOpenSearch(s.id)}>
                View
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
            </div>
          </li>
        ))}
      </ul>
    </section>
  );
}
