import { useCallback, useEffect, useState } from "react";
import { api, getToken } from "../api/client.js";

const SECTIONS = [
  { id: "overview", label: "Overview" },
  { id: "engine", label: "Discovery engine" },
  { id: "targeting", label: "Targeting" },
  { id: "fields", label: "Field priorities" },
  { id: "providers", label: "Providers" },
  { id: "usage", label: "Usage" },
  { id: "health", label: "Cache health" },
  { id: "leads", label: "Cached leads" },
  { id: "companies", label: "Cached companies" },
  { id: "jobs", label: "Discovery jobs" },
];

const COUNTRY_CODES = [
  "US", "GB", "DE", "FR", "NL", "IE", "SE", "NO", "DK", "FI", "CH", "AT", "BE",
  "ES", "IT", "PT", "PL", "CZ", "EE", "LT", "LV", "RO",
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
      <p className="meta">Live public search stays off. Paid email runs only after an operator enables it and a budget reservation succeeds. Company research has no adapter.</p>
      {error && <p className="error-msg">{error}</p>}
      {section === "overview" && <Overview onError={setError} />}
      {section === "engine" && <Engine onError={setError} />}
      {section === "targeting" && <Targeting onError={setError} />}
      {section === "fields" && <FieldPriorities onError={setError} />}
      {section === "providers" && <Providers onError={setError} />}
      {section === "usage" && <Usage onError={setError} />}
      {section === "health" && <CacheHealth onError={setError} />}
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

function blankCriteria() {
  return {
    geography: { include_countries: [], exclude_countries: [], include_regions: [], include_cities: [], exclude_cities: [] },
    industry: { include: [], exclude: [], include_sub: [], exclude_sub: [] },
    titles: { exact: [], synonyms: [], exclude: [] },
    functions: [],
    seniorities: [],
    departments: [],
    company: { employee_min: null, employee_max: null, sizes: [], types: [] },
    technologies: { mode: "optional", include: [] },
  };
}

function formFromTarget(target) {
  const criteria = target?.criteria || {};
  const company = criteria.company || {};
  return {
    name: target?.name || "",
    priority: target?.priority ?? 100,
    schedule_type: target?.schedule_type || "weekdays",
    timezone: target?.timezone || "UTC",
    interval_minutes: target?.interval_minutes ?? 60,
    hour: target?.hour ?? 7,
    minute: target?.minute ?? 0,
    max_people_per_run: target?.max_people_per_run ?? 25,
    max_queries_per_run: target?.max_queries_per_run ?? 5,
    refresh_hours: target?.refresh_hours ?? 168,
    criteria: {
      ...blankCriteria(),
      ...criteria,
      geography: { ...blankCriteria().geography, ...(criteria.geography || {}) },
      industry: { ...blankCriteria().industry, ...(criteria.industry || {}) },
      titles: { ...blankCriteria().titles, ...(criteria.titles || {}) },
      company: {
        employee_min: company.employee_min ?? "",
        employee_max: company.employee_max ?? "",
        sizes: company.sizes || [],
        types: company.types || [],
      },
      technologies: { mode: "optional", include: [], ...(criteria.technologies || {}) },
    },
  };
}

function listText(values) {
  return (values || []).join(", ");
}

function parseList(text) {
  return String(text || "").split(",").map((part) => part.trim()).filter(Boolean);
}

function toggleValue(values, value) {
  return values.includes(value) ? values.filter((item) => item !== value) : [...values, value];
}

function Targeting({ onError }) {
  const [targets, setTargets] = useState([]);
  const [catalogue, setCatalogue] = useState([]);
  const [selectedId, setSelectedId] = useState(null);
  const [form, setForm] = useState(null);
  const [loaded, setLoaded] = useState(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [notice, setNotice] = useState("");
  const [preview, setPreview] = useState(null);
  const [categoryName, setCategoryName] = useState("");
  const [categoryKind, setCategoryKind] = useState("industry");

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const [targetBody, catalogueBody] = await Promise.all([
        api("/api/platform/discovery/targets"),
        api("/api/platform/discovery/catalogue"),
      ]);
      const items = targetBody.items || [];
      setTargets(items);
      setCatalogue(catalogueBody.items || []);
      setSelectedId((current) => current || items[0]?.id || null);
    } catch (err) {
      onError(err.message);
    } finally {
      setLoading(false);
    }
  }, [onError]);

  useEffect(() => { load(); }, [load]);

  useEffect(() => {
    const target = targets.find((item) => item.id === selectedId);
    if (!target) {
      setForm(null);
      setLoaded(null);
      return;
    }
    const next = formFromTarget(target);
    setForm(next);
    setLoaded(next);
  }, [selectedId, targets]);

  useEffect(() => {
    setNotice("");
    setPreview(null);
  }, [selectedId]);

  function patchCriteria(group, key, value) {
    setForm((current) => ({
      ...current,
      criteria: { ...current.criteria, [group]: { ...current.criteria[group], [key]: value } },
    }));
  }

  function chips(kind) {
    return catalogue.filter((item) => item.kind === kind);
  }

  async function save(event) {
    event.preventDefault();
    if (!form || !selectedId) return;
    setSaving(true);
    setNotice("");
    const criteria = {
      ...form.criteria,
      titles: {
        exact: parseList(form.criteria.titles.exactText ?? listText(form.criteria.titles.exact)),
        synonyms: parseList(form.criteria.titles.synonymText ?? listText(form.criteria.titles.synonyms)),
        exclude: parseList(form.criteria.titles.excludeText ?? listText(form.criteria.titles.exclude)),
      },
      geography: {
        ...form.criteria.geography,
        include_regions: parseList(form.criteria.geography.regionText ?? listText(form.criteria.geography.include_regions)),
        include_cities: parseList(form.criteria.geography.cityText ?? listText(form.criteria.geography.include_cities)),
        exclude_cities: parseList(form.criteria.geography.excludeCityText ?? listText(form.criteria.geography.exclude_cities)),
      },
      company: {
        ...form.criteria.company,
        employee_min: form.criteria.company.employee_min === "" ? null : Number(form.criteria.company.employee_min),
        employee_max: form.criteria.company.employee_max === "" ? null : Number(form.criteria.company.employee_max),
        sizes: parseList(form.criteria.company.sizeText ?? listText(form.criteria.company.sizes)),
        types: parseList(form.criteria.company.typeText ?? listText(form.criteria.company.types)),
      },
    };
    try {
      const saved = await api(`/api/platform/discovery/targets/${selectedId}`, {
        method: "PATCH",
        body: {
          name: form.name.trim(),
          priority: Number(form.priority),
          schedule_type: form.schedule_type,
          timezone: form.timezone.trim(),
          interval_minutes: form.interval_minutes === "" ? null : Number(form.interval_minutes),
          hour: Number(form.hour),
          minute: Number(form.minute),
          max_people_per_run: Number(form.max_people_per_run),
          max_queries_per_run: Number(form.max_queries_per_run),
          refresh_hours: Number(form.refresh_hours),
          criteria,
        },
      });
      setNotice("Saved. The target stays in its current scheduling state.");
      setTargets((current) => current.map((item) => (item.id === saved.id ? saved : item)));
    } catch (err) {
      onError(err.message);
    } finally {
      setSaving(false);
    }
  }

  async function runPreview() {
    if (!selectedId) return;
    try {
      setPreview(await api(`/api/platform/discovery/targets/${selectedId}/preview`, { method: "POST" }));
    } catch (err) {
      onError(err.message);
    }
  }

  async function addCategory() {
    if (!categoryName.trim()) return;
    try {
      await api("/api/platform/discovery/catalogue", {
        method: "POST",
        body: { kind: categoryKind, name: categoryName.trim() },
      });
      setCategoryName("");
      const catalogueBody = await api("/api/platform/discovery/catalogue");
      setCatalogue(catalogueBody.items || []);
    } catch (err) {
      onError(err.message);
    }
  }

  if (loading) return <div className="empty">Loading targets…</div>;
  if (!targets.length) return <div className="empty">No targets yet. Add a paused target from Discovery engine.</div>;
  if (!form) return <div className="empty">Select a target.</div>;

  const titles = form.criteria.titles;
  const geo = form.criteria.geography;
  const company = form.criteria.company;

  return (
    <form className="panel admin-note" onSubmit={save}>
      <p>Saved rules are what the cache query uses. A missing optional value does not drop a person. People who fail a required field stay in the cache and are not eligible. Technology names are labels, not connections. This page does not start discovery.</p>
      <label>Target
        <select value={selectedId || ""} onChange={(e) => setSelectedId(Number(e.target.value))}>
          {targets.map((target) => (
            <option key={target.id} value={target.id}>{target.name} · {target.scheduling_status}</option>
          ))}
        </select>
      </label>
      <div className="people-toolbar">
        <label className="target-name">Name
          <input value={form.name} aria-label="Target name" onChange={(e) => setForm({ ...form, name: e.target.value })} />
        </label>
        <label>Priority
          <input type="number" value={form.priority} onChange={(e) => setForm({ ...form, priority: e.target.value })} />
        </label>
        <label>Schedule
          <select value={form.schedule_type} onChange={(e) => setForm({ ...form, schedule_type: e.target.value })}>
            <option value="hourly">Hourly</option>
            <option value="interval">Interval</option>
            <option value="daily">Daily</option>
            <option value="weekdays">Weekdays</option>
          </select>
        </label>
        <label>Timezone
          <input value={form.timezone} aria-label="Timezone" onChange={(e) => setForm({ ...form, timezone: e.target.value })} />
        </label>
        <label>Interval minutes
          <input type="number" min="15" value={form.interval_minutes ?? ""} onChange={(e) => setForm({ ...form, interval_minutes: e.target.value })} />
        </label>
        <label>Hour
          <input type="number" min="0" max="23" value={form.hour} onChange={(e) => setForm({ ...form, hour: e.target.value })} />
        </label>
        <label>Minute
          <input type="number" min="0" max="59" value={form.minute} onChange={(e) => setForm({ ...form, minute: e.target.value })} />
        </label>
        <label>People per run
          <input type="number" min="1" max="500" value={form.max_people_per_run} onChange={(e) => setForm({ ...form, max_people_per_run: e.target.value })} />
        </label>
        <label>Queries per run
          <input type="number" min="1" max="50" value={form.max_queries_per_run} onChange={(e) => setForm({ ...form, max_queries_per_run: e.target.value })} />
        </label>
        <label>Refresh hours
          <input type="number" min="1" value={form.refresh_hours} onChange={(e) => setForm({ ...form, refresh_hours: e.target.value })} />
        </label>
      </div>
      <h3>Countries to include</h3>
      <div className="people-chips">
        {COUNTRY_CODES.map((code) => (
          <button
            key={`in-${code}`}
            type="button"
            className={`people-chip ${geo.include_countries.includes(code) ? "is-on" : ""}`}
            onClick={() => patchCriteria("geography", "include_countries", toggleValue(geo.include_countries, code))}
          >{code}</button>
        ))}
      </div>
      <h3>Countries to exclude</h3>
      <div className="people-chips">
        {COUNTRY_CODES.map((code) => (
          <button
            key={`ex-${code}`}
            type="button"
            className={`people-chip ${geo.exclude_countries.includes(code) ? "is-on" : ""}`}
            onClick={() => patchCriteria("geography", "exclude_countries", toggleValue(geo.exclude_countries, code))}
          >{code}</button>
        ))}
      </div>
      <div className="people-toolbar">
        <label>States
          <input
            aria-label="States"
            value={geo.regionText ?? listText(geo.include_regions)}
            onChange={(e) => patchCriteria("geography", "regionText", e.target.value)}
          />
        </label>
        <label>Cities to include
          <input
            aria-label="Cities to include"
            value={geo.cityText ?? listText(geo.include_cities)}
            onChange={(e) => patchCriteria("geography", "cityText", e.target.value)}
          />
        </label>
        <label>Cities to exclude
          <input
            aria-label="Cities to exclude"
            value={geo.excludeCityText ?? listText(geo.exclude_cities)}
            onChange={(e) => patchCriteria("geography", "excludeCityText", e.target.value)}
          />
        </label>
      </div>
      <h3>Industries</h3>
      <div className="people-chips">
        {chips("industry").map((item) => (
          <button
            key={item.id}
            type="button"
            className={`people-chip ${form.criteria.industry.include.includes(item.normalized) ? "is-on" : ""}`}
            onClick={() => patchCriteria("industry", "include", toggleValue(form.criteria.industry.include, item.normalized))}
          >{item.name}</button>
        ))}
      </div>
      <h3>Industries to exclude</h3>
      <div className="people-chips">
        {chips("industry").map((item) => (
          <button
            key={`out-${item.id}`}
            type="button"
            className={`people-chip ${form.criteria.industry.exclude.includes(item.normalized) ? "is-on" : ""}`}
            onClick={() => patchCriteria("industry", "exclude", toggleValue(form.criteria.industry.exclude, item.normalized))}
          >{item.name}</button>
        ))}
      </div>
      <h3>Subindustries</h3>
      <div className="people-chips">
        {chips("subindustry").map((item) => (
          <button
            key={item.id}
            type="button"
            className={`people-chip ${form.criteria.industry.include_sub.includes(item.normalized) ? "is-on" : ""}`}
            onClick={() => patchCriteria("industry", "include_sub", toggleValue(form.criteria.industry.include_sub, item.normalized))}
          >{item.name}</button>
        ))}
      </div>
      <h3>Titles</h3>
      <div className="people-toolbar">
        <label>Exact titles
          <input aria-label="Exact titles" value={titles.exactText ?? listText(titles.exact)} onChange={(e) => patchCriteria("titles", "exactText", e.target.value)} />
        </label>
        <label>Synonyms
          <input aria-label="Title synonyms" value={titles.synonymText ?? listText(titles.synonyms)} onChange={(e) => patchCriteria("titles", "synonymText", e.target.value)} />
        </label>
        <label>Excluded titles
          <input aria-label="Excluded titles" value={titles.excludeText ?? listText(titles.exclude)} onChange={(e) => patchCriteria("titles", "excludeText", e.target.value)} />
        </label>
      </div>
      <h3>Function, seniority, department</h3>
      {["function", "seniority", "department"].map((kind) => (
        <div key={kind}>
          <p className="meta">{kind}</p>
          <div className="people-chips">
            {chips(kind).map((item) => {
              const key = kind === "function" ? "functions" : kind === "seniority" ? "seniorities" : "departments";
              const selected = form.criteria[key] || [];
              return (
                <button
                  key={item.id}
                  type="button"
                  className={`people-chip ${selected.includes(item.normalized) ? "is-on" : ""}`}
                  onClick={() => setForm((current) => ({
                    ...current,
                    criteria: { ...current.criteria, [key]: toggleValue(selected, item.normalized) },
                  }))}
                >{item.name}</button>
              );
            })}
          </div>
        </div>
      ))}
      <h3>Company</h3>
      <div className="people-toolbar">
        <label>Employees min
          <input type="number" min="0" aria-label="Employees min" value={company.employee_min} onChange={(e) => patchCriteria("company", "employee_min", e.target.value)} />
        </label>
        <label>Employees max
          <input type="number" min="0" aria-label="Employees max" value={company.employee_max} onChange={(e) => patchCriteria("company", "employee_max", e.target.value)} />
        </label>
        <label>Size labels
          <input aria-label="Size labels" value={company.sizeText ?? listText(company.sizes)} onChange={(e) => patchCriteria("company", "sizeText", e.target.value)} />
        </label>
        <label>Company types
          <input aria-label="Company types" value={company.typeText ?? listText(company.types)} onChange={(e) => patchCriteria("company", "typeText", e.target.value)} />
        </label>
      </div>
      <h3>Technology labels</h3>
      <label>Match
        <select
          value={form.criteria.technologies.mode}
          onChange={(e) => patchCriteria("technologies", "mode", e.target.value)}
        >
          <option value="optional">Optional — a person with no technology label still matches</option>
          <option value="required">Required — a listed label must be present</option>
        </select>
      </label>
      <div className="people-chips">
        {chips("technology").map((item) => (
          <button
            key={item.id}
            type="button"
            className={`people-chip ${form.criteria.technologies.include.includes(item.normalized) ? "is-on" : ""}`}
            onClick={() => patchCriteria("technologies", "include", toggleValue(form.criteria.technologies.include, item.normalized))}
          >{item.name}</button>
        ))}
      </div>
      <div className="people-toolbar">
        <label>Catalogue
          <select value={categoryKind} onChange={(e) => setCategoryKind(e.target.value)}>
            <option value="industry">Industry</option>
            <option value="subindustry">Subindustry</option>
            <option value="technology">Technology label</option>
            <option value="function">Function</option>
            <option value="seniority">Seniority</option>
            <option value="department">Department</option>
          </select>
        </label>
        <label>New label
          <input aria-label="New catalogue label" value={categoryName} onChange={(e) => setCategoryName(e.target.value)} />
        </label>
        <button type="button" className="ghost" onClick={addCategory}>Add label</button>
      </div>
      <div className="people-toolbar">
        <button type="submit" className="btn-new-search" disabled={saving}>{saving ? "Saving…" : "Save target"}</button>
        <button
          type="button"
          className="ghost"
          onClick={() => { setForm(loaded); setNotice(""); setPreview(null); }}
        >Reset</button>
        <button type="button" className="ghost" onClick={runPreview}>Preview matches</button>
      </div>
      {notice && <p className="meta">{notice}</p>}
      {preview && (
        <p className="meta">
          {preview.eligible} eligible. {preview.preferred_complete} of those meet preferred fields. {preview.detail} Enrichment: {preview.enrichment}.
        </p>
      )}
    </form>
  );
}

function FieldPriorities({ onError }) {
  const [items, setItems] = useState([]);
  const [detail, setDetail] = useState("");
  const [loading, setLoading] = useState(true);
  const [notice, setNotice] = useState("");

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const body = await api("/api/platform/discovery/fields");
      setItems(body.items || []);
      setDetail(body.detail || "");
    } catch (err) {
      onError(err.message);
    } finally {
      setLoading(false);
    }
  }, [onError]);

  useEffect(() => { load(); }, [load]);

  function update(name, key, value) {
    setItems((current) => current.map((item) => (item.field_name === name ? { ...item, [key]: value } : item)));
  }

  async function save(item) {
    setNotice("");
    try {
      await api(`/api/platform/discovery/fields/${item.field_name}`, {
        method: "PATCH",
        body: {
          collection_enabled: item.collection_enabled,
          requirement: item.requirement,
          priority: Number(item.priority),
          refresh_hours: Number(item.refresh_hours),
          verification_required: item.verification_required,
          enrichment_source: item.field_name === "email" ? (item.enrichment_source || null) : null,
        },
      });
      setNotice(`Saved ${item.field_name}. Enrichment is still unavailable.`);
    } catch (err) {
      onError(err.message);
    }
  }

  if (loading) return <div className="empty">Loading field rules…</div>;
  if (!items.length) return <div className="empty">No field rules have been saved.</div>;
  return (
    <div className="panel admin-note">
      <p>{detail}</p>
      <div className="people-table-wrap">
        <table className="people-table">
          <thead>
            <tr>
              <th>Field</th>
              <th>Collection</th>
              <th>Requirement</th>
              <th>Priority</th>
              <th>Refresh hours</th>
              <th>Verification</th>
              <th>Enrichment source</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {items.map((item) => (
              <tr key={item.field_name}>
                <td>{item.field_name}</td>
                <td>
                  <input
                    type="checkbox"
                    aria-label={`${item.field_name} collection`}
                    checked={item.collection_enabled}
                    onChange={(e) => update(item.field_name, "collection_enabled", e.target.checked)}
                  />
                </td>
                <td>
                  <select aria-label={`${item.field_name} requirement`} value={item.requirement} onChange={(e) => update(item.field_name, "requirement", e.target.value)}>
                    <option value="required">Required</option>
                    <option value="preferred">Preferred</option>
                    <option value="optional">Optional</option>
                  </select>
                </td>
                <td><input type="number" aria-label={`${item.field_name} priority`} value={item.priority} onChange={(e) => update(item.field_name, "priority", e.target.value)} /></td>
                <td><input type="number" aria-label={`${item.field_name} refresh`} value={item.refresh_hours} onChange={(e) => update(item.field_name, "refresh_hours", e.target.value)} /></td>
                <td>
                  <input
                    type="checkbox"
                    aria-label={`${item.field_name} verification`}
                    checked={item.verification_required}
                    onChange={(e) => update(item.field_name, "verification_required", e.target.checked)}
                  />
                </td>
                <td>
                  {item.field_name === "email" ? (
                    <input
                      aria-label="email enrichment source"
                      placeholder="Optional provider"
                      value={item.enrichment_source || ""}
                      onChange={(e) => update(item.field_name, "enrichment_source", e.target.value)}
                    />
                  ) : "Unavailable"}
                </td>
                <td><button type="button" className="ghost" onClick={() => save(item)}>Save</button></td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div className="people-toolbar">
        <button type="button" className="ghost" onClick={() => { setNotice(""); load(); }}>Reset</button>
      </div>
      {notice && <p className="meta">{notice}</p>}
      <p className="meta">Turning collection off stops that field from filtering. Evidence, identity keys, and suppression stay. A required field changes eligibility. It does not delete the person. Email is the only field with an adapter, and it still does not run until the paid gates pass.</p>
    </div>
  );
}

function money(minor, status) {
  if (minor === null || minor === undefined) return status === "unknown" ? "Unknown" : "None recorded";
  return `${minor} minor units`;
}

function Providers({ onError }) {
  const [settings, setSettings] = useState(null);
  const [items, setItems] = useState([]);
  const [budgets, setBudgets] = useState([]);
  const [loading, setLoading] = useState(true);
  const [notice, setNotice] = useState("");
  const [secret, setSecret] = useState("");
  const [budget, setBudget] = useState({ scope: "global", provider_name: "", period: "day", currency: "USD", limit_minor: "" });

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const [settingsBody, providerBody, budgetBody] = await Promise.all([
        api("/api/platform/discovery/spend-settings"),
        api("/api/platform/discovery/providers"),
        api("/api/platform/discovery/budgets"),
      ]);
      setSettings(settingsBody);
      setItems(providerBody.items || []);
      setBudgets(budgetBody.items || []);
    } catch (err) {
      onError(err.message);
    } finally {
      setLoading(false);
    }
  }, [onError]);

  useEffect(() => { load(); }, [load]);

  function patchItem(name, key, value) {
    setItems((current) => current.map((item) => (item.name === name ? { ...item, [key]: value } : item)));
  }

  async function saveSettings(event) {
    event.preventDefault();
    setNotice("");
    try {
      setSettings(await api("/api/platform/discovery/spend-settings", { method: "PATCH", body: settings }));
      setNotice("Switches saved. Live search stays off.");
    } catch (err) {
      onError(err.message);
    }
  }

  async function saveProvider(item) {
    setNotice("");
    try {
      await api(`/api/platform/discovery/providers/${item.name}`, {
        method: "PATCH",
        body: { enabled: item.enabled, priority: Number(item.priority), rate_per_hour: Number(item.rate_per_hour) },
      });
      const price = item.draft_price === "" || item.draft_price === undefined ? null : Number(item.draft_price);
      if (price !== null) {
        await api(`/api/platform/discovery/providers/${item.name}/price`, {
          method: "PUT",
          body: { operation: "email_find", currency: "USD", max_cost_minor: price },
        });
      }
      if (secret.trim() && secret.startsWith(`${item.name}:`)) {
        await api(`/api/platform/discovery/providers/${item.name}/credential`, {
          method: "PUT",
          body: { api_key: secret.slice(item.name.length + 1) },
        });
        setSecret("");
      }
      setNotice(`Saved ${item.name}. The credential value is not shown again.`);
      load();
    } catch (err) {
      onError(err.message);
    }
  }

  async function saveBudget(event) {
    event.preventDefault();
    setNotice("");
    try {
      await api("/api/platform/discovery/budgets", {
        method: "PUT",
        body: {
          scope: budget.scope,
          provider_name: budget.scope === "global" ? "" : budget.provider_name,
          period: budget.period,
          currency: budget.currency,
          limit_minor: Number(budget.limit_minor),
        },
      });
      setNotice("Budget saved.");
      load();
    } catch (err) {
      onError(err.message);
    }
  }

  if (loading) return <div className="empty">Loading providers…</div>;
  if (!items.length) return <div className="empty">No providers are registered.</div>;
  return (
    <div className="panel admin-note">
      <p>These are global-job adapters. Customer workspace keys are not used and are not shown here. A price of zero is rejected. Company research is unavailable.</p>
      <form onSubmit={saveSettings}>
        <label className="inline-check">
          <input type="checkbox" checked={Boolean(settings?.paid_providers_enabled)} onChange={(e) => setSettings({ ...settings, paid_providers_enabled: e.target.checked })} />
          Allow paid global calls
        </label>
        <label className="inline-check">
          <input type="checkbox" checked={Boolean(settings?.pause_enrichment)} onChange={(e) => setSettings({ ...settings, pause_enrichment: e.target.checked })} />
          Pause company enrichment
        </label>
        <label className="inline-check">
          <input type="checkbox" checked={Boolean(settings?.pause_email)} onChange={(e) => setSettings({ ...settings, pause_email: e.target.checked })} />
          Pause email finding
        </label>
        <label className="inline-check">
          <input type="checkbox" checked={Boolean(settings?.pause_verify)} onChange={(e) => setSettings({ ...settings, pause_verify: e.target.checked })} />
          Pause email verification
        </label>
        <div className="people-toolbar">
          <button type="submit" className="btn-new-search">Save switches</button>
        </div>
      </form>
      <p className="meta">{settings?.detail} Live public search: {settings?.live_public_search}.</p>
      <div className="people-table-wrap">
        <table className="people-table">
          <thead>
            <tr><th>Provider</th><th>Enabled</th><th>Priority</th><th>Rate / hour</th><th>Credential</th><th>Find price max</th><th>Company</th><th></th></tr>
          </thead>
          <tbody>
            {items.map((item) => (
              <tr key={item.name}>
                <td>{item.name}</td>
                <td><input type="checkbox" aria-label={`${item.name} enabled`} checked={item.enabled} onChange={(e) => patchItem(item.name, "enabled", e.target.checked)} /></td>
                <td><input type="number" aria-label={`${item.name} priority`} value={item.priority} onChange={(e) => patchItem(item.name, "priority", e.target.value)} /></td>
                <td><input type="number" min="0" aria-label={`${item.name} rate`} value={item.rate_per_hour} onChange={(e) => patchItem(item.name, "rate_per_hour", e.target.value)} /></td>
                <td>{item.credential_configured ? "Configured" : "Missing"}</td>
                <td><input type="number" min="1" aria-label={`${item.name} price`} placeholder={item.prices?.find((price) => price.operation === "email_find")?.max_cost_minor ?? "minor units"} value={item.draft_price ?? ""} onChange={(e) => patchItem(item.name, "draft_price", e.target.value)} /></td>
                <td>Unavailable</td>
                <td><button type="button" className="ghost" onClick={() => saveProvider(item)}>Save</button></td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <form className="people-toolbar" onSubmit={(event) => { event.preventDefault(); }}>
        <label>Credential
          <input type="password" aria-label="Platform credential" placeholder="provider:key" value={secret} onChange={(e) => setSecret(e.target.value)} autoComplete="off" />
        </label>
        <p className="meta">Type the provider name, a colon, and the key, then save that row. The key is not displayed after saving.</p>
      </form>
      <form className="people-toolbar" onSubmit={saveBudget}>
        <label>Scope
          <select value={budget.scope} onChange={(e) => setBudget({ ...budget, scope: e.target.value })}>
            <option value="global">Global</option>
            <option value="provider">Provider</option>
          </select>
        </label>
        <label>Provider
          <input aria-label="Budget provider" value={budget.provider_name} onChange={(e) => setBudget({ ...budget, provider_name: e.target.value })} disabled={budget.scope === "global"} />
        </label>
        <label>Period
          <select value={budget.period} onChange={(e) => setBudget({ ...budget, period: e.target.value })}>
            <option value="day">Daily</option>
            <option value="month">Monthly</option>
          </select>
        </label>
        <label>Currency
          <input aria-label="Budget currency" value={budget.currency} onChange={(e) => setBudget({ ...budget, currency: e.target.value })} />
        </label>
        <label>Limit minor units
          <input type="number" min="1" aria-label="Budget limit" value={budget.limit_minor} onChange={(e) => setBudget({ ...budget, limit_minor: e.target.value })} />
        </label>
        <button type="submit" className="ghost">Save budget</button>
        <button type="button" className="ghost" onClick={() => { setNotice(""); setSecret(""); load(); }}>Reset</button>
      </form>
      {!budgets.length && <div className="empty">No spending limit has been saved. Paid calls stay denied until global and provider daily and monthly limits exist.</div>}
      {!!budgets.length && (
        <ul className="mini-list">
          {budgets.map((item) => (
            <li key={`${item.scope}-${item.provider_name}-${item.period}`}>
              {item.scope} {item.provider_name || "all"} {item.period}: limit {item.limit_minor} {item.currency}. Reserved {item.reserved_minor}. Settled {item.settled_minor}. Unknown {item.unknown_minor}.
            </li>
          ))}
        </ul>
      )}
      {notice && <p className="meta">{notice}</p>}
    </div>
  );
}

function Usage({ onError }) {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    api("/api/platform/discovery/usage")
      .then((body) => { if (!cancelled) setData(body); })
      .catch((err) => { if (!cancelled) onError(err.message); })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [onError]);

  if (loading) return <div className="empty">Loading usage…</div>;
  if (!data) return <div className="empty">Usage is not available.</div>;
  const denominators = data.denominators || {};
  return (
    <div className="panel admin-note">
      <p>{data.currency_note}</p>
      <div className="metric-row">
        <div className="metric"><span>Requests</span><strong>{data.requests}</strong></div>
        <div className="metric"><span>Successes</span><strong>{data.successes}</strong></div>
        <div className="metric"><span>Failures</span><strong>{data.failures}</strong></div>
        <div className="metric"><span>429</span><strong>{data.rate_limited}</strong></div>
      </div>
      <p className="meta">Estimated {money(data.estimated_cost_minor, data.estimated_cost_status)} ({data.estimated_cost_status}). Actual {money(data.actual_cost_minor, data.actual_cost_status)} ({data.actual_cost_status}). Unknown {money(data.unknown_cost_minor, data.unknown_cost_status)} ({data.unknown_cost_status}).</p>
      <p className="meta">Credits {data.credits === null ? "unknown" : data.credits}. Cost per verified person: {data.cost_per_verified_minor === null ? data.cost_per_verified_status : data.cost_per_verified_minor}.</p>
      <p className="meta">Discovered {denominators.discovered_people ?? 0}. Enriched {denominators.enriched_people ?? 0}. Verified {denominators.verified_people ?? 0}.</p>
      {!Object.keys(data.providers || {}).length && <div className="empty">No global provider calls have been recorded.</div>}
      {!!Object.keys(data.providers || {}).length && (
        <ul className="mini-list">
          {Object.entries(data.providers).map(([name, count]) => <li key={name}>{name}: {count} calls</li>)}
        </ul>
      )}
    </div>
  );
}

function CacheHealth({ onError }) {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    api("/api/platform/discovery/cache-health")
      .then((body) => { if (!cancelled) setData(body); })
      .catch((err) => { if (!cancelled) onError(err.message); })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [onError]);

  if (loading) return <div className="empty">Loading cache health…</div>;
  if (!data) return <div className="empty">Cache health is not available.</div>;
  return (
    <div className="panel admin-note">
      <p>{data.detail}</p>
      <div className="metric-row">
        <div className="metric"><span>People</span><strong>{data.people}</strong></div>
        <div className="metric"><span>Companies</span><strong>{data.companies}</strong></div>
        <div className="metric"><span>Stale fields</span><strong>{data.stale_fields}</strong></div>
        <div className="metric"><span>Fresh fields</span><strong>{data.fresh_fields}</strong></div>
      </div>
      <p className="meta">Discovered today {data.people_today}. This week {data.people_week}. Ineligible evidence rows {data.ineligible_evidence}.</p>
      <h3>Email status</h3>
      {!Object.keys(data.email_status || {}).length && <div className="empty">No cached people.</div>}
      <ul className="mini-list">
        {Object.entries(data.email_status || {}).map(([name, count]) => <li key={name}>{name || "null"}: {count}</li>)}
      </ul>
      <h3>Quality tier</h3>
      <ul className="mini-list">
        {Object.entries(data.quality_tier || {}).map(([name, count]) => <li key={name}>{name}: {count}</li>)}
      </ul>
    </div>
  );
}
