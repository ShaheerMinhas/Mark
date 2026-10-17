const STAGES = [
  { key: "discovery", label: "Discover" },
  { key: "qualification", label: "Qualify" },
  { key: "enrichment", label: "Enrich" },
];

const STAGE_FOR_STATUS = {
  pending: null,
  discovering: "discovery",
  filtering: "qualification",
  enriching: "enrichment",
};

function plural(count, one, many) {
  return `${count} ${count === 1 ? one : many}`;
}

/**
 * Renders only values reported by the backend. There is no time-based estimate:
 * a stalled search looks stalled rather than appearing to progress.
 */
export default function SearchProgress({ progress = 0, message = "", status, stage, counters }) {
  const {
    total_queries: totalQueries = 0,
    completed_queries: completedQueries = 0,
    failed_queries: failedQueries = 0,
    candidates_found: candidatesFound = 0,
    candidates_deduplicated: duplicates = 0,
    qualified_leads: qualifiedLeads = 0,
    emails_found: emailsFound = 0,
    phones_found: phonesFound = 0,
  } = counters || {};

  const pct = Math.max(0, Math.min(100, Number(progress) || 0));
  const activeStage = stage || STAGE_FOR_STATUS[status] || null;
  const activeIndex = STAGES.findIndex((item) => item.key === activeStage);
  const queriesDone = completedQueries + failedQueries;

  return (
    <div
      className="search-progress"
      role="progressbar"
      aria-label="Search progress"
      aria-valuemin={0}
      aria-valuemax={100}
      aria-valuenow={Math.round(pct)}
    >
      <div className="search-progress-top">
        <div>
          <p className="search-progress-kicker">Public search</p>
          <h3 className="search-progress-title">
            {candidatesFound > 0
              ? `${plural(candidatesFound, "candidate", "candidates")} found`
              : "Searching public profiles…"}
          </h3>
        </div>
        {totalQueries > 0 ? (
          <div className="search-progress-clock" aria-label="Queries completed">
            <span>
              {queriesDone}/{totalQueries}
            </span>
            <small>queries</small>
          </div>
        ) : null}
      </div>

      <div className="search-progress-track">
        <div className="search-progress-fill" style={{ width: `${pct}%` }} />
      </div>

      <div className="search-progress-meta">
        <p className="search-progress-stage">{message || "Queued — discovery is starting…"}</p>
        <p className="search-progress-found">
          {qualifiedLeads > 0 ? `${plural(qualifiedLeads, "relevant lead", "relevant leads")}` : ""}
          {emailsFound > 0 ? ` · ${emailsFound} emails` : ""}
          {phonesFound > 0 ? ` · ${phonesFound} phones` : ""}
        </p>
      </div>

      <ol className="search-progress-phases">
        {STAGES.map((item, index) => (
          <li
            key={item.key}
            className={
              activeIndex < 0
                ? ""
                : index < activeIndex
                  ? "is-done"
                  : index === activeIndex
                    ? "is-active"
                    : ""
            }
          >
            {item.label}
          </li>
        ))}
      </ol>

      {duplicates > 0 || failedQueries > 0 ? (
        <p className="search-progress-note">
          {duplicates > 0 ? `${duplicates} duplicates removed` : ""}
          {duplicates > 0 && failedQueries > 0 ? " · " : ""}
          {failedQueries > 0
            ? `${plural(failedQueries, "query", "queries")} failed — continuing with the rest`
            : ""}
        </p>
      ) : null}
    </div>
  );
}
