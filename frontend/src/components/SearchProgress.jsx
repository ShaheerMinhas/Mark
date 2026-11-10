const EMAIL_CHIP = {
  off: "Off",
  waiting: "Waiting",
  active: "Looking",
  done: "Done",
  skipped: "Skipped",
  failed: "Failed",
  stopped: "Stopped",
};

function plural(count, one, many) {
  return `${count} ${count === 1 ? one : many}`;
}

/**
 * One progress story for a search run. The percent and the email state come from
 * the backend. There is no time-based estimate and no percent when nothing has
 * been measured yet.
 */
export default function SearchProgress({ run, counters }) {
  const email = run?.email_lookup;
  const stages = run?.stages || [];
  const determinate = Boolean(run?.determinate) && run?.percent != null;
  const percent = determinate ? Math.max(0, Math.min(100, Number(run.percent))) : null;
  const active = Boolean(run?.active);
  const outcome = run?.stage || "";
  const tone =
    outcome === "failed" || email?.state === "failed"
      ? "is-failed"
      : outcome === "cancelled" || email?.state === "stopped"
        ? "is-stopped"
        : active
          ? "is-active"
          : "is-settled";

  const {
    total_queries: totalQueries = 0,
    completed_queries: completedQueries = 0,
    failed_queries: failedQueries = 0,
    candidates_found: candidatesFound = 0,
    candidates_deduplicated: duplicates = 0,
    qualified_leads: qualifiedLeads = 0,
  } = counters || {};
  const queriesDone = completedQueries + failedQueries;

  const emailLine = email
    ? describeEmail(email)
    : "Email lookup status is not available yet.";

  return (
    <div
      className={`search-progress ${tone}`}
      role="progressbar"
      aria-label="Search progress"
      aria-valuemin={0}
      aria-valuemax={100}
      aria-valuenow={determinate ? Math.round(percent) : undefined}
      aria-busy={active}
    >
      <div className="search-progress-top">
        <div>
          <p className="search-progress-kicker">{active ? "Search in progress" : "Search status"}</p>
          <h3 className="search-progress-title">{run?.stage_label || "Waiting for search status"}</h3>
        </div>
        <div className="search-progress-clock" aria-label={determinate ? "Measured progress" : "Progress not measured yet"}>
          <span>{determinate ? `${Math.round(percent)}%` : "—"}</span>
          <small>{determinate ? "measured" : "not measured"}</small>
        </div>
      </div>

      <div className={`search-progress-track ${determinate ? "" : "is-indeterminate"}`}>
        <div
          className="search-progress-fill"
          style={determinate ? { width: `${percent}%` } : undefined}
        />
      </div>

      <p className="search-progress-stage">{run?.message || "Waiting for the search to report what it is doing."}</p>
      <p className="search-progress-email">{emailLine}</p>

      {stages.length ? (
        <ol className="search-progress-phases">
          {stages.map((item) => (
            <li key={item.key} className={`is-${item.state || "pending"}`}>
              <span>{item.label}</span>
              {item.key === "email" ? (
                <small>{EMAIL_CHIP[item.state] || item.state}</small>
              ) : null}
            </li>
          ))}
        </ol>
      ) : null}

      <p className="search-progress-found">
        {totalQueries > 0 ? `${queriesDone}/${totalQueries} queries` : "Queries not planned yet"}
        {candidatesFound > 0 ? ` · ${plural(candidatesFound, "candidate", "candidates")}` : ""}
        {qualifiedLeads > 0 ? ` · ${plural(qualifiedLeads, "relevant lead", "relevant leads")}` : ""}
        {duplicates > 0 ? ` · ${duplicates} duplicates removed` : ""}
        {failedQueries > 0
          ? ` · ${plural(failedQueries, "query failed", "queries failed")}`
          : ""}
      </p>
    </div>
  );
}

function describeEmail(email) {
  const state = email.state;
  if (state === "running") {
    if (email.total > 0) {
      return `Email lookup is running · checked ${email.processed} of ${email.total}.`;
    }
    return "Email lookup is running.";
  }
  if (state === "pending") {
    return email.detail || "Email lookup has not started.";
  }
  if (state === "not_requested") {
    return "Email lookup is off. This search will not look for addresses.";
  }
  if (state === "finished") {
    return email.detail || "Email lookup finished.";
  }
  if (state === "skipped") {
    return email.detail || "Email lookup did not run.";
  }
  if (state === "failed") {
    return "Email lookup failed. It is not still running.";
  }
  if (state === "stopped") {
    return email.detail || "Email lookup was stopped.";
  }
  return email.detail || "Email lookup status is unknown.";
}
