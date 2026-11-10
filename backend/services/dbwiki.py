"""Generate docs/dbwiki.html from the live SQLAlchemy schema.

Rewrites the wiki only when the inspected schema fingerprint changes.
"""

from __future__ import annotations

import hashlib
import html
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import inspect as sa_inspect

from backend.config import DATABASE_URL, ROOT

WIKI_PATH = ROOT / "dbwiki.html"

TABLE_DOCS: dict[str, str] = {
    "users": "People who sign in. Every search is owned by a user.",
    "organizations": "Team workspaces. Optional — individual accounts do not need one.",
    "organization_members": "Join table: which users belong to which org, and their role.",
    "linkedin_sessions": "Stored LinkedIn cookies. Current public search does not use this.",
    "search_jobs": "One saved search: prompt, filters, status, and who ran it.",
    "lead_records": "Customer-owned leads for one search. Notes and tags stay here and are not copied into the shared cache.",
    "global_people": "Shared public prospects. No workspace notes, tags, or CRM fields.",
    "global_companies": "Shared company evidence, reused by domain when a domain is known.",
    "global_person_technologies": "Normalized technology labels on a shared person. These are attributes, not connected services.",
    "global_evidence": "Field-level provenance. Ineligible rows are kept and are not served.",
    "global_identity_keys": "Dedup keys: profile URL, email, provider id, or name plus domain. Never a name alone.",
    "cache_policies": "Rules for Basic, Enriched, Contactable, and Verified labels. Not an accuracy guarantee.",
    "schema_migrations": "Versioned schema steps that have been applied.",
    "domain_email_patterns": "Learned {first}.{last} style patterns per company domain.",
    "email_evidence": "Why Mark believes an email: public URL, provider, or pattern.",
    "email_verification_events": "Each verify attempt (local or provider).",
    "provider_usage": "Finder/verify calls for cost tracking. No hard-coded prices.",
    "email_lookup_cache": "Recent person+company email results. Avoids repeat spend.",
    "email_enrichment_jobs": "Background Find Emails run for a saved search.",
}

COLUMN_DOCS: dict[tuple[str, str], str] = {
    ("users", "id"): "Primary key.",
    ("users", "email"): "Login email. Unique.",
    ("users", "full_name"): "Display name in the top bar.",
    ("users", "hashed_password"): "bcrypt hash — never store plain passwords.",
    ("users", "account_type"): "individual or organization.",
    ("users", "is_active"): "If false, login should be rejected.",
    ("users", "created_at"): "Account created (UTC).",
    ("organizations", "id"): "Primary key.",
    ("organizations", "name"): "Team name. Unique.",
    ("organizations", "slug"): "URL-safe unique key.",
    ("organizations", "created_at"): "Org created (UTC).",
    ("organization_members", "id"): "Primary key.",
    ("organization_members", "organization_id"): "FK → organizations.id",
    ("organization_members", "user_id"): "FK → users.id",
    ("organization_members", "role"): "owner, admin, or member.",
    ("organization_members", "joined_at"): "When they joined (UTC).",
    ("linkedin_sessions", "id"): "Primary key.",
    ("linkedin_sessions", "user_id"): "FK → users.id",
    ("linkedin_sessions", "label"): "User-facing name, e.g. Primary.",
    ("linkedin_sessions", "encrypted_cookie"): "Encrypted li_at cookie. Unused by public search.",
    ("linkedin_sessions", "linkedin_name"): "Cached LinkedIn display name.",
    ("linkedin_sessions", "is_valid"): "Last validation result.",
    ("linkedin_sessions", "last_validated_at"): "When the cookie was last checked.",
    ("linkedin_sessions", "created_at"): "Session saved (UTC).",
    ("search_jobs", "id"): "Primary key. Returned as search_id in the API.",
    ("search_jobs", "name"): "User-given search name.",
    ("search_jobs", "prompt"): "Natural-language search prompt.",
    ("search_jobs", "filters"): "JSON of FilterGrid values (location, industry, …).",
    ("search_jobs", "linkedin_url"): "Legacy field. Public search leaves this null.",
    ("search_jobs", "query_used"): "Actual DuckDuckGo query string.",
    ("search_jobs", "mode"): "live, sample, or public.",
    ("search_jobs", "status"): "pending → running → completed | failed.",
    ("search_jobs", "message"): "Human status / error text.",
    ("search_jobs", "owner_id"): "FK → users.id (who ran the search).",
    ("search_jobs", "organization_id"): "Optional FK → organizations.id for shared searches.",
    ("search_jobs", "created_at"): "Search created (UTC).",
    ("lead_records", "id"): "Primary key.",
    ("lead_records", "search_job_id"): "FK → search_jobs.id. Cascade delete with the search.",
    ("lead_records", "name"): "Person name from the LinkedIn SERP.",
    ("lead_records", "email"): "Best email candidate. Empty if NOT_FOUND. Never a dummy guess.",
    ("lead_records", "email_status"): "found | inferred | verified | unverified | not_found | verify_uncertain.",
    ("lead_records", "email_confidence"): "high | medium | low.",
    ("lead_records", "email_source"): "snippet, public_web, pattern, or provider name.",
    ("lead_records", "email_pattern"): "Detected pattern such as first.last.",
    ("lead_records", "company_domain"): "Employer website host when known.",
    ("lead_records", "phone"): "Phone if scraped.",
    ("lead_records", "personal_location"): "Person location.",
    ("lead_records", "ai_description"): "Short AI / template summary.",
    ("lead_records", "work_field"): "Industry / field.",
    ("lead_records", "work_designation"): "Job title.",
    ("lead_records", "company_name"): "Employer name.",
    ("lead_records", "company_location"): "Company location (often copied from filters).",
    ("lead_records", "linkedin_url"): "https://www.linkedin.com/in/…",
    ("lead_records", "relevance_score"): "0–1 score vs the prompt.",
    ("lead_records", "activity_status"): "Active / Likely active / Indexed profile.",
    ("lead_records", "raw_snippet"): "Search snippet used for parsing.",
    ("lead_records", "tags"): "JSON list of filter tags.",
    ("lead_records", "created_at"): "Lead saved (UTC).",
}

ENUM_DOCS: dict[str, list[str]] = {
    "account_type": ["individual", "organization"],
    "role": ["owner", "admin", "member"],
    "status": ["pending", "running", "completed", "failed"],
}


def _esc(value: Any) -> str:
    return html.escape("" if value is None else str(value))


def _type_name(col: dict[str, Any]) -> str:
    t = col.get("type")
    return str(t) if t is not None else "?"


def schema_fingerprint(inspector) -> str:
    chunks: list[str] = []
    for table in sorted(inspector.get_table_names()):
        cols = [
            (
                c["name"],
                str(c.get("type")),
                bool(c.get("nullable")),
                str(c.get("default")),
                bool(c.get("autoincrement")),
            )
            for c in inspector.get_columns(table)
        ]
        pks = inspector.get_pk_constraint(table)
        fks = [
            (fk.get("name"), fk.get("constrained_columns"), fk.get("referred_table"), fk.get("referred_columns"))
            for fk in inspector.get_foreign_keys(table)
        ]
        uniques = [(u.get("name"), u.get("column_names")) for u in inspector.get_unique_constraints(table)]
        indexes = [(i.get("name"), i.get("unique"), i.get("column_names")) for i in inspector.get_indexes(table)]
        chunks.append(repr((table, cols, pks, fks, uniques, indexes)))
    return hashlib.sha256("\n".join(chunks).encode("utf-8")).hexdigest()[:16]


def _read_existing_fingerprint(path: Path) -> str | None:
    if not path.exists():
        return None
    text = path.read_text(encoding="utf-8")
    marker = 'data-schema-fingerprint="'
    start = text.find(marker)
    if start < 0:
        return None
    start += len(marker)
    end = text.find('"', start)
    if end < 0:
        return None
    return text[start:end]


def _row_counts(engine, tables: list[str]) -> dict[str, int | None]:
    counts: dict[str, int | None] = {}
    with engine.connect() as conn:
        for table in tables:
            try:
                counts[table] = int(conn.exec_driver_sql(f'SELECT COUNT(*) FROM "{table}"').scalar() or 0)
            except Exception:
                counts[table] = None
    return counts


def _collect_schema(engine) -> dict[str, Any]:
    from backend import models  # noqa: F401 — register mappers

    inspector = sa_inspect(engine)
    tables = sorted(inspector.get_table_names())
    referenced_by: dict[str, list[dict[str, Any]]] = {t: [] for t in tables}
    table_data: list[dict[str, Any]] = []

    for table in tables:
        fks = inspector.get_foreign_keys(table)
        for fk in fks:
            referred = fk.get("referred_table")
            if referred in referenced_by:
                referenced_by[referred].append(
                    {
                        "from_table": table,
                        "from_cols": fk.get("constrained_columns") or [],
                        "to_cols": fk.get("referred_columns") or [],
                        "name": fk.get("name") or "",
                    }
                )

        pk = inspector.get_pk_constraint(table)
        pk_cols = set(pk.get("constrained_columns") or [])
        unique_cols: set[str] = set()
        for u in inspector.get_unique_constraints(table):
            cols = u.get("column_names") or []
            if len(cols) == 1:
                unique_cols.add(cols[0])

        indexed_cols: set[str] = set()
        for idx in inspector.get_indexes(table):
            for col in idx.get("column_names") or []:
                indexed_cols.add(col)

        fk_by_col: dict[str, dict[str, Any]] = {}
        for fk in fks:
            for col in fk.get("constrained_columns") or []:
                fk_by_col[col] = fk

        columns = []
        for col in inspector.get_columns(table):
            name = col["name"]
            columns.append(
                {
                    "name": name,
                    "type": _type_name(col),
                    "nullable": bool(col.get("nullable")),
                    "default": col.get("default"),
                    "pk": name in pk_cols,
                    "unique": name in unique_cols,
                    "indexed": name in indexed_cols or name in pk_cols,
                    "fk": fk_by_col.get(name),
                    "doc": COLUMN_DOCS.get((table, name), ""),
                    "enums": ENUM_DOCS.get(name, []),
                }
            )

        table_data.append(
            {
                "name": table,
                "doc": TABLE_DOCS.get(table, "No description yet."),
                "columns": columns,
                "foreign_keys": fks,
                "unique": inspector.get_unique_constraints(table),
                "indexes": inspector.get_indexes(table),
                "pk": pk,
            }
        )

    return {
        "tables": table_data,
        "referenced_by": referenced_by,
        "fingerprint": schema_fingerprint(inspector),
        "counts": _row_counts(engine, tables),
        "dialect": engine.dialect.name,
        "url_scheme": DATABASE_URL.split("://", 1)[0],
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
    }


def _badge(text: str, kind: str) -> str:
    return f'<span class="badge badge-{_esc(kind)}">{_esc(text)}</span>'


def _col_badges(col: dict[str, Any]) -> str:
    bits = []
    if col["pk"]:
        bits.append(_badge("PK", "pk"))
    if col["unique"]:
        bits.append(_badge("unique", "unique"))
    if col["indexed"] and not col["pk"] and not col["unique"]:
        bits.append(_badge("index", "idx"))
    if not col["nullable"] and not col["pk"]:
        bits.append(_badge("required", "req"))
    elif col["nullable"]:
        bits.append(_badge("nullable", "null"))
    return " ".join(bits)


def _fk_cell(col: dict[str, Any]) -> str:
    fk = col.get("fk")
    if not fk:
        return "—"
    table = fk.get("referred_table") or ""
    cols = ", ".join(fk.get("referred_columns") or [])
    return (
        f'<a class="ref-link" href="#table-{_esc(table)}">'
        f"{_esc(table)}.{_esc(cols)}</a>"
    )


def _render_html(schema: dict[str, Any]) -> str:
    tables = schema["tables"]
    referenced_by = schema["referenced_by"]
    counts = schema["counts"]
    nav = []
    sections = []

    for table in tables:
        name = table["name"]
        count = counts.get(name)
        count_label = "—" if count is None else str(count)
        nav.append(
            f'<a class="nav-item" data-table="{_esc(name)}" href="#table-{_esc(name)}">'
            f'<span>{_esc(name)}</span><span class="nav-count">{_esc(count_label)}</span></a>'
        )

        rows = []
        for col in table["columns"]:
            default = col["default"]
            default_s = "—" if default is None else str(default)
            enums = ""
            if col["enums"]:
                enums = "<br><span class='enum'>values: " + ", ".join(_esc(v) for v in col["enums"]) + "</span>"
            search_blob = " ".join(
                [
                    name,
                    col["name"],
                    col["type"],
                    col.get("doc") or "",
                    " ".join(col.get("enums") or []),
                ]
            )
            rows.append(
                f"""<tr data-search="{_esc(search_blob.lower())}">
                  <td><code>{_esc(col["name"])}</code></td>
                  <td><code>{_esc(col["type"])}</code>{enums}</td>
                  <td>{_col_badges(col)}</td>
                  <td>{_fk_cell(col)}</td>
                  <td class="muted"><code>{_esc(default_s)}</code></td>
                  <td>{_esc(col["doc"] or "—")}</td>
                </tr>"""
            )

        fk_lines = []
        for fk in table["foreign_keys"]:
            src = ", ".join(fk.get("constrained_columns") or [])
            dst_t = fk.get("referred_table") or ""
            dst_c = ", ".join(fk.get("referred_columns") or [])
            fk_lines.append(
                f'<li><code>{_esc(src)}</code> → '
                f'<a class="ref-link" href="#table-{_esc(dst_t)}">{_esc(dst_t)}.{_esc(dst_c)}</a></li>'
            )
        if not fk_lines:
            fk_lines.append("<li class='muted'>No outgoing foreign keys.</li>")

        inbound = referenced_by.get(name) or []
        in_lines = []
        for ref in inbound:
            src_t = ref["from_table"]
            src_c = ", ".join(ref["from_cols"])
            dst_c = ", ".join(ref["to_cols"])
            in_lines.append(
                f'<li><a class="ref-link" href="#table-{_esc(src_t)}">{_esc(src_t)}</a>'
                f".<code>{_esc(src_c)}</code> → <code>{_esc(dst_c)}</code></li>"
            )
        if not in_lines:
            in_lines.append("<li class='muted'>Nothing else points at this table.</li>")

        idx_lines = []
        for idx in table["indexes"]:
            cols = ", ".join(idx.get("column_names") or [])
            uniq = " unique" if idx.get("unique") else ""
            idx_lines.append(f'<li><code>{_esc(idx.get("name") or "")}</code>{uniq} on <code>{_esc(cols)}</code></li>')
        for u in table["unique"]:
            cols = ", ".join(u.get("column_names") or [])
            idx_lines.append(f'<li>unique <code>{_esc(u.get("name") or "")}</code> on <code>{_esc(cols)}</code></li>')
        if not idx_lines:
            idx_lines.append("<li class='muted'>No extra indexes.</li>")

        sections.append(
            f"""<article class="table-card" id="table-{_esc(name)}" data-table="{_esc(name)}">
              <header class="table-head">
                <h2>{_esc(name)}</h2>
                <p class="lede">{_esc(table["doc"])}</p>
                <p class="meta">{_esc(count_label)} rows</p>
              </header>
              <div class="table-wrap">
                <table>
                  <thead>
                    <tr>
                      <th>Column</th>
                      <th>Type</th>
                      <th>Flags</th>
                      <th>References</th>
                      <th>Default</th>
                      <th>Meaning</th>
                    </tr>
                  </thead>
                  <tbody>
                    {''.join(rows)}
                  </tbody>
                </table>
              </div>
              <div class="ref-grid">
                <section>
                  <h3>This table points to</h3>
                  <ul>{''.join(fk_lines)}</ul>
                </section>
                <section>
                  <h3>Referenced by</h3>
                  <ul>{''.join(in_lines)}</ul>
                </section>
                <section>
                  <h3>Indexes &amp; unique</h3>
                  <ul>{''.join(idx_lines)}</ul>
                </section>
              </div>
            </article>"""
        )

    er_lines = []
    for table in tables:
        for fk in table["foreign_keys"]:
            src = table["name"]
            dst = fk.get("referred_table") or "?"
            cols = ", ".join(fk.get("constrained_columns") or [])
            er_lines.append(
                f'<li><a href="#table-{_esc(src)}">{_esc(src)}</a> '
                f'<span class="muted">({_esc(cols)})</span> → '
                f'<a href="#table-{_esc(dst)}">{_esc(dst)}</a></li>'
            )

    payload = json.dumps(
        {
            "fingerprint": schema["fingerprint"],
            "tables": [t["name"] for t in tables],
        }
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Mark — Database wiki</title>
  <meta name="schema-fingerprint" content="{_esc(schema["fingerprint"])}" />
</head>
<body data-schema-fingerprint="{_esc(schema["fingerprint"])}">
  <div class="shell">
    <aside class="sidebar">
      <a class="brand" href="#top">Mark DB</a>
      <p class="sidebar-sub">Schema wiki</p>
      <label class="search-wrap" for="db-search">Search</label>
      <input id="db-search" type="search" placeholder="Table or column…" autocomplete="off" />
      <nav id="table-nav" aria-label="Tables">{''.join(nav)}</nav>
      <p class="sidebar-meta">
        {_esc(schema["dialect"])} · {_esc(schema["url_scheme"])}<br />
        fingerprint {_esc(schema["fingerprint"])}<br />
        {_esc(schema["generated_at"])}
      </p>
    </aside>
    <main id="top">
      <header class="page-head">
        <h1>Database wiki</h1>
        <p class="lede">
          Live schema from SQLAlchemy + the connected database.
          Regenerated when tables, columns, indexes, or foreign keys change.
        </p>
      </header>
      <section class="overview">
        <h2>How tables connect</h2>
        <ul class="er-list">{''.join(er_lines)}</ul>
      </section>
      {''.join(sections)}
      <p id="no-results" class="empty hidden">No tables or columns match that search.</p>
    </main>
  </div>
  <script id="dbwiki-meta" type="application/json">{payload}</script>
  <style>
    :root {{
      --ink: #10231f;
      --ink-soft: #1c3a33;
      --paper: #f4faf7;
      --line: rgba(16, 35, 31, 0.14);
      --accent: #e85d04;
      --accent-deep: #c44900;
      --good: #0f7a5a;
      --sidebar: #0e1f1c;
      --radius: 12px;
      --font-display: "Fraunces", Georgia, serif;
      --font-body: "Sora", "Segoe UI", sans-serif;
    }}
    * {{ box-sizing: border-box; }}
    html {{ scroll-behavior: smooth; }}
    body {{
      margin: 0;
      font-family: var(--font-body);
      color: var(--ink);
      background: linear-gradient(165deg, #dceee7, var(--paper) 45%, #eef6f2);
    }}
    .shell {{ display: grid; grid-template-columns: 260px minmax(0, 1fr); min-height: 100dvh; }}
    .sidebar {{
      position: sticky; top: 0; height: 100dvh;
      background: var(--sidebar); color: #e8f3ee;
      padding: 1.25rem 1rem; overflow: auto;
    }}
    .brand {{
      font-family: var(--font-display); font-size: 1.6rem; color: #fff;
      text-decoration: none; font-weight: 700;
    }}
    .sidebar-sub {{ margin: 0.15rem 0 1rem; color: #9cb8af; font-size: 0.85rem; }}
    .search-wrap {{ display: block; font-size: 0.72rem; letter-spacing: 0.04em; text-transform: uppercase; color: #9cb8af; margin-bottom: 0.35rem; }}
    #db-search {{
      width: 100%; padding: 0.65rem 0.75rem; border-radius: 8px; border: 1px solid rgba(255,255,255,0.12);
      background: rgba(255,255,255,0.06); color: #fff; font: inherit; margin-bottom: 1rem;
    }}
    #table-nav {{ display: grid; gap: 0.25rem; }}
    .nav-item {{
      display: flex; justify-content: space-between; gap: 0.5rem;
      color: #d5e8e0; text-decoration: none; padding: 0.45rem 0.55rem; border-radius: 8px; font-size: 0.9rem;
    }}
    .nav-item:hover, .nav-item.active {{ background: rgba(232, 93, 4, 0.25); color: #fff; }}
    .nav-count {{ color: #9cb8af; font-size: 0.75rem; }}
    .sidebar-meta {{ margin-top: 1.5rem; font-size: 0.72rem; color: #9cb8af; line-height: 1.5; }}
    main {{ padding: 1.5rem 1.75rem 3rem; }}
    h1, h2, h3 {{ font-family: var(--font-display); letter-spacing: -0.02em; }}
    h1 {{ margin: 0 0 0.4rem; font-size: 2.1rem; }}
    h2 {{ margin: 0 0 0.35rem; }}
    .lede {{ color: var(--ink-soft); max-width: 46rem; }}
    .meta {{ font-size: 0.8rem; color: var(--ink-soft); }}
    .overview, .table-card {{
      background: rgba(255,255,255,0.84); border: 1px solid var(--line);
      border-radius: var(--radius); padding: 1.1rem 1.2rem; margin: 1rem 0 1.25rem;
    }}
    .er-list, .ref-grid ul {{ margin: 0.4rem 0 0; padding-left: 1.15rem; }}
    .table-wrap {{ overflow-x: auto; margin: 0.75rem 0 1rem; }}
    table {{ width: 100%; border-collapse: collapse; font-size: 0.88rem; }}
    th, td {{ text-align: left; vertical-align: top; padding: 0.55rem 0.65rem; border-bottom: 1px solid var(--line); }}
    th {{ font-size: 0.72rem; text-transform: uppercase; letter-spacing: 0.04em; color: var(--ink-soft); background: #e8f3ee; }}
    code {{ font-size: 0.84em; }}
    .muted {{ color: var(--ink-soft); }}
    .ref-link {{ color: var(--accent-deep); }}
    .badge {{
      display: inline-block; font-size: 0.68rem; font-weight: 700; letter-spacing: 0.03em;
      text-transform: uppercase; padding: 0.12rem 0.4rem; border-radius: 999px; margin: 0.1rem 0.15rem 0.1rem 0;
    }}
    .badge-pk {{ background: #10231f; color: #fff; }}
    .badge-unique {{ background: #c44900; color: #fff; }}
    .badge-idx {{ background: #dceee7; color: var(--good); }}
    .badge-req {{ background: #fde8d8; color: var(--accent-deep); }}
    .badge-null {{ background: #eef2f1; color: var(--ink-soft); }}
    .enum {{ font-size: 0.75rem; color: var(--ink-soft); }}
    .ref-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 1rem; }}
    .empty {{ padding: 1rem; border: 1px dashed var(--line); border-radius: var(--radius); text-align: center; color: var(--ink-soft); }}
    .hidden {{ display: none; }}
    tr.col-hit td {{ background: rgba(232, 93, 4, 0.08); }}
    @media (max-width: 840px) {{
      .shell {{ grid-template-columns: 1fr; }}
      .sidebar {{ position: relative; height: auto; }}
    }}
  </style>
  <script>
    const search = document.getElementById("db-search");
    const cards = [...document.querySelectorAll(".table-card")];
    const nav = [...document.querySelectorAll(".nav-item")];
    const empty = document.getElementById("no-results");
    const overview = document.querySelector(".overview");

    function filterWiki() {{
      const q = (search.value || "").trim().toLowerCase();
      let visible = 0;
      cards.forEach((card) => {{
        const tableName = (card.dataset.table || "").toLowerCase();
        const tableHit = !q || tableName.includes(q);
        const rows = [...card.querySelectorAll("tbody tr")];
        let anyRowHit = tableHit;
        rows.forEach((row) => {{
          const rowHit = !q || tableHit || (row.dataset.search || "").includes(q);
          row.classList.toggle("hidden", Boolean(q && !rowHit));
          row.classList.toggle("col-hit", Boolean(q && (row.dataset.search || "").includes(q)));
          if (rowHit) anyRowHit = true;
        }});
        card.classList.toggle("hidden", Boolean(q && !anyRowHit));
        const navItem = nav.find((n) => n.dataset.table === card.dataset.table);
        if (navItem) navItem.classList.toggle("hidden", Boolean(q && !anyRowHit));
        if (!card.classList.contains("hidden")) visible += 1;
      }});
      if (overview) overview.classList.toggle("hidden", Boolean(q));
      empty.classList.toggle("hidden", visible !== 0);
    }}
    search.addEventListener("input", filterWiki);
  </script>
</body>
</html>
"""


def refresh_dbwiki(*, force: bool = False) -> Path | None:
    """Rewrite dbwiki.html when the live schema fingerprint changes."""
    from backend.database import engine

    schema = _collect_schema(engine)
    existing = _read_existing_fingerprint(WIKI_PATH)
    if not force and existing == schema["fingerprint"] and WIKI_PATH.exists():
        return None
    WIKI_PATH.write_text(_render_html(schema), encoding="utf-8")
    return WIKI_PATH


def generate_dbwiki() -> Path:
    path = refresh_dbwiki(force=True)
    assert path is not None
    return path
