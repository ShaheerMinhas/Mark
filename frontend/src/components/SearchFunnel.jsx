const REJECTION_LABELS = {
  missing_required_terms: "Missing a required term",
  title_mismatch: "Different job title",
  no_usable_company: "No identifiable employer",
  below_minimum_score: "Scored below the relevance cut-off",
};

/**
 * Keeps the four counts distinguishable, so a small lead count is explained by
 * real data rather than looking like a bug.
 */
export default function SearchFunnel({ funnel }) {
  if (!funnel) return null;

  const steps = [
    { label: "Candidates found", value: funnel.candidates_found, hint: "Everyone discovered" },
    { label: "Relevant leads", value: funnel.relevant_leads, hint: "Matched your criteria" },
    { label: "With LinkedIn", value: funnel.linkedin_leads, hint: "Kept even without an email" },
    { label: "With email", value: funnel.contactable_leads, hint: "Has a usable email" },
    { label: "With phone", value: funnel.phoneable_leads, hint: "Has a phone number" },
  ];

  const rejections = Object.entries(funnel.rejections || {})
    .filter(([, count]) => count > 0)
    .sort((a, b) => b[1] - a[1]);

  const failedQueries = funnel.query_outcomes?.failed || 0;

  return (
    <section className="search-funnel" aria-label="Search funnel">
      <ol className="search-funnel-steps">
        {steps.map((step) => (
          <li key={step.label}>
            <span className="search-funnel-value">{step.value ?? 0}</span>
            <span className="search-funnel-label">{step.label}</span>
            <small className="search-funnel-hint">{step.hint}</small>
          </li>
        ))}
      </ol>

      {rejections.length > 0 ? (
        <details className="search-funnel-rejections">
          <summary>Why candidates were filtered out</summary>
          <ul>
            {rejections.map(([reason, count]) => (
              <li key={reason}>
                <span>{REJECTION_LABELS[reason] || reason.replace(/_/g, " ")}</span>
                <strong>{count}</strong>
              </li>
            ))}
          </ul>
        </details>
      ) : null}

      {failedQueries > 0 ? (
        <p className="search-funnel-note">
          {failedQueries} search {failedQueries === 1 ? "query" : "queries"} failed, so this pool is
          smaller than it could be. Running the search again will reuse cached results and retry the
          rest.
        </p>
      ) : null}
    </section>
  );
}
