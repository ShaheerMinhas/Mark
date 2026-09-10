import { useCallback, useEffect, useState } from "react";
import { api, downloadExport } from "../api/client.js";

export default function Dashboard({ onOpenSearch }) {
  const [searches, setSearches] = useState([]);
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const data = await api("/api/searches");
      setSearches(data);
    } catch {
      setSearches([]);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { load(); }, [load]);

  return (
    <section className="view">
      <h2>Saved searches</h2>
      <p className="lede">Your lead lists are stored on-site. Export anytime as CSV or XML.</p>
      <ul className="search-list">
        {loading && <li className="empty">Loading…</li>}
        {!loading && !searches.length && (
          <li className="empty">No saved searches yet. Run your first search.</li>
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
                {s.lead_count} leads · {s.mode} · {new Date(s.created_at).toLocaleDateString()}
              </span>
            </div>
            <div className="search-actions">
              <button type="button" className="ghost" onClick={() => onOpenSearch(s.id)}>
                View
              </button>
              <button type="button" className="ghost" onClick={() => downloadExport(s.id, "csv")}>
                CSV
              </button>
              <button type="button" className="ghost" onClick={() => downloadExport(s.id, "xml")}>
                XML
              </button>
            </div>
          </li>
        ))}
      </ul>
    </section>
  );
}
