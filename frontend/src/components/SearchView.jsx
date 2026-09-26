import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../api/client.js";
import FilterGrid from "./FilterGrid.jsx";
import LeadsTable from "./LeadsTable.jsx";
import SearchProgress from "./SearchProgress.jsx";

function defaultFilterValues(filterDefs) {
  const values = {};
  for (const [key, def] of Object.entries(filterDefs || {})) {
    if (def.type === "boolean") values[key] = def.default !== false;
    else if (def.default != null) values[key] = def.default;
    else values[key] = "";
  }
  return values;
}

function csvList(value) {
  if (Array.isArray(value)) return value.flatMap(csvList);
  return String(value ?? "")
    .split(/[,;]+/)
    .map((s) => s.trim())
    .filter(Boolean);
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function isOpenStatus(status) {
  return status === "pending" || status === "running";
}

export default function SearchView({ prefill, onClearPrefill }) {
  const [filterDefs, setFilterDefs] = useState({});
  const [filterValues, setFilterValues] = useState({});
  const [name, setName] = useState("");
  const [prompt, setPrompt] = useState("");
  const [leads, setLeads] = useState([]);
  const [meta, setMeta] = useState("");
  const [searching, setSearching] = useState(false);
  const [showResults, setShowResults] = useState(false);
  const [progress, setProgress] = useState(0);
  const [found, setFound] = useState(0);
  const [startedAt, setStartedAt] = useState(null);
  const [searchId, setSearchId] = useState(null);
  const [emailJob, setEmailJob] = useState(null);
  const [findingEmails, setFindingEmails] = useState(false);
  const [lookingId, setLookingId] = useState(null);
  const watchToken = useRef(0);
  const lookingLock = useRef(false);

  useEffect(() => {
    api("/api/filters").then((data) => {
      setFilterDefs(data.filters);
      setFilterValues(defaultFilterValues(data.filters));
    });
  }, []);

  const applySnapshot = useCallback((data) => {
    setProgress(data.search?.progress ?? 0);
    setFound(data.search?.found_count ?? data.count ?? 0);
    setMeta(data.message || "");
    setName(data.search?.name || "");
    setPrompt(data.search?.prompt || "");
    if (data.search?.id) setSearchId(data.search.id);
    if (data.email_job) setEmailJob(data.email_job);
    if (data.leads) setLeads(data.leads);
  }, []);

  const watchSearch = useCallback(
    async (id) => {
      const token = ++watchToken.current;
      while (token === watchToken.current) {
        const data = await api(`/api/searches/${id}`);
        if (token !== watchToken.current) return null;
        applySnapshot(data);
        if (!isOpenStatus(data.search?.status)) {
          setLeads(data.leads || []);
          setMeta(`${data.message} · ${data.mode} mode · ${data.count} leads`);
          setProgress(100);
          return data;
        }
        await sleep(1100);
      }
      return null;
    },
    [applySnapshot]
  );

  const loadSearch = useCallback(
    async (id) => {
      setSearching(true);
      setShowResults(true);
      setLeads([]);
      setProgress(4);
      setFound(0);
      setStartedAt(Date.now());
      setMeta("Loading…");
      try {
        const data = await api(`/api/searches/${id}`);
        applySnapshot(data);
        if (isOpenStatus(data.search?.status)) {
          await watchSearch(id);
        } else {
          setLeads(data.leads || []);
          setMeta(`${data.message} · ${data.count} leads`);
        }
      } catch (err) {
        setMeta(err.message);
        setLeads([]);
      } finally {
        setSearching(false);
      }
    },
    [applySnapshot, watchSearch]
  );

  useEffect(() => {
    if (prefill?.searchId) {
      loadSearch(prefill.searchId);
      onClearPrefill?.();
    }
  }, [prefill, loadSearch, onClearPrefill]);

  useEffect(() => () => {
    watchToken.current += 1;
  }, []);

  async function handleSubmit(e) {
    e.preventDefault();
    watchToken.current += 1;
    setSearching(true);
    setShowResults(true);
    setLeads([]);
    setProgress(1);
    setFound(0);
    setStartedAt(Date.now());
    setMeta("Starting a 2-minute public search…");
    try {
      const filters = {
        ...filterValues,
        job_titles: csvList(filterValues.job_titles),
        interests: csvList(filterValues.interests),
        location: String(filterValues.location ?? "").trim(),
        company: String(filterValues.company ?? "").trim(),
        company_location: String(filterValues.company_location ?? "").trim(),
        industry: String(filterValues.industry ?? "").trim(),
      };
      const data = await api("/api/searches", {
        method: "POST",
        body: { name: name.trim(), prompt: prompt.trim(), filters, save_results: true },
      });
      applySnapshot(data);
      if (isOpenStatus(data.search?.status) && data.search?.id) {
        await watchSearch(data.search.id);
      } else {
        setLeads(data.leads || []);
        setMeta(`${data.message} · ${data.mode} mode · ${data.count} leads`);
      }
    } catch (err) {
      setMeta(err.message);
    } finally {
      setSearching(false);
    }
  }

  async function watchEmails(id) {
    setFindingEmails(true);
    try {
      while (true) {
        const data = await api(`/api/searches/${id}/emails`);
        applySnapshot(data);
        const status = data.email_job?.status;
        if (!status || status === "completed" || status === "failed") {
          if (data.email_job?.message) setMeta(data.email_job.message);
          break;
        }
        await sleep(1200);
      }
    } catch (err) {
      setMeta(err.message);
    } finally {
      setFindingEmails(false);
    }
  }

  async function handleFindEmails() {
    if (!searchId) return;
    setFindingEmails(true);
    setMeta("Finding emails…");
    try {
      const data = await api(`/api/searches/${searchId}/emails`, { method: "POST" });
      applySnapshot(data);
      await watchEmails(searchId);
    } catch (err) {
      setMeta(err.message);
      setFindingEmails(false);
    }
  }

  async function handleFindOne(leadId) {
    if (lookingLock.current) return;
    lookingLock.current = true;
    setLookingId(leadId);
    setMeta("Looking up email…");
    try {
      const updated = await api(`/api/leads/${leadId}/email`, { method: "POST" });
      setLeads((rows) => rows.map((row) => (row.id === updated.id ? { ...row, ...updated } : row)));
      setMeta(updated.email ? `Found ${updated.email}` : "No email found for that person");
    } catch (err) {
      setMeta(err.message);
    } finally {
      lookingLock.current = false;
      setLookingId(null);
    }
  }

  return (
    <section className="view search-view">
      <div className="search-main">
        <h2>New lead search</h2>
        <p className="lede">
          No LinkedIn login required. Mark spends about 2 minutes on a paced public search so it can
          find more people without getting blocked.
        </p>
        <section className="search-results-col" aria-live="polite">
          {showResults ? (
            <>
              <div className="results-head">
                <div>
                  <h3>Results</h3>
                  {searching ? null : <p className="meta">{meta}</p>}
                  {emailJob?.status === "running" || findingEmails ? (
                    <p className="meta">
                      Emails {emailJob?.processed ?? 0}/{emailJob?.total ?? leads.length}
                      {emailJob?.message ? ` · ${emailJob.message}` : ""}
                    </p>
                  ) : null}
                </div>
                {!searching && searchId && leads.length ? (
                  <button type="button" onClick={handleFindEmails} disabled={findingEmails}>
                    {findingEmails ? "Finding emails…" : "Find emails"}
                  </button>
                ) : null}
              </div>
              {searching ? (
                <SearchProgress
                  progress={progress}
                  message={meta}
                  found={found}
                  startedAt={startedAt}
                />
              ) : null}
              {!searching || leads.length ? (
                <LeadsTable
                  leads={leads}
                  onFindOne={findingEmails ? undefined : handleFindOne}
                  lookingId={lookingId}
                />
              ) : null}
            </>
          ) : (
            <div className="empty search-results-empty">
              <p>Set a prompt and filters on the right, then find leads. Results will show here.</p>
            </div>
          )}
        </section>
      </div>

      <form className="panel search-sidebar" onSubmit={handleSubmit}>
        <h3 className="search-sidebar-title">Search filters</h3>
        <div className="search-sidebar-body">
          <div className="field">
            <label htmlFor="search-name">Search name</label>
            <input
              id="search-name"
              required
              placeholder="e.g. AI in Healthcare Administration"
              value={name}
              onChange={(e) => setName(e.target.value)}
            />
          </div>
          <div className="field">
            <label htmlFor="prompt">Prompt search</label>
            <textarea
              id="prompt"
              rows={3}
              required
              placeholder="e.g. Founders of boutique fitness studios in Austin who are active online"
              value={prompt}
              onChange={(e) => setPrompt(e.target.value)}
            />
          </div>
          <FilterGrid filterDefs={filterDefs} values={filterValues} onChange={setFilterValues} />
        </div>
        <div className="actions">
          <button type="submit" disabled={searching}>
            {searching ? "Searching for 2 minutes…" : "Find leads"}
          </button>
          <p className="search-pace-hint">Paced on purpose — better chance at dozens of real people, not a rate-limit wall.</p>
        </div>
      </form>
    </section>
  );
}
