function dash(value) {
  return value?.trim?.() ? value : "—";
}

function scorePct(score) {
  return `${Math.round((score || 0) * 100)}%`;
}

export default function LeadsTable({ leads }) {
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
                {lead.email ? (
                  <a href={`mailto:${lead.email}`}>{lead.email}</a>
                ) : (
                  "—"
                )}
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
