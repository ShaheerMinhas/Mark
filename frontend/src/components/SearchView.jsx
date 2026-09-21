import { useCallback, useEffect, useState } from "react";
import { api } from "../api/client.js";
import FilterGrid from "./FilterGrid.jsx";
import LeadsTable from "./LeadsTable.jsx";

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

export default function SearchView({ prefill, onClearPrefill }) {
  const [filterDefs, setFilterDefs] = useState({});
  const [filterValues, setFilterValues] = useState({});
  const [name, setName] = useState("");
  const [prompt, setPrompt] = useState("");
  const [leads, setLeads] = useState([]);
  const [meta, setMeta] = useState("");
  const [searching, setSearching] = useState(false);
  const [showResults, setShowResults] = useState(false);

  useEffect(() => {
    api("/api/filters").then((data) => {
      setFilterDefs(data.filters);
      setFilterValues(defaultFilterValues(data.filters));
    });
  }, []);

  const loadSearch = useCallback(async (id) => {
    setSearching(true);
    setShowResults(true);
    setMeta("Loading…");
    try {
      const data = await api(`/api/searches/${id}`);
      setLeads(data.leads);
      setMeta(`${data.message} · ${data.count} leads`);
      setName(data.search.name);
      setPrompt(data.search.prompt);
    } catch (err) {
      setMeta(err.message);
      setLeads([]);
    } finally {
      setSearching(false);
    }
  }, []);

  useEffect(() => {
    if (prefill?.searchId) {
      loadSearch(prefill.searchId);
      onClearPrefill?.();
    }
  }, [prefill, loadSearch, onClearPrefill]);

  async function handleSubmit(e) {
    e.preventDefault();
    setSearching(true);
    setShowResults(true);
    setMeta("Running search…");
    setLeads([]);
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
      setLeads(data.leads);
      setMeta(`${data.message} · ${data.mode} mode · ${data.count} leads`);
    } catch (err) {
      setMeta(err.message);
    } finally {
      setSearching(false);
    }
  }

  return (
    <section className="view search-view">
      <div className="search-main">
        <h2>New lead search</h2>
        <p className="lede">
          No LinkedIn login required. Mark finds publicly indexed business people from the open web.
        </p>
        <section className="search-results-col" aria-live="polite">
          {showResults ? (
            <>
              <div className="results-head">
                <h3>Results</h3>
                <p className="meta">{meta}</p>
              </div>
              <LeadsTable leads={leads} />
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
            {searching ? "Searching…" : "Find leads"}
          </button>
        </div>
      </form>
    </section>
  );
}
