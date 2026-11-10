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

function runIsActive(status) {
  if (!status) return false;
  if (status.run && typeof status.run.active === "boolean") return status.run.active;
  return isOpen(status.status);
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
  const [findingEmails, setFindingEmails] = useState(false);
  const [lookingId, setLookingId] = useState(null);
  const [notice, setNotice] = useState("");
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
    async (id, token) => {
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

        if (!runIsActive(status)) {
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
      const token = ++watchToken.current;
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
        if (token !== watchToken.current) return;
        if (runIsActive(status)) {
          await watchSearch(id, token);
        } else {
          setMeta(status.message || "");
          await loadFunnel(id);
          if (status.lead_count > 0) await loadPage(id, 1);
        }
      } catch (err) {
        if (token === watchToken.current) {
          setMeta(err.message);
          setLeads([]);
        }
      } finally {
        if (token === watchToken.current) setSearching(false);
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
    const token = ++watchToken.current;
    setSearching(true);
    setShowResults(true);
    setLeads([]);
    setPageMeta(null);
    setFunnel(null);
    setProgress(null);
    setNotice("");
    setMeta("Queued. Discovery has not started.");
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
      if (id && token === watchToken.current) await watchSearch(id, token);
    } catch (err) {
      if (token === watchToken.current) setMeta(err.message);
    } finally {
      if (token === watchToken.current) setSearching(false);
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

  async function handleFindEmails() {
    if (!searchId || runIsActive(progress)) return;
    const token = ++watchToken.current;
    setFindingEmails(true);
    setSearching(true);
    setMeta("Email lookup is starting…");
    try {
      await api(`/api/searches/${searchId}/emails`, { method: "POST" });
      if (token === watchToken.current) await watchSearch(searchId, token);
    } catch (err) {
      if (token === watchToken.current) setMeta(err.message);
    } finally {
      if (token === watchToken.current) {
        setFindingEmails(false);
        setSearching(false);
      }
    }
  }

  async function handleFindOne(leadId) {
    if (lookingLock.current) return;
    lookingLock.current = true;
    setLookingId(leadId);
    setNotice("Looking up an email for this person…");
    try {
      const updated = await api(`/api/leads/${leadId}/email`, { method: "POST" });
      setLeads((rows) => rows.map((row) => (row.id === updated.id ? { ...row, ...updated } : row)));
      setNotice(updated.email ? `Found ${updated.email}` : "No email found for that person.");
    } catch (err) {
      setNotice(err.message);
    } finally {
      lookingLock.current = false;
      setLookingId(null);
    }
  }

  const totalLeads = pageMeta?.total ?? leads.length;
  const runActive = searching || runIsActive(progress);

  return (
    <section className={`view search-view ${showResults ? "has-results" : "is-idle"}`}>
      <div className="search-main">
        <div className="view-head">
          <div>
            <h2>Find people</h2>
            <p className="lede">
              Describe who you want. Results show up here while the search runs, including whether email lookup is on.
            </p>
          </div>
        </div>
        <section className="search-results-col" aria-live="polite">
          {showResults ? (
            <>
              <div className="results-head">
                <div>
                  <h3>Results</h3>
                  {notice ? <p className={`notice ${notice.startsWith("Found ") ? "is-ok" : ""}`}>{notice}</p> : null}
                </div>
                <div className="results-actions">
                  {runActive ? (
                    <button type="button" className="ghost" onClick={handleCancel}>
                      Stop search
                    </button>
                  ) : null}
                  {!runActive && searchId && totalLeads > 0 ? (
                    <button type="button" className="btn-new-search" onClick={handleFindEmails} disabled={findingEmails}>
                      Find emails
                    </button>
                  ) : null}
                </div>
              </div>

              {runActive || progress?.run ? (
                <SearchProgress run={progress?.run} counters={progress} />
              ) : null}
              {runActive ? null : <SearchFunnel funnel={funnel} />}

              <LeadsTable
                leads={leads}
                onFindOne={runActive || findingEmails ? undefined : handleFindOne}
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
              <p>Name the search and describe who you want. Results will show here.</p>
            </div>
          )}
        </section>
      </div>

      <form className="panel search-sidebar" onSubmit={handleSubmit}>
        <h3 className="search-sidebar-title">Search</h3>
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
            <label htmlFor="prompt">Who to find</label>
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
            People are saved as they are found. Email lookup follows the choice below.
          </p>
        </div>
      </form>
    </section>
  );
}
