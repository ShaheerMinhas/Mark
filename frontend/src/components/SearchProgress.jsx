import { useEffect, useState } from "react";

const SEARCH_SECONDS = 120;
const PHASES = [
  { until: 88, label: "Find people" },
  { until: 96, label: "Map companies" },
  { until: 101, label: "Public emails" },
];

function formatClock(totalSeconds) {
  const s = Math.max(0, Math.floor(totalSeconds));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}

export default function SearchProgress({ progress = 0, message = "", found = 0, startedAt }) {
  const [now, setNow] = useState(Date.now());

  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), 250);
    return () => clearInterval(timer);
  }, []);

  const elapsed = startedAt ? (now - startedAt) / 1000 : 0;
  const remaining = Math.max(0, SEARCH_SECONDS - elapsed);
  const overtime = elapsed > SEARCH_SECONDS;
  const server = Math.max(0, Math.min(99, Number(progress) || 0));
  const timed = Math.min(90, (elapsed / SEARCH_SECONDS) * 90);
  const pct = Math.min(99, Math.max(server, timed * 0.4 + server * 0.6));
  const phaseIndex = PHASES.findIndex((phase) => pct < phase.until);
  const activePhase = phaseIndex < 0 ? PHASES.length - 1 : phaseIndex;
  const foundLabel = found === 1 ? "1 person" : `${found} people`;

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
          <h3 className="search-progress-title">Taking about 2 minutes</h3>
        </div>
        <div className="search-progress-clock" aria-label="Time remaining">
          <span>{overtime ? "Wrapping up" : formatClock(remaining)}</span>
          <small>{overtime ? "finishing details" : "left"}</small>
        </div>
      </div>

      <div className="search-progress-track">
        <div className="search-progress-fill" style={{ width: `${pct}%` }} />
      </div>

      <div className="search-progress-meta">
        <p className="search-progress-stage">{message || "Starting a 2-minute public search…"}</p>
        <p className="search-progress-found">{foundLabel} so far</p>
      </div>

      <ol className="search-progress-phases">
        {PHASES.map((phase, index) => (
          <li
            key={phase.label}
            className={
              index < activePhase ? "is-done" : index === activePhase ? "is-active" : ""
            }
          >
            {phase.label}
          </li>
        ))}
      </ol>
    </div>
  );
}
