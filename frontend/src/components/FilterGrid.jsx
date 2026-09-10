export default function FilterGrid({ filterDefs, values, onChange }) {
  if (!filterDefs || !Object.keys(filterDefs).length) {
    return <p className="meta">Loading filters…</p>;
  }

  function setValue(key, val) {
    onChange({ ...values, [key]: val });
  }

  return (
    <div className="filter-grid">
      {Object.entries(filterDefs).map(([key, def]) => (
        <div key={key} className={`field ${def.type === "tags" ? "span-2" : ""}`}>
          <label htmlFor={`f-${key}`}>{def.label}</label>
          {def.type === "select" && (
            <select
              id={`f-${key}`}
              value={values[key] ?? ""}
              onChange={(e) => setValue(key, e.target.value)}
            >
              <option value="">Any</option>
              {(def.options || []).map((o) => (
                <option key={o} value={o}>{o}</option>
              ))}
            </select>
          )}
          {def.type === "boolean" && (
            <input
              id={`f-${key}`}
              type="checkbox"
              checked={values[key] ?? def.default !== false}
              onChange={(e) => setValue(key, e.target.checked)}
            />
          )}
          {def.type === "tags" && (
            <input
              id={`f-${key}`}
              type="text"
              placeholder={(def.suggestions || []).slice(0, 3).join(", ")}
              value={Array.isArray(values[key]) ? values[key].join(", ") : values[key] ?? ""}
              onChange={(e) =>
                setValue(
                  key,
                  e.target.value.split(",").map((s) => s.trim()).filter(Boolean)
                )
              }
            />
          )}
          {(def.type === "text" || def.type === "number") && (
            <input
              id={`f-${key}`}
              type={def.type === "number" ? "number" : "text"}
              min={def.min}
              max={def.max}
              placeholder={def.placeholder}
              value={values[key] ?? def.default ?? ""}
              onChange={(e) =>
                setValue(key, def.type === "number" ? Number(e.target.value) : e.target.value)
              }
            />
          )}
        </div>
      ))}
    </div>
  );
}
