import { useCallback, useEffect, useState } from "react";
import { api, getToken } from "../api/client.js";

const STAGES = [
  { id: "new", label: "New" },
  { id: "contacted", label: "Contacted" },
  { id: "qualified", label: "Qualified" },
  { id: "interested", label: "Interested" },
  { id: "converted", label: "Converted" },
  { id: "disqualified", label: "Disqualified" },
];

export default function CrmView() {
  const [contacts, setContacts] = useState([]);
  const [groups, setGroups] = useState([]);
  const [exportsLog, setExportsLog] = useState([]);
  const [activeGroupId, setActiveGroupId] = useState(null);
  const [q, setQ] = useState("");
  const [counts, setCounts] = useState({});
  const [error, setError] = useState("");
  const [selected, setSelected] = useState(null);
  const [notes, setNotes] = useState("");
  const [saving, setSaving] = useState(false);

  // Group / Subgroup builder
  const [groupName, setGroupName] = useState("");
  const [parentId, setParentId] = useState("");
  const [groupKind, setGroupKind] = useState("static");
  const [ruleLocation, setRuleLocation] = useState("");
  const [ruleTitle, setRuleTitle] = useState("");

  const load = useCallback(async () => {
    setError("");
    try {
      const params = new URLSearchParams({ page: "1", page_size: "200" });
      if (q.trim()) params.set("q", q.trim());
      if (activeGroupId != null) params.set("group_id", String(activeGroupId));
      const [cRes, gRes, eRes] = await Promise.all([
        api(`/api/crm/contacts?${params}`),
        api("/api/crm/groups"),
        api("/api/crm/exports"),
      ]);
      setContacts(cRes.items || []);
      setCounts(cRes.stage_counts || {});
      setGroups(gRes.items || []);
      setExportsLog(eRes.items || []);
    } catch (err) {
      setError(err.message || "Could not load CRM");
    }
  }, [q, activeGroupId]);

  useEffect(() => { load(); }, [load]);

  async function createGroup(e) {
    e.preventDefault();
    if (!groupName.trim()) return;
    const smart_rules = {};
    if (groupKind === "smart") {
      if (ruleLocation.trim()) smart_rules.location = ruleLocation.trim();
      if (ruleTitle.trim()) smart_rules.job_title = ruleTitle.trim();
    }
    try {
      await api("/api/crm/groups", {
        method: "POST",
        body: {
          name: groupName.trim(),
          parent_id: parentId ? Number(parentId) : null,
          kind: groupKind,
          smart_rules,
        },
      });
      setGroupName("");
      setRuleLocation("");
      setRuleTitle("");
      load();
    } catch (err) {
      setError(err.message);
    }
  }

  async function deleteGroup(id) {
    await api(`/api/crm/groups/${id}`, { method: "DELETE" });
    if (activeGroupId === id) setActiveGroupId(null);
    load();
  }

  async function moveStage(id, stage) {
    setContacts((rows) => rows.map((row) => (row.id === id ? { ...row, stage, crm_stage: stage } : row)));
    setSelected((cur) => (cur?.id === id ? { ...cur, stage } : cur));
    try {
      await api(`/api/crm/contacts/${id}`, { method: "PATCH", body: { stage } });
      load();
    } catch (err) {
      setError(err.message);
      load();
    }
  }

  async function assignToGroup(contactId, targetGid) {
    if (!targetGid) return;
    await api(`/api/crm/groups/${targetGid}/contacts`, {
      method: "POST",
      body: { contact_ids: [contactId] },
    });
    load();
  }

  function open(person) {
    setSelected(person);
    setNotes(person.notes || "");
  }

  async function saveNotes() {
    if (!selected) return;
    setSaving(true);
    try {
      const updated = await api(`/api/crm/contacts/${selected.id}`, {
        method: "PATCH",
        body: { notes },
      });
      setContacts((rows) => rows.map((row) => (row.id === updated.id ? { ...row, ...updated } : row)));
      setSelected(updated);
    } catch (err) {
      setError(err.message);
    } finally {
      setSaving(false);
    }
  }

  async function exportCrmCsv() {
    const token = getToken();
    const qs = activeGroupId ? `?format=csv&group_id=${activeGroupId}` : "?format=csv";
    const res = await fetch(`/api/crm/export${qs}`, {
      headers: token ? { Authorization: `Bearer ${token}` } : {},
    });
    if (!res.ok) return;
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = "mark-crm-contacts.csv";
    a.click();
    URL.revokeObjectURL(url);
    load();
  }

  const topGroups = groups.filter((g) => !g.parent_id);
  const childrenOf = (pid) => groups.filter((g) => g.parent_id === pid);

  return (
    <section className="view crm-view">
      <div className="view-head">
        <div>
          <h2>CRM</h2>
          <p className="lede">
            Contacts you add from Leads. They are not added automatically.
          </p>
        </div>
        <div className="search-actions">
          <button type="button" className="ghost" onClick={exportCrmCsv}>
            Export CSV
          </button>
        </div>
      </div>

      {error && <p className="error-msg">{error}</p>}

      <div className="crm-workspace">
        <aside className="crm-groups-pane">
          <p className="app-kicker">Groups</p>
          <button
            type="button"
            className={`group-item ${activeGroupId === null ? "active" : ""}`}
            onClick={() => setActiveGroupId(null)}
          >
            <span>All CRM Contacts</span>
            <strong>{contacts.length}</strong>
          </button>

          <ul className="group-tree">
            {topGroups.map((g) => (
              <li key={g.id}>
                <div className={`group-item ${activeGroupId === g.id ? "active" : ""}`}>
                  <button type="button" className="group-link" onClick={() => setActiveGroupId(g.id)}>
                    {g.name} {g.kind === "smart" ? "⚡" : ""}
                  </button>
                  <span>{g.contact_count}</span>
                  <button type="button" className="group-del" onClick={() => deleteGroup(g.id)} title="Delete">×</button>
                </div>
                {childrenOf(g.id).length > 0 && (
                  <ul className="subgroup-tree">
                    {childrenOf(g.id).map((sub) => (
                      <li key={sub.id}>
                        <div className={`group-item group-item--sub ${activeGroupId === sub.id ? "active" : ""}`}>
                          <button type="button" className="group-link" onClick={() => setActiveGroupId(sub.id)}>
                            ↳ {sub.name} {sub.kind === "smart" ? "⚡" : ""}
                          </button>
                          <span>{sub.contact_count}</span>
                          <button type="button" className="group-del" onClick={() => deleteGroup(sub.id)}>×</button>
                        </div>
                      </li>
                    ))}
                  </ul>
                )}
              </li>
            ))}
          </ul>

          <form className="group-form" onSubmit={createGroup}>
            <p className="app-kicker">New Group / Subgroup</p>
            <input
              className="note-input"
              placeholder="e.g. Solar Companies or California"
              value={groupName}
              onChange={(e) => setGroupName(e.target.value)}
              required
            />
            <select className="stage-select" value={parentId} onChange={(e) => setParentId(e.target.value)}>
              <option value="">Top-level group</option>
              {groups.map((g) => <option key={g.id} value={g.id}>Subgroup of: {g.name}</option>)}
            </select>
            <select className="stage-select" value={groupKind} onChange={(e) => setGroupKind(e.target.value)}>
              <option value="static">Static group</option>
              <option value="smart">Smart / dynamic group</option>
            </select>
            {groupKind === "smart" && (
              <>
                <input
                  className="note-input"
                  placeholder="Rule: Location contains (e.g. California)"
                  value={ruleLocation}
                  onChange={(e) => setRuleLocation(e.target.value)}
                />
                <input
                  className="note-input"
                  placeholder="Rule: Title contains (e.g. Director)"
                  value={ruleTitle}
                  onChange={(e) => setRuleTitle(e.target.value)}
                />
              </>
            )}
            <button type="submit" className="btn-new-search">Create group</button>
          </form>
        </aside>

        <div className="crm-main-area">
          <div className="crm-toolbar">
            <input
              value={q}
              placeholder="Search CRM contacts…"
              onChange={(e) => setQ(e.target.value)}
            />
          </div>

          <div className="crm-layout">
            <div className="board">
              {STAGES.map((stage) => {
                const cards = contacts.filter((c) => (c.stage || "new") === stage.id);
                return (
                  <div
                    className="board-col"
                    key={stage.id}
                    onDragOver={(e) => e.preventDefault()}
                    onDrop={(e) => {
                      const id = Number(e.dataTransfer.getData("text/plain"));
                      if (id) moveStage(id, stage.id);
                    }}
                  >
                    <h3>
                      {stage.label}
                      <span>{counts[stage.id] ?? cards.length}</span>
                    </h3>
                    {cards.map((person) => (
                      <article
                        className={`board-card${selected?.id === person.id ? " is-selected" : ""}`}
                        key={person.id}
                        draggable
                        onDragStart={(e) => e.dataTransfer.setData("text/plain", String(person.id))}
                        onClick={() => open(person)}
                      >
                        <strong>{person.name}</strong>
                        <p>{person.title || "No title"}</p>
                        <p>{person.company_name || "No company"}</p>
                        {person.email ? <p className="board-email">{person.email}</p> : null}
                      </article>
                    ))}
                  </div>
                );
              })}
            </div>

            <aside className="crm-drawer">
              {selected ? (
                <>
                  <p className="app-kicker">CRM Contact</p>
                  <h3>{selected.name}</h3>
                  <p className="lede">{selected.title || "—"} · {selected.company_name || "No company"}</p>
                  <dl className="drawer-facts">
                    <div><dt>Email</dt><dd>{selected.email || "Not found"}</dd></div>
                    <div><dt>Phone</dt><dd>{selected.phone || "—"}</dd></div>
                    <div><dt>Location</dt><dd>{selected.location || "—"}</dd></div>
                  </dl>
                  <label htmlFor="crm-stage">Stage</label>
                  <select
                    id="crm-stage"
                    className="stage-select"
                    value={selected.stage || "new"}
                    onChange={(e) => moveStage(selected.id, e.target.value)}
                  >
                    {STAGES.map((s) => <option key={s.id} value={s.id}>{s.label}</option>)}
                  </select>
                  <label>Assign to Group / Subgroup</label>
                  <select
                    className="stage-select"
                    defaultValue=""
                    onChange={(e) => { assignToGroup(selected.id, Number(e.target.value)); e.target.value = ""; }}
                  >
                    <option value="">Choose group…</option>
                    {groups.filter((g) => g.kind === "static").map((g) => (
                      <option key={g.id} value={g.id}>{g.parent_id ? `↳ ${g.name}` : g.name}</option>
                    ))}
                  </select>
                  <label htmlFor="crm-notes">CRM Notes</label>
                  <textarea
                    id="crm-notes"
                    className="note-input"
                    rows={5}
                    value={notes}
                    onChange={(e) => setNotes(e.target.value)}
                  />
                  <button type="button" className="btn-new-search" onClick={saveNotes} disabled={saving}>
                    {saving ? "Saving…" : "Save notes"}
                  </button>
                </>
              ) : (
                <>
                  <p className="app-kicker">Transfer & Export History</p>
                  <p className="lede">Select a contact card to edit notes or assign groups.</p>
                  <ul className="mini-list">
                    {exportsLog.slice(0, 6).map((ex) => (
                      <li key={ex.id} className="people-sub">
                        {ex.kind}: {ex.transferred_count}/{ex.selected_count} records ({ex.with_email_count} w/ email)
                      </li>
                    ))}
                  </ul>
                </>
              )}
            </aside>
          </div>
        </div>
      </div>
    </section>
  );
}
