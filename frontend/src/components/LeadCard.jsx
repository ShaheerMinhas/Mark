export default function LeadCard({ lead }) {
  return (
    <li className="lead">
      <div className="lead-top">
        <div>
          <h4>{lead.name}</h4>
          <p className="role">
            {lead.work_designation} · {lead.company_name}
          </p>
        </div>
        <span className="score">{Math.round((lead.relevance_score || 0) * 100)}%</span>
      </div>
      <p className="ai-desc">{lead.ai_description}</p>
      <div className="lead-fields">
        <span>{lead.email || "—"}</span>
        <span>{lead.phone || "—"}</span>
        <span>{lead.personal_location || "—"}</span>
        <span>{lead.work_field || "—"}</span>
        <span>{lead.company_location || "—"}</span>
        {lead.linkedin_url && (
          <a href={lead.linkedin_url} target="_blank" rel="noopener noreferrer">
            LinkedIn
          </a>
        )}
      </div>
    </li>
  );
}
