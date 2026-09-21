import { useState } from "react";

function asDraft(value) {
  if (Array.isArray(value)) return value.join(", ");
  return value ?? "";
}

export default function FilterGrid({ filterDefs, values, onChange }) {
  const [customMode, setCustomMode] = useState({});

  if (!filterDefs || !Object.keys(filterDefs).length) {
    return <p className="meta">Loading filters…</p>;
  }

  function setValue(key, val) {
    onChange({ ...values, [key]: val });
  }

  function isCustom(key, options) {
    const current = asDraft(values[key]);
    if (customMode[key]) return true;
    return Boolean(current) && !(options || []).includes(current);
  }

  function selectValue(key, options) {
    if (isCustom(key, options)) return "__custom__";
    return values[key] ?? "";
  }

  return (
    <div className="filter-grid">
      {Object.entries(filterDefs).map(([key, def]) => {
        const multi = def.type === "tags" || def.multi;
        const hint = def.hint || (multi ? "Separate multiple values with commas. Spaces are allowed." : "");
        return (
          <div key={key} className={`field ${multi ? "span-2" : ""}`}>
            <label htmlFor={`f-${key}`}>{def.label}</label>
            {def.type === "select" && (
              <>
                <select
                  id={`f-${key}`}
                  value={selectValue(key, def.options)}
                  onChange={(e) => {
                    const next = e.target.value;
                    if (next === "__custom__") {
                      setCustomMode((prev) => ({ ...prev, [key]: true }));
                      if ((def.options || []).includes(values[key])) {
                        setValue(key, "");
                      }
                    } else {
                      setCustomMode((prev) => ({ ...prev, [key]: false }));
                      setValue(key, next);
                    }
                  }}
                >
                  <option value="">Any</option>
                  {(def.options || []).map((o) => (
                    <option key={o} value={o}>{o}</option>
                  ))}
                  <option value="__custom__">Custom…</option>
                </select>
                {isCustom(key, def.options) && (
                  <>
                    <input
                      id={`f-${key}-custom`}
                      type="text"
                      className="filter-custom-input"
                      placeholder={def.placeholder || "Type one or more values, separated by commas"}
                      value={asDraft(values[key])}
                      onChange={(e) => setValue(key, e.target.value)}
                      autoComplete="off"
                    />
                    <p className="field-hint">Separate multiple values with commas. Spaces are allowed.</p>
                  </>
                )}
              </>
            )}
            {def.type === "boolean" && (
              <input
                id={`f-${key}`}
                type="checkbox"
                checked={values[key] ?? def.default !== false}
                onChange={(e) => setValue(key, e.target.checked)}
              />
            )}
            {(def.type === "tags" || def.type === "text") && (
              <>
                <input
                  id={`f-${key}`}
                  type="text"
                  placeholder={def.placeholder || (def.suggestions || []).slice(0, 3).join(", ")}
                  value={asDraft(values[key])}
                  onChange={(e) => setValue(key, e.target.value)}
                  autoComplete="off"
                />
                {hint && <p className="field-hint">{hint}</p>}
              </>
            )}
            {def.type === "number" && (
              <input
                id={`f-${key}`}
                type="number"
                min={def.min}
                max={def.max}
                placeholder={def.placeholder}
                value={values[key] ?? def.default ?? ""}
                onChange={(e) => setValue(key, e.target.value === "" ? "" : Number(e.target.value))}
              />
            )}
          </div>
        );
      })}
    </div>
  );
}
