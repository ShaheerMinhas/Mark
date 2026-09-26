import { useState } from "react";

function dash(value) {
  return value?.trim?.() ? value : "—";
}

function scorePct(score) {
  return `${Math.round((score || 0) * 100)}%`;
}

function statusMeta(lead) {
  const status = lead.email_status || (lead.email ? "found" : "");
  if (!status || status === "not_found") {
    return { label: lead.email ? "Unlabeled" : "Not found", tone: "muted" };
  }
  const map = {
    verified: { label: "Verified", tone: "good" },
    found: { label: "Found", tone: "good" },
    unverified: { label: "Unverified", tone: "warn" },
    inferred: { label: "Inferred", tone: "warn" },
    verify_uncertain: { label: "Uncertain", tone: "warn" },
    invalid: { label: "Invalid", tone: "bad" },
    not_found: { label: "Not found", tone: "muted" },
  };
  return map[status] || { label: status, tone: "muted" };
}

function EmailCell({ lead, onFindOne, looking, lookupBusy }) {
  const [open, setOpen] = useState(false);
  const meta = statusMeta(lead);
  if (!lead.email && (lead.email_status === "not_found" || !lead.email_status)) {
    return (
      <div className="email-cell">
        <span className="email-missing">{looking ? "Looking up…" : "Email not found"}</span>
        {onFindOne && lead.id ? (
          <button
            type="button"
            className="ghost email-mini"
            disabled={lookupBusy}
            onClick={() => onFindOne(lead.id)}
          >
            {looking ? "Looking up…" : "Lookup"}
          </button>
        ) : null}
      </div>
    );
  }
  if (!lead.email) {
    return <span className="email-missing">Email not found</span>;
  }
  const sources = Array.isArray(lead.email_evidence) ? lead.email_evidence : [];
  return (
    <div className={`email-cell email-cell--${meta.tone}`}>
      <a href={`mailto:${lead.email}`}>{lead.email}</a>
      <div className="email-flags">
        <span className={`email-badge email-badge--${meta.tone}`}>{meta.label}</span>
        {lead.email_confidence ? (
          <span className="email-conf">{lead.email_confidence} confidence</span>
        ) : null}
      </div>
      {lead.email_status === "inferred" ? (
        <p className="email-warn">Pattern {lead.email_pattern || "unknown"} · not independently found</p>
      ) : null}
      {lead.email_source ? <p className="email-source">Source: {lead.email_source}</p> : null}
      <button type="button" className="ghost email-mini" onClick={() => setOpen((v) => !v)}>
        {open ? "Hide evidence" : "Evidence"}
      </button>
      {open ? (
        <ul className="email-evidence">
          {sources.length ? (
            sources.map((item, i) => (
              <li key={i}>
                {item.type}
                {item.provider ? ` · ${item.provider}` : ""}
                {item.url ? (
                  <>
                    {" · "}
                    <a href={item.url} target="_blank" rel="noopener noreferrer">
                      source
                    </a>
                  </>
                ) : null}
                {item.detail ? ` — ${item.detail}` : ""}
              </li>
            ))
          ) : (
            <li>No extra evidence stored.</li>
          )}
        </ul>
      ) : null}
    </div>
  );
}

export default function LeadsTable({ leads, onFindOne, lookingId }) {
  if (!leads?.length) {
    return <p className="empty">No leads yet.</p>;
  }

  return (
    <div className="leads-table-wrap">
      <table className="leads-table">
        <thead>
          <tr>
            <th>Name</th>
            <th>Title</th>
            <th>Company</th>
            <th>Email</th>
            <th>Phone</th>
            <th>Location</th>
            <th>Field</th>
            <th>Company loc.</th>
            <th>Score</th>
            <th>LinkedIn</th>
            <th>Summary</th>
          </tr>
        </thead>
        <tbody>
          {leads.map((lead, i) => (
            <tr key={lead.id ?? i}>
              <td className="leads-table-name">{dash(lead.name)}</td>
              <td>{dash(lead.work_designation)}</td>
              <td>{dash(lead.company_name)}</td>
              <td>
                <EmailCell
                  lead={lead}
                  onFindOne={onFindOne}
                  looking={lookingId === lead.id}
                  lookupBusy={Boolean(lookingId)}
                />
              </td>
              <td>{dash(lead.phone)}</td>
              <td>{dash(lead.personal_location)}</td>
              <td>{dash(lead.work_field)}</td>
              <td>{dash(lead.company_location)}</td>
              <td className="leads-table-score">{scorePct(lead.relevance_score)}</td>
              <td>
                {lead.linkedin_url ? (
                  <a href={lead.linkedin_url} target="_blank" rel="noopener noreferrer">
                    Profile
                  </a>
                ) : (
                  "—"
                )}
              </td>
              <td className="leads-table-summary" title={lead.ai_description || undefined}>
                {dash(lead.ai_description)}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
