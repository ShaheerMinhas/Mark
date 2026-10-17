import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../api/client.js";
import FilterGrid from "./FilterGrid.jsx";
import LeadsTable from "./LeadsTable.jsx";
import SearchProgress from "./SearchProgress.jsx";
import SearchFunnel from "./SearchFunnel.jsx";

const POLL_INTERVAL_MS = 1200;
const PAGE_SIZE = 50;
const OPEN_STATUSES = new Set(["pending", "discovering", "filtering", "enriching", "running"]);

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

function isOpen(status) {
  return OPEN_STATUSES.has(status);
}

export default function SearchView({ prefill, onClearPrefill }) {
  const [filterDefs, setFilterDefs] = useState({});
  const [filterValues, setFilterValues] = useState({});
  const [name, setName] = useState("");
  const [prompt, setPrompt] = useState("");
  const [leads, setLeads] = useState([]);
  const [pageMeta, setPageMeta] = useState(null);
  const [page, setPage] = useState(1);
  const [meta, setMeta] = useState("");
  const [searching, setSearching] = useState(false);
  const [showResults, setShowResults] = useState(false);
  const [progress, setProgress] = useState(null);
  const [funnel, setFunnel] = useState(null);
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

  const loadPage = useCallback(async (id, pageNumber) => {
    const data = await api(
      `/api/search-jobs/${id}/leads?page=${pageNumber}&page_size=${PAGE_SIZE}`
    );
    setLeads(data.items || []);
    setPageMeta(data.page || null);
    return data;
  }, []);

  const loadFunnel = useCallback(async (id) => {
    try {
      setFunnel(await api(`/api/search-jobs/${id}/funnel`));
    } catch {
      setFunnel(null);
    }
  }, []);

  /**
   * Polls job status and refreshes the visible page whenever the backend reports
   * more leads. Leads appear while discovery is still running.
   */
  const watchSearch = useCallback(
    async (id) => {
      const token = ++watchToken.current;
      let lastLeadCount = -1;

      while (token === watchToken.current) {
        const status = await api(`/api/search-jobs/${id}`);
        if (token !== watchToken.current) return null;

        setProgress(status);
        setMeta(status.message || "");

        if (status.lead_count !== lastLeadCount) {
          lastLeadCount = status.lead_count;
          if (status.lead_count > 0) {
            await loadPage(id, 1);
            setPage(1);
          }
        }

        if (!isOpen(status.status)) {
          await loadFunnel(id);
          if (status.lead_count > 0) await loadPage(id, 1);
          return status;
        }
        await sleep(POLL_INTERVAL_MS);
      }
      return null;
    },
    [loadFunnel, loadPage]
  );

  const loadSearch = useCallback(
    async (id) => {
      watchToken.current += 1;
      setSearching(true);
      setShowResults(true);
      setLeads([]);
      setPageMeta(null);
      setFunnel(null);
      setProgress(null);
      setSearchId(id);
      setMeta("Loading…");
      try {
        const status = await api(`/api/search-jobs/${id}`);
        setProgress(status);
        setName(status.name || "");
        if (isOpen(status.status)) {
          await watchSearch(id);
        } else {
          setMeta(status.message || "");
          await loadFunnel(id);
          if (status.lead_count > 0) await loadPage(id, 1);
        }
      } catch (err) {
        setMeta(err.message);
        setLeads([]);
      } finally {
        setSearching(false);
      }
    },
    [loadFunnel, loadPage, watchSearch]
  );

  useEffect(() => {
    if (prefill?.searchId) {
      loadSearch(prefill.searchId);
      onClearPrefill?.();
    }
  }, [prefill, loadSearch, onClearPrefill]);

  useEffect(
    () => () => {
      watchToken.current += 1;
    },
    []
  );

  async function handleSubmit(e) {
    e.preventDefault();
    watchToken.current += 1;
    setSearching(true);
    setShowResults(true);
    setLeads([]);
    setPageMeta(null);
    setFunnel(null);
    setProgress(null);
    setEmailJob(null);
    setMeta("Queued — discovery is starting…");
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
      // Returns immediately with a job id; the worker does the discovery.
      const created = await api("/api/searches", {
        method: "POST",
        body: { name: name.trim(), prompt: prompt.trim(), filters, save_results: true },
      });
      const id = created.search?.id;
      setSearchId(id);
      if (id) await watchSearch(id);
    } catch (err) {
      setMeta(err.message);
    } finally {
      setSearching(false);
    }
  }

  async function goToPage(pageNumber) {
    if (!searchId) return;
    try {
      await loadPage(searchId, pageNumber);
      setPage(pageNumber);
    } catch (err) {
      setMeta(err.message);
    }
  }

  async function handleCancel() {
    if (!searchId) return;
    try {
      setProgress(await api(`/api/search-jobs/${searchId}/cancel`, { method: "POST" }));
    } catch (err) {
      setMeta(err.message);
    }
  }

  async function watchEmails(id) {
    setFindingEmails(true);
    try {
      while (true) {
        const data = await api(`/api/searches/${id}/emails`);
        setEmailJob(data.email_job || null);
        const status = data.email_job?.status;
        if (!status || status === "completed" || status === "failed") {
          if (data.email_job?.message) setMeta(data.email_job.message);
          break;
        }
        await sleep(POLL_INTERVAL_MS);
      }
      await loadPage(id, page);
      await loadFunnel(id);
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
      setEmailJob(data.email_job || null);
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

  const totalLeads = pageMeta?.total ?? leads.length;

  return (
    <section className="view search-view">
      <div className="search-main">
        <h2>New lead search</h2>
        <p className="lede">
          No LinkedIn login required. Mark searches public profiles in the background and shows
          leads as it finds them — you don't have to wait for the whole run.
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
                      Emails {emailJob?.processed ?? 0}/{emailJob?.total ?? totalLeads}
                      {emailJob?.message ? ` · ${emailJob.message}` : ""}
                    </p>
                  ) : null}
                </div>
                <div className="results-actions">
                  {searching ? (
                    <button type="button" onClick={handleCancel}>
                      Stop search
                    </button>
                  ) : null}
                  {!searching && searchId && leads.length ? (
                    <button type="button" onClick={handleFindEmails} disabled={findingEmails}>
                      {findingEmails ? "Finding emails…" : "Find emails"}
                    </button>
                  ) : null}
                </div>
              </div>

              {searching ? (
                <SearchProgress
                  progress={progress?.progress ?? 0}
                  message={meta}
                  status={progress?.status}
                  stage={progress?.current_stage}
                  counters={progress}
                />
              ) : (
                <SearchFunnel funnel={funnel} />
              )}

              <LeadsTable
                leads={leads}
                onFindOne={findingEmails ? undefined : handleFindOne}
                lookingId={lookingId}
              />

              {pageMeta && pageMeta.total_pages > 1 ? (
                <nav className="results-pager" aria-label="Lead pages">
                  <button type="button" onClick={() => goToPage(page - 1)} disabled={page <= 1}>
                    Previous
                  </button>
                  <span>
                    Page {pageMeta.page} of {pageMeta.total_pages} · {pageMeta.total} leads
                  </span>
                  <button
                    type="button"
                    onClick={() => goToPage(page + 1)}
                    disabled={!pageMeta.has_next}
                  >
                    Next
                  </button>
                </nav>
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
              placeholder="e.g. Marketing managers in Swedish logistics companies"
              value={prompt}
              onChange={(e) => setPrompt(e.target.value)}
            />
          </div>
          <FilterGrid filterDefs={filterDefs} values={filterValues} onChange={setFilterValues} />
        </div>
        <div className="actions">
          <button type="submit" disabled={searching}>
            {searching ? "Searching…" : "Find leads"}
          </button>
          <p className="search-pace-hint">
            Runs in the background. Candidates are saved as they're found, so a rate limit never
            wipes out the whole search.
          </p>
        </div>
      </form>
    </section>
  );
}
