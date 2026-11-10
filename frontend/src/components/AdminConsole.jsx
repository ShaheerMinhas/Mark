import { useCallback, useEffect, useState } from "react";
import { api, getToken } from "../api/client.js";

const SECTIONS = [
  { id: "overview", label: "Overview" },
  { id: "engine", label: "Discovery engine" },
  { id: "leads", label: "Cached leads" },
  { id: "companies", label: "Cached companies" },
  { id: "jobs", label: "Discovery jobs" },
];

function statusText(value) {
  if (value === "lease_active") return "Planner lease active";
  if (value === "lease_expired") return "Planner lease expired";
  return "Worker unknown";
}

export default function AdminConsole() {
  const [section, setSection] = useState("overview");
  const [error, setError] = useState("");

  return (
    <section className="view">
      <div className="view-head">
        <div>
          <h2>Operator</h2>
          <p className="lede">
            Shared cache and schedules. Customer leads, notes, and CRM are not shown here. Live search and paid providers are off.
          </p>
        </div>
      </div>
      <div className="people-chips" role="tablist" aria-label="Operator sections">
        {SECTIONS.map((item) => (
          <button
            key={item.id}
            type="button"
            className={`people-chip ${section === item.id ? "is-on" : ""}`}
            aria-selected={section === item.id}
            onClick={() => { setError(""); setSection(item.id); }}
          >
            {item.label}
          </button>
        ))}
      </div>
      <p className="meta">Targeting builder and budgets are not available in this version.</p>
      {error && <p className="error-msg">{error}</p>}
      {section === "overview" && <Overview onError={setError} />}
      {section === "engine" && <Engine onError={setError} />}
      {section === "leads" && <Leads onError={setError} />}
      {section === "companies" && <Companies onError={setError} />}
      {section === "jobs" && <Jobs onError={setError} />}
    </section>
  );
}

function Overview({ onError }) {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    api("/api/platform/discovery/overview")
      .then((body) => { if (!cancelled) setData(body); })
      .catch((err) => { if (!cancelled) onError(err.message); })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [onError]);

  if (loading) return <div className="empty">Loading overview…</div>;
  if (!data) return <div className="empty">Overview is not available.</div>;
  const counts = data.counts || {};
  return (
    <>
      <div className="metric-row">
        <div className="metric"><span>Cached people</span><strong>{counts.people ?? 0}</strong></div>
        <div className="metric"><span>Cached companies</span><strong>{counts.companies ?? 0}</strong></div>
        <div className="metric"><span>Queued runs</span><strong>{counts.runs_queued ?? 0}</strong></div>
        <div className="metric"><span>Cancelled runs</span><strong>{counts.runs_cancelled ?? 0}</strong></div>
      </div>
      <div className="panel admin-note">
        <p><strong>Scheduling:</strong> {data.engine?.scheduling_enabled ? "On" : "Off"}</p>
        <p><strong>Live search:</strong> Disabled. {data.engine?.detail}</p>
        <p><strong>Paid providers:</strong> Disabled. Estimated cost is {data.usage?.estimated_cost_status || "unavailable"}.</p>
        <p><strong>Worker:</strong> {statusText(data.worker?.status)}. {data.worker?.detail}</p>
        <p className="meta">
          Recorded provider calls in the last 24 hours: {data.usage?.provider_calls_24h ?? 0}. That count is from usage rows, not a cost.
        </p>
        <p className="meta">
          Targets — active {data.scheduler?.active_targets ?? 0}, paused {data.scheduler?.paused_targets ?? 0}, stopped {data.scheduler?.stopped_targets ?? 0}.
          Pausing a target does not cancel a queued run.
        </p>
      </div>
    </>
  );
}

function Engine({ onError }) {
  const [settings, setSettings] = useState(null);
  const [targets, setTargets] = useState([]);
  const [loading, setLoading] = useState(true);
  const [name, setName] = useState("");

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const [settingsBody, targetBody] = await Promise.all([
        api("/api/platform/discovery/settings"),
        api("/api/platform/discovery/targets"),
      ]);
      setSettings(settingsBody);
      setTargets(targetBody.items || []);
    } catch (err) {
      onError(err.message);
    } finally {
      setLoading(false);
    }
  }, [onError]);

  useEffect(() => { load(); }, [load]);

  async function saveLimits(event) {
    event.preventDefault();
    try {
      setSettings(await api("/api/platform/discovery/settings", {
        method: "PATCH",
        body: {
          enabled: settings.enabled,
          max_concurrency: Number(settings.max_concurrency),
          max_runs_per_tick: Number(settings.max_runs_per_tick),
          max_runs_per_hour: Number(settings.max_runs_per_hour),
          max_runs_per_day: Number(settings.max_runs_per_day),
        },
      }));
    } catch (err) {
      onError(err.message);
    }
  }

  async function addTarget(event) {
    event.preventDefault();
    if (!name.trim()) return;
    try {
      await api("/api/platform/discovery/targets", {
        method: "POST",
        body: { name: name.trim(), schedule_type: "daily", timezone: "UTC", countries: ["US"] },
      });
      setName("");
      load();
    } catch (err) {
      onError(err.message);
    }
  }

  async function act(id, action) {
    try {
      await api(`/api/platform/discovery/targets/${id}/${action}`, { method: "POST" });
      load();
    } catch (err) {
      onError(err.message);
    }
  }

  if (loading) return <div className="empty">Loading engine…</div>;
  return (
    <>
      <form className="panel admin-note" onSubmit={saveLimits}>
        <p>{settings?.detail}</p>
        <label className="inline-check">
          <input
            type="checkbox"
            checked={Boolean(settings?.enabled)}
            onChange={(e) => setSettings({ ...settings, enabled: e.target.checked })}
          />
          Allow the worker to plan schedules
        </label>
        <div className="people-toolbar">
          <label>Concurrency
            <input type="number" min="1" max="8" value={settings?.max_concurrency ?? 1} onChange={(e) => setSettings({ ...settings, max_concurrency: e.target.value })} />
          </label>
          <label>Per tick
            <input type="number" min="1" max="20" value={settings?.max_runs_per_tick ?? 5} onChange={(e) => setSettings({ ...settings, max_runs_per_tick: e.target.value })} />
          </label>
          <label>Per hour
            <input type="number" min="1" max="48" value={settings?.max_runs_per_hour ?? 4} onChange={(e) => setSettings({ ...settings, max_runs_per_hour: e.target.value })} />
          </label>
          <label>Per day
            <input type="number" min="1" max="200" value={settings?.max_runs_per_day ?? 24} onChange={(e) => setSettings({ ...settings, max_runs_per_day: e.target.value })} />
          </label>
          <button type="submit" className="btn-new-search">Save limits</button>
        </div>
      </form>
      <form className="people-toolbar" onSubmit={addTarget}>
        <input value={name} placeholder="New target name" aria-label="New target name" onChange={(e) => setName(e.target.value)} />
        <button type="submit" className="ghost">Add paused target</button>
      </form>
      {!targets.length && <div className="empty">No targets yet. A new target starts paused.</div>}
      {!!targets.length && (
        <div className="people-table-wrap">
          <table className="people-table">
            <thead>
              <tr><th>Target</th><th>Schedule</th><th>Status</th><th></th></tr>
            </thead>
            <tbody>
              {targets.map((target) => (
                <tr key={target.id}>
                  <td>{target.name}</td>
                  <td>{target.schedule_type} · {target.timezone}</td>
                  <td>{target.scheduling_status}</td>
                  <td className="search-actions">
                    <button type="button" className="ghost" onClick={() => act(target.id, "start")}>Start</button>
                    <button type="button" className="ghost" onClick={() => act(target.id, "pause")}>Pause</button>
                    <button type="button" className="ghost" onClick={() => act(target.id, "resume")}>Resume</button>
                    <button type="button" className="ghost" onClick={() => act(target.id, "stop")}>Stop scheduling</button>
                    <button type="button" className="ghost" onClick={() => act(target.id, "run-now")}>Run now</button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <p className="meta">Start and pause change whether new runs are planned. They do not delete a run that is already queued. Run now queues one job. This build will not call a provider.</p>
    </>
  );
}

function Leads({ onError }) {
  const [q, setQ] = useState("");
  const [page, setPage] = useState(1);
  const [data, setData] = useState(null);
  const [detail, setDetail] = useState(null);
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const params = new URLSearchParams({ page: String(page), page_size: "25", sort: "name" });
      if (q.trim()) params.set("q", q.trim());
      setData(await api(`/api/platform/discovery/people?${params}`));
    } catch (err) {
      onError(err.message);
    } finally {
      setLoading(false);
    }
  }, [onError, page, q]);

  useEffect(() => { load(); }, [load]);

  async function open(id) {
    try {
      setDetail(await api(`/api/platform/discovery/people/${id}`));
    } catch (err) {
      onError(err.message);
    }
  }

  async function archive(id) {
    try {
      await api(`/api/platform/discovery/people/${id}/archive`, { method: "POST" });
      setDetail(null);
      load();
    } catch (err) {
      onError(err.message);
    }
  }

  return (
    <div className="people-split">
      <div>
        <div className="people-toolbar">
          <input value={q} placeholder="Name, company, or email" aria-label="Filter cached leads" onChange={(e) => { setPage(1); setQ(e.target.value); }} />
          <button type="button" className="ghost" onClick={async () => {
            const token = getToken();
            const response = await fetch("/api/platform/discovery/people/export", { headers: token ? { Authorization: `Bearer ${token}` } : {} });
            if (!response.ok) { onError("Export failed"); return; }
            const blob = await response.blob();
            const url = URL.createObjectURL(blob);
            const link = document.createElement("a");
            link.href = url;
            link.download = "cached-people.csv";
            link.click();
            URL.revokeObjectURL(url);
          }}>Export CSV</button>
        </div>
        {loading && <div className="empty">Loading cached leads…</div>}
        {!loading && !(data?.items || []).length && <div className="empty">No cached leads match.</div>}
        {!loading && !!(data?.items || []).length && (
          <div className="people-table-wrap">
            <table className="people-table">
              <thead><tr><th>Person</th><th>Company</th><th>Email</th><th>Country</th><th>Tier</th></tr></thead>
              <tbody>
                {(data.items || []).map((row) => (
                  <tr key={row.id} onClick={() => open(row.id)}>
                    <td>{row.full_name}</td>
                    <td>{row.company_name || "—"}</td>
                    <td>{row.email || "—"}<div className="people-sub">{row.email_status || ""}</div></td>
                    <td>{row.country_code || "—"}</td>
                    <td>{row.quality_tier}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        <p className="people-count">{data ? `${data.total} cached people` : ""}</p>
      </div>
      <aside className="crm-drawer">
        {!detail && <p className="lede">Select a cached person to see provenance and identity keys.</p>}
        {detail && (
          <>
            <h3>{detail.person.full_name}</h3>
            <p className="meta">{detail.person.title || "No title"} · {detail.person.company_name || "No company"}</p>
            <p className="meta">Research notes: {detail.research}</p>
            <h4>Evidence</h4>
            <ul className="mini-list">
              {(detail.evidence || []).map((item, index) => (
                <li key={index}>{item.field_name}: {item.value_text || "—"} {item.eligible ? "" : "(not served)"}</li>
              ))}
              {!(detail.evidence || []).length && <li>No evidence stored.</li>}
            </ul>
            <h4>Identity keys</h4>
            <ul className="mini-list">
              {(detail.identity_keys || []).map((item) => <li key={item.kind}>{item.kind}: {item.key_value}</li>)}
              {!(detail.identity_keys || []).length && <li>No identity key stored.</li>}
            </ul>
            <button type="button" className="ghost" onClick={() => archive(detail.person.id)}>Archive</button>
            <p className="meta">Archive hides the person from search. It does not delete evidence.</p>
          </>
        )}
      </aside>
    </div>
  );
}

function Companies({ onError }) {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  useEffect(() => {
    api("/api/platform/discovery/companies?page=1&page_size=25")
      .then(setData)
      .catch((err) => onError(err.message))
      .finally(() => setLoading(false));
  }, [onError]);
  if (loading) return <div className="empty">Loading companies…</div>;
  if (!(data?.items || []).length) return <div className="empty">No cached companies.</div>;
  return (
    <div className="people-table-wrap">
      <table className="people-table">
        <thead><tr><th>Company</th><th>Domain</th><th>Country</th><th>Industry</th></tr></thead>
        <tbody>
          {data.items.map((row) => (
            <tr key={row.id}>
              <td>{row.name || "—"}</td>
              <td>{row.domain || "—"}</td>
              <td>{row.country_code || "—"}</td>
              <td>{row.industry || "—"}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <p className="people-count">{data.total} companies</p>
    </div>
  );
}

function Jobs({ onError }) {
  const [data, setData] = useState(null);
  const [detail, setDetail] = useState(null);
  const [loading, setLoading] = useState(true);
  const load = useCallback(async () => {
    setLoading(true);
    try {
      setData(await api("/api/platform/discovery/runs?page=1&page_size=25&sort=newest"));
    } catch (err) {
      onError(err.message);
    } finally {
      setLoading(false);
    }
  }, [onError]);
  useEffect(() => { load(); }, [load]);

  async function open(id) {
    try {
      setDetail(await api(`/api/platform/discovery/runs/${id}`));
    } catch (err) {
      onError(err.message);
    }
  }

  async function cancel(id) {
    try {
      await api(`/api/platform/discovery/runs/${id}/cancel`, { method: "POST" });
      load();
      open(id);
    } catch (err) {
      onError(err.message);
    }
  }

  if (loading) return <div className="empty">Loading jobs…</div>;
  return (
    <div className="people-split">
      <div>
        {!data?.items?.length && <div className="empty">No discovery jobs.</div>}
        {!!data?.items?.length && (
          <div className="people-table-wrap">
            <table className="people-table">
              <thead><tr><th>Target</th><th>Run status</th><th>Stage</th><th></th></tr></thead>
              <tbody>
                {data.items.map((row) => (
                  <tr key={row.id} onClick={() => open(row.id)}>
                    <td>{row.target_name || "—"}</td>
                    <td>{row.status}{row.cancel_requested ? " · cancel requested" : ""}</td>
                    <td>{row.current_stage || "—"}</td>
                    <td><button type="button" className="ghost" onClick={(e) => { e.stopPropagation(); cancel(row.id); }}>Cancel run</button></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        <p className="meta">Cancelling a run is not the same as pausing the schedule. The run row stays.</p>
      </div>
      <aside className="crm-drawer">
        {!detail && <p className="lede">Select a job to read each stage.</p>}
        {detail && (
          <>
            <h3>Run {detail.run.id}</h3>
            <p className="meta">Status {detail.run.status}. Queue job {detail.queue_job ? detail.queue_job.status : "none"}.</p>
            <ul className="mini-list">
              {(detail.stages || []).map((stage) => (
                <li key={`${stage.stage}-${stage.attempt}`}>{stage.stage}: {stage.status}. {stage.detail}</li>
              ))}
              {!(detail.stages || []).length && <li>No stage has been recorded yet.</li>}
            </ul>
          </>
        )}
      </aside>
    </div>
  );
}
