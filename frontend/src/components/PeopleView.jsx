import { useCallback, useEffect, useState } from "react";
import { api } from "../api/client.js";

const STAGES = [
  { id: "", label: "All statuses" },
  { id: "new", label: "New" },
  { id: "unreviewed", label: "Unreviewed" },
  { id: "qualified", label: "Qualified" },
  { id: "contacted", label: "Contacted" },
  { id: "interested", label: "Interested" },
  { id: "not_interested", label: "Not interested" },
  { id: "converted", label: "Converted" },
  { id: "disqualified", label: "Disqualified" },
  { id: "archived", label: "Archived" },
];

const EMAIL_FILTERS = [
  { id: "", label: "Any email" },
  { id: "has_email", label: "Has email" },
  { id: "verified", label: "Verified" },
  { id: "found", label: "Found" },
  { id: "inferred", label: "Inferred" },
  { id: "missing", label: "Missing email" },
];

const SORTS = [
  { id: "recent", label: "Newest first" },
  { id: "relevance", label: "Relevance score" },
  { id: "name", label: "Name (A–Z)" },
  { id: "company", label: "Company (A–Z)" },
  { id: "priority", label: "Priority" },
];

export default function PeopleView() {
  const [q, setQ] = useState("");
  const [stage, setStage] = useState("");
  const [emailStatus, setEmailStatus] = useState("");
  const [company, setCompany] = useState("");
  const [location, setLocation] = useState("");
  const [sort, setSort] = useState("recent");
  const [page, setPage] = useState(1);
  const [data, setData] = useState(null);
  const [error, setError] = useState("");
  const [banner, setBanner] = useState("");
  const [loading, setLoading] = useState(true);

  const [selectedIds, setSelectedIds] = useState([]);
  const [selected, setSelected] = useState(null);
  const [detail, setDetail] = useState(null);
  const [notes, setNotes] = useState("");
  const [newNote, setNewNote] = useState("");
  const [newTask, setNewTask] = useState("");

  // Selective CRM transfer modal state
  const [transferOpen, setTransferOpen] = useState(false);
  const [groups, setGroups] = useState([]);
  const [groupId, setGroupId] = useState("");
  const [subgroupId, setSubgroupId] = useState("");
  const [preview, setPreview] = useState(null);
  const [excludeNoEmail, setExcludeNoEmail] = useState(false);
  const [excludeUnverified, setExcludeUnverified] = useState(false);
  const [transferring, setTransferring] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    const params = new URLSearchParams({ page: String(page), page_size: "40", sort });
    if (q.trim()) params.set("q", q.trim());
    if (stage) params.set("stage", stage);
    if (emailStatus) params.set("email_status", emailStatus);
    if (company.trim()) params.set("company", company.trim());
    if (location.trim()) params.set("location", location.trim());
    try {
      setData(await api(`/api/people?${params}`));
    } catch (err) {
      setError(err.message || "Could not load leads");
    } finally {
      setLoading(false);
    }
  }, [page, q, stage, emailStatus, company, location, sort]);

  useEffect(() => {
    const t = setTimeout(load, 180);
    return () => clearTimeout(t);
  }, [load]);

  const loadDetail = useCallback(async (id) => {
    try {
      const d = await api(`/api/people/${id}`);
      setDetail(d);
    } catch {
      setDetail(null);
    }
  }, []);

  function openRecord(person) {
    setSelected(person);
    setNotes(person.crm_notes || "");
    loadDetail(person.id);
  }

  async function move(id, lead_status) {
    const updated = await api(`/api/people/${id}`, {
      method: "PATCH",
      body: { lead_status, crm_stage: lead_status },
    });
    setSelected((current) => (current?.id === id ? updated : current));
    load();
    if (selected?.id === id) loadDetail(id);
  }

  async function saveNotes() {
    if (!selected) return;
    const updated = await api(`/api/people/${selected.id}`, {
      method: "PATCH",
      body: { crm_notes: notes },
    });
    setSelected(updated);
    load();
    loadDetail(selected.id);
  }

  async function addNote(e) {
    e.preventDefault();
    if (!selected || !newNote.trim()) return;
    await api(`/api/people/${selected.id}/notes`, {
      method: "POST",
      body: { body: newNote.trim() },
    });
    setNewNote("");
    loadDetail(selected.id);
  }

  async function addTask(e) {
    e.preventDefault();
    if (!selected || !newTask.trim()) return;
    await api(`/api/people/${selected.id}/tasks`, {
      method: "POST",
      body: { title: newTask.trim() },
    });
    setNewTask("");
    loadDetail(selected.id);
  }

  async function toggleTask(task) {
    if (!selected) return;
    await api(`/api/people/${selected.id}/tasks/${task.id}`, {
      method: "PATCH",
      body: { completed: !task.completed },
    });
    loadDetail(selected.id);
  }

  async function runBulk(action, extra = {}) {
    if (!selectedIds.length) return;
    setBanner("");
    try {
      const res = await api("/api/people/bulk", {
        method: "POST",
        body: { lead_ids: selectedIds, action, ...extra },
      });
      setBanner(`Bulk ${action}: ${res.affected} lead(s) updated.`);
      setSelectedIds([]);
      load();
    } catch (err) {
      setError(err.message);
    }
  }

  async function openCrmTransfer() {
    if (!selectedIds.length) return;
    setTransferOpen(true);
    setPreview(null);
    try {
      const [gRes, pRes] = await Promise.all([
        api("/api/crm/groups"),
        api("/api/crm/transfer/preview", {
          method: "POST",
          body: { lead_ids: selectedIds },
        }),
      ]);
      setGroups(gRes.items || []);
      setPreview(pRes);
    } catch (err) {
      setError(err.message);
    }
  }

  async function confirmTransfer() {
    setTransferring(true);
    setError("");
    try {
      const res = await api("/api/crm/transfer", {
        method: "POST",
        body: {
          lead_ids: selectedIds,
          group_id: groupId ? Number(groupId) : null,
          subgroup_id: subgroupId ? Number(subgroupId) : null,
          exclude_without_email: excludeNoEmail,
          exclude_unverified: excludeUnverified,
        },
      });
      setBanner(
        `Added ${res.transferred_count} contact(s) to CRM (${res.duplicate_count} duplicate(s) skipped).`
      );
      setTransferOpen(false);
      setSelectedIds([]);
    } catch (err) {
      setError(err.message);
    } finally {
      setTransferring(false);
    }
  }

  const items = data?.items || [];
  const meta = data?.page;
  const allOnPageSelected = items.length > 0 && items.every((p) => selectedIds.includes(p.id));
  const topGroups = groups.filter((g) => !g.parent_id);
  const subGroups = groups.filter((g) => String(g.parent_id) === String(groupId));

  function toggleOne(id) {
    setSelectedIds((prev) => (prev.includes(id) ? prev.filter((x) => x !== id) : [...prev, id]));
  }

  function toggleAllOnPage() {
    if (allOnPageSelected) {
      setSelectedIds((prev) => prev.filter((id) => !items.some((p) => p.id === id)));
    } else {
      setSelectedIds((prev) => Array.from(new Set([...prev, ...items.map((p) => p.id)])));
    }
  }

  return (
    <section className="view">
      <p className="app-kicker">My Leads · Discovery Database</p>
      <div className="view-head">
        <div>
          <h2>Discovered Leads</h2>
          <p className="lede">
            All people discovered across your searches live here first. Review, qualify, and selectively add chosen leads to the CRM.
          </p>
        </div>
        {selectedIds.length > 0 && (
          <div className="bulk-bar">
            <strong>{selectedIds.length} selected</strong>
            <button type="button" className="btn-new-search" onClick={openCrmTransfer}>
              Add to CRM…
            </button>
            <button type="button" className="ghost" onClick={() => runBulk("update", { lead_status: "qualified" })}>
              Mark Qualified
            </button>
            <button type="button" className="ghost" onClick={() => runBulk("enrich")}>
              Enrich Emails
            </button>
            <button type="button" className="ghost" onClick={() => runBulk("archive")}>
              Archive
            </button>
            <button type="button" className="ghost" onClick={() => runBulk("delete")}>
              Delete
            </button>
          </div>
        )}
      </div>

      <div className="people-toolbar">
        <input
          value={q}
          placeholder="Search name, email, company, title…"
          onChange={(e) => { setPage(1); setQ(e.target.value); }}
        />
        <input
          value={company}
          placeholder="Filter company…"
          style={{ minWidth: 160 }}
          onChange={(e) => { setPage(1); setCompany(e.target.value); }}
        />
        <input
          value={location}
          placeholder="Filter location…"
          style={{ minWidth: 160 }}
          onChange={(e) => { setPage(1); setLocation(e.target.value); }}
        />
        <select className="stage-select" value={stage} onChange={(e) => { setPage(1); setStage(e.target.value); }}>
          {STAGES.map((s) => <option key={s.id || "all"} value={s.id}>{s.label}</option>)}
        </select>
        <select className="stage-select" value={emailStatus} onChange={(e) => { setPage(1); setEmailStatus(e.target.value); }}>
          {EMAIL_FILTERS.map((s) => <option key={s.id || "any"} value={s.id}>{s.label}</option>)}
        </select>
        <select className="stage-select" value={sort} onChange={(e) => { setPage(1); setSort(e.target.value); }}>
          {SORTS.map((s) => <option key={s.id} value={s.id}>{s.label}</option>)}
        </select>
      </div>

      {banner && <p className="meta" style={{ color: "#047857", fontWeight: 600 }}>{banner}</p>}
      {error && <p className="error-msg">{error}</p>}

      {transferOpen && (
        <div className="modal-card">
          <div className="view-head">
            <div>
              <p className="app-kicker">Selective CRM Transfer</p>
              <h3>Add selected leads to CRM</h3>
            </div>
            <button type="button" className="ghost" onClick={() => setTransferOpen(false)}>Close</button>
          </div>
          {preview ? (
            <>
              <div className="metric-row" style={{ margin: "0.75rem 0" }}>
                <div className="metric"><span>Selected leads</span><strong>{preview.selected_count}</strong></div>
                <div className="metric"><span>With email</span><strong>{preview.with_email}</strong></div>
                <div className="metric"><span>Without email</span><strong>{preview.without_email}</strong></div>
                <div className="metric"><span>Duplicates in CRM</span><strong>{preview.duplicate_count}</strong></div>
              </div>
              {preview.warnings?.length > 0 && (
                <ul className="warn-list">
                  {preview.warnings.map((w, i) => <li key={i}>{w}</li>)}
                </ul>
              )}
              <div className="people-toolbar">
                <select className="stage-select" value={groupId} onChange={(e) => { setGroupId(e.target.value); setSubgroupId(""); }}>
                  <option value="">Destination group (optional)…</option>
                  {topGroups.map((g) => <option key={g.id} value={g.id}>{g.name}</option>)}
                </select>
                {subGroups.length > 0 && (
                  <select className="stage-select" value={subgroupId} onChange={(e) => setSubgroupId(e.target.value)}>
                    <option value="">Subgroup (optional)…</option>
                    {subGroups.map((g) => <option key={g.id} value={g.id}>{g.name}</option>)}
                  </select>
                )}
                <label className="inline-check">
                  <input type="checkbox" checked={excludeNoEmail} onChange={(e) => setExcludeNoEmail(e.target.checked)} />
                  Exclude leads without email
                </label>
                <label className="inline-check">
                  <input type="checkbox" checked={excludeUnverified} onChange={(e) => setExcludeUnverified(e.target.checked)} />
                  Confirmed emails only
                </label>
                <button type="button" className="btn-new-search" disabled={transferring} onClick={confirmTransfer}>
                  {transferring ? "Transferring…" : "Confirm Add to CRM"}
                </button>
              </div>
            </>
          ) : (
            <p className="meta">Calculating transfer summary…</p>
          )}
        </div>
      )}

      <div className="people-split">
        <div className="people-table-wrap">
          <table className="people-table">
            <thead>
              <tr>
                <th style={{ width: 36 }}>
                  <input type="checkbox" checked={allOnPageSelected} onChange={toggleAllOnPage} aria-label="Select page" />
                </th>
                <th>Person</th>
                <th>Company</th>
                <th>Email</th>
                <th>Score</th>
                <th>Search</th>
                <th>Status</th>
              </tr>
            </thead>
            <tbody>
              {loading && <tr><td colSpan={7}>Loading…</td></tr>}
              {!loading && !items.length && <tr><td colSpan={7}>No leads match this filter.</td></tr>}
              {items.map((person) => (
                <tr
                  key={person.id}
                  className={selected?.id === person.id ? "is-selected" : ""}
                  onClick={() => openRecord(person)}
                >
                  <td onClick={(e) => e.stopPropagation()}>
                    <input
                      type="checkbox"
                      checked={selectedIds.includes(person.id)}
                      onChange={() => toggleOne(person.id)}
                    />
                  </td>
                  <td>
                    <div className="people-name">{person.name}</div>
                    <div className="people-sub">{person.work_designation || person.personal_location || "—"}</div>
                  </td>
                  <td>
                    <div>{person.company_name || "—"}</div>
                    <div className="people-sub">{person.company_domain || ""}</div>
                  </td>
                  <td>
                    <div>{person.email || "—"}</div>
                    {person.email_status ? <div className="people-sub">{person.email_status}</div> : null}
                  </td>
                  <td>{Math.round((person.relevance_score || 0) * 100)}%</td>
                  <td className="people-sub">{person.search_name}</td>
                  <td onClick={(e) => e.stopPropagation()}>
                    <select
                      className="stage-select"
                      value={person.lead_status || person.crm_stage || "new"}
                      onChange={(e) => move(person.id, e.target.value)}
                    >
                      {STAGES.filter((s) => s.id).map((s) => (
                        <option key={s.id} value={s.id}>{s.label}</option>
                      ))}
                    </select>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>

        <aside className="crm-drawer">
          {selected ? (
            <>
              <p className="app-kicker">Lead Profile</p>
              <h3>{selected.name}</h3>
              <p className="lede">{selected.work_designation || "—"} · {selected.company_name || "No company"}</p>
              <dl className="drawer-facts">
                <div><dt>Email</dt><dd>{selected.email || "Not found"}</dd></div>
                <div><dt>Phone</dt><dd>{selected.phone || "—"}</dd></div>
                <div><dt>Location</dt><dd>{selected.personal_location || "—"}</dd></div>
                <div><dt>Domain</dt><dd>{selected.company_domain || "—"}</dd></div>
                <div><dt>LinkedIn</dt><dd>{selected.linkedin_url ? <a href={selected.linkedin_url} target="_blank" rel="noreferrer">Profile</a> : "—"}</dd></div>
              </dl>

              {selected.score_breakdown && (
                <div className="score-box">
                  <p className="app-kicker">Evidence Scores</p>
                  <div className="people-sub">
                    Title {Math.round((selected.score_breakdown.job_title_match || 0) * 100)}% ·
                    Company {Math.round((selected.score_breakdown.company_relevance || 0) * 100)}% ·
                    Location {Math.round((selected.score_breakdown.location_match || 0) * 100)}% ·
                    Identity {Math.round((selected.score_breakdown.identity_confidence || 0) * 100)}%
                  </div>
                </div>
              )}

              <label htmlFor="people-stage">Lifecycle Status</label>
              <select
                id="people-stage"
                className="stage-select"
                value={selected.lead_status || selected.crm_stage || "new"}
                onChange={(e) => move(selected.id, e.target.value)}
              >
                {STAGES.filter((s) => s.id).map((s) => (
                  <option key={s.id} value={s.id}>{s.label}</option>
                ))}
              </select>

              <label htmlFor="people-notes">Summary Notes</label>
              <textarea
                id="people-notes"
                className="note-input"
                rows={3}
                value={notes}
                onChange={(e) => setNotes(e.target.value)}
              />
              <button type="button" className="btn-new-search" onClick={saveNotes}>Save notes</button>

              <form onSubmit={addTask} style={{ marginTop: "0.75rem" }}>
                <label>Tasks ({detail?.tasks?.length || 0})</label>
                <div style={{ display: "flex", gap: "0.35rem" }}>
                  <input
                    className="note-input"
                    placeholder="Add follow-up task…"
                    value={newTask}
                    onChange={(e) => setNewTask(e.target.value)}
                  />
                  <button type="submit" className="ghost">Add</button>
                </div>
              </form>
              <ul className="mini-list">
                {(detail?.tasks || []).map((t) => (
                  <li key={t.id}>
                    <label className="inline-check">
                      <input type="checkbox" checked={t.completed} onChange={() => toggleTask(t)} />
                      <span style={{ textDecoration: t.completed ? "line-through" : "none" }}>{t.title}</span>
                    </label>
                  </li>
                ))}
              </ul>

              <form onSubmit={addNote} style={{ marginTop: "0.6rem" }}>
                <label>Activity & Notes Log</label>
                <div style={{ display: "flex", gap: "0.35rem" }}>
                  <input
                    className="note-input"
                    placeholder="Log a call or note…"
                    value={newNote}
                    onChange={(e) => setNewNote(e.target.value)}
                  />
                  <button type="submit" className="ghost">Post</button>
                </div>
              </form>
              <ul className="mini-list">
                {(detail?.notes || []).map((n) => (
                  <li key={`n-${n.id}`}><strong>{n.author_name}:</strong> {n.body}</li>
                ))}
                {(detail?.activities || []).slice(0, 6).map((a) => (
                  <li key={`a-${a.id}`} className="people-sub">{a.event_type}: {a.detail}</li>
                ))}
              </ul>
            </>
          ) : (
            <p className="lede">Select a lead to inspect evidence scores, tasks, notes, and activity history, or check rows to selectively transfer them to the CRM.</p>
          )}
        </aside>
      </div>

      {meta && (
        <div className="pager">
          <span>{meta.total} discovered leads</span>
          <span>
            <button type="button" disabled={page <= 1} onClick={() => setPage((p) => p - 1)}>Previous</button>
            {" "}page {meta.page} of {meta.total_pages}{" "}
            <button type="button" disabled={!meta.has_next} onClick={() => setPage((p) => p + 1)}>Next</button>
          </span>
        </div>
      )}
    </section>
  );
}
