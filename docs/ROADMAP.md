# Mark — Product roadmap

Pick **one task at a time**. Check boxes as you ship. Do not skip **Step 1** — later steps depend on async jobs.

**How to use this doc**

1. Choose the next unchecked task in order (or the one marked `optional`).
2. Create a Git branch: `feat/roadmap-1.3-pagination` (match task id).
3. Ship it, check the box, add a one-line note under **Changelog** at the bottom.
4. Optional: open a GitHub Issue from the task title and close it when done.

**Status legend:** `[ ]` todo · `[~]` in progress · `[x]` done · `[—]` optional / later

**Estimate:** 1 full-time dev. `#` = task id for branches/issues.

---

## Already shipped

- [x] Auth (individual + organization)
- [x] Prompt search + filters
- [x] Save searches + leads (Postgres)
- [x] CSV / XML export
- [x] AI summaries (OpenAI optional)
- [x] Landing page + splash
- [x] Deploy (Railway + Netlify)

---

## Step 1 — Foundation (1–2 weeks)

*Everything else needs async search and stable data layer.*

| ID | Task | Deps |
|----|------|------|
| 1.1 | [x] Add `status` progress to `SearchJob` (`pending`, `running`, `completed`, `failed`) | — |
| 1.2 | [ ] Background worker (RQ/Celery or Railway worker process) | 1.1 |
| 1.3 | [ ] API: `POST /searches` returns job id immediately; poll `GET /searches/{id}` | 1.2 |
| 1.4 | [ ] Frontend: progress UI (spinner + “Searching… X leads”) | 1.3 |
| 1.5 | [ ] Pagination: `GET /searches/{id}/leads?page=&limit=` | 1.1 |
| 1.6 | [ ] DB indexes: `search_jobs(owner_id, created_at)`, `lead_records(search_job_id)`, `lead_records(relevance_score)` | — |
| 1.7 | [ ] `global_leads` table keyed by `linkedin_url` + merge on save | 1.5 |
| 1.8 | [ ] Move filter definitions to `config/filters.json` + loader | — |

**Step 1 done when:** User runs a search, sees progress, gets 100+ leads without timeout.

---

## Step 2 — Better discovery (2–3 weeks)

| ID | Task | Deps |
|----|------|------|
| 2.1 | [ ] LLM multi-query generator (5–8 queries per prompt) | 1.2 |
| 2.2 | [ ] Run queries in parallel in worker | 2.1 |
| 2.3 | [ ] Search provider interface (`SearchProvider` protocol) | — |
| 2.4 | [ ] Provider: DuckDuckGo (current) | 2.3 |
| 2.5 | [ ] Provider: Bing or SerpAPI fallback | 2.3, 2.4 |
| 2.6 | [ ] Redis query cache (24h TTL) — Upstash free tier | 1.2 |
| 2.7 | [ ] Dedup leads across searches (same `linkedin_url` for user) | 1.7 |
| 2.8 | [ ] Raise per-search cap to 100–500 (plan-aware later) | 1.2 |

**Step 2 done when:** Repeat/n similar searches are faster; fewer empty results.

---

## Step 3 — Lead library UI (2–3 weeks)

| ID | Task | Deps |
|----|------|------|
| 3.1 | [ ] Add React Router (`/`, `/login`, `/app`, `/app/leads`) | — |
| 3.2 | [ ] Leads table: sort, column filters, pagination | 1.5 |
| 3.3 | [ ] “All my leads” view (`GET /api/leads?…`) | 1.7, 2.7 |
| 3.4 | [ ] Lead detail drawer (full fields + export one) | 3.2 |
| 3.5 | [ ] Saved filter presets (CRUD + apply on search form) | 1.8 |
| 3.6 | [ ] Revamp app shell UI to match landing page | 3.1 |

**Step 3 done when:** App feels like a spreadsheet-style lead tool.

---

## Step 4 — Enrichment v1 (2–4 weeks)

| ID | Task | Deps |
|----|------|------|
| 4.1 | [ ] Second-pass open-web enrich (company domain, public snippets) | 1.2 |
| 4.2 | [ ] Email confidence: `found` / `guessed` / `unknown` on each lead | — |
| 4.3 | [ ] Integrate one email API (Hunter.io or similar) | 4.2 |
| 4.4 | [ ] Bulk enrich selected rows (async job) | 1.2, 3.2 |
| 4.5 | [ ] `first_seen_at` / `last_seen_at` on global leads | 1.7 |
| 4.6 | [ ] Show “stale” badge if not seen in 90+ days | 4.5 |

**Step 4 done when:** Users see verified vs guessed email clearly.

---

## Step 5 — Import, export, activate (2–3 weeks)

| ID | Task | Deps |
|----|------|------|
| 5.1 | [ ] CSV import → enrich pipeline | 1.2, 4.1 |
| 5.2 | [ ] JSON export | — |
| 5.3 | [ ] Streamed CSV export for large lists | 1.5 |
| 5.4 | [ ] Webhooks: `search.completed`, `leads.enriched` | 1.3 |
| 5.5 | [ ] One CRM: HubSpot **or** Zapier (pick one first) | 5.4 |
| 5.6 | [ ] Privacy/Terms/Refund pages (required for payments) | — |

**Step 5 done when:** Users can import a list and push results outward.

---

## Step 6 — Teams & billing (3–4 weeks)

| ID | Task | Deps |
|----|------|------|
| 6.1 | [ ] Org-shared searches (query by org membership, not only `owner_id`) | — |
| 6.2 | [ ] Org-shared lead library + permissions | 3.3, 6.1 |
| 6.3 | [ ] Plans table + credits / usage counters | — |
| 6.4 | [ ] Payments (Paddle or Lemon Squeezy recommended for PK) | 6.3, 5.6 |
| 6.5 | [ ] Enforce limits per plan (searches/month, leads/search) | 6.3, 2.8 |
| 6.6 | [ ] Account delete + export my data (GDPR-lite) | — |

**Step 6 done when:** You can charge $29–99/mo with team seats.

---

## Step 7 — Power features (3–5 weeks)

| ID | Task | Deps |
|----|------|------|
| 7.1 | [ ] Waterfall enrichment (email: provider A → B → C) | 4.3 |
| 7.2 | [ ] Public REST API + API keys for users | 1.3 |
| 7.3 | [ ] Scheduled searches (weekly re-run, new leads only) | 1.2, 2.7 |
| 7.4 | [ ] Lookalike search from 3 starred leads | 2.1, 3.3 |
| 7.5 | [ ] Query analytics (which queries yield best exports) | 2.6 |

**Step 7 done when:** “AirScale / Scrupp lite” for prompt-led discovery.

---

## Step 8 — Optional / competitor parity (pick if users pay for it)

*Not required for a viable SaaS. High cost or legal risk.*

| ID | Task | Notes |
|----|------|-------|
| 8.1 | [—] Chrome extension (public LinkedIn pages) | ToS review |
| 8.2 | [—] Sales Navigator URL export | Needs user session |
| 8.3 | [—] AI icebreakers per lead | Builds on 4.x |
| 8.4 | [—] AI web research agent | Airsearch-style |
| 8.5 | [—] Company lookalikes | Extra data source |
| 8.6 | [—] Companies hiring signal | Extra data source |
| 8.7 | [—] Google Maps local businesses | AirScale source |
| 8.8 | [—] 10+ CRM/sequencer native integrations | Maintenance heavy |

---

## What this roadmap does **not** promise

| Competitor strength | Covered? |
|--------------------|----------|
| Sales Nav 2,500 scrape (Scrupp) | No — Step 8.2 optional |
| 250M contact DB (Apollo/ZoomInfo) | No — different product |
| 15 providers / 450 fields (AirScale) | Partial — Step 7.1 only |
| Visual workflow builder (Clay) | No |
| Built-in email sequences | No — use HubSpot/Zapier |

See [COMPETITOR-GAP.md](./COMPETITOR-GAP.md) for full comparison.

---

## Suggested “next task”

If nothing is checked in Step 1 yet, start with **1.1**.

---

## Changelog

| Date | Task | Note |
|------|------|------|
| 2026-09-12 | — | Roadmap created |
| 2026-09-12 | 1.1 | SearchJobStatus enum + lifecycle in search_service |
