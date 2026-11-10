# Discovery engine

This document is the audit, the target design, and the acceptance checklist for a persistent B2B discovery cache. Phase 1 is the only phase implemented. Later phases are specified so they are not redesigned from scratch, and they are not built yet.

## Audit (current system)

Mark is a React 19 + Vite frontend and a FastAPI + SQLAlchemy backend. SQLite is the local database. Postgres is the production target via `DATABASE_URL`. There is no Alembic project. `init_db()` calls `create_all`, then adds missing columns and indexes, then runs versioned steps in `backend/migrations.py`. `create_all` does not alter existing tables, so the column and version steps are what an already-populated database needs.

### What a customer search does today

1. `POST /api/searches` stores a `SearchJob` owned by the user (and optionally an organization) and queues `search_discovery`.
2. The worker runs `run_discovery`: public web queries, `SearchCandidate` rows, qualification, then `LeadRecord` rows.
3. Unless email lookup is set to `selected`, a second queue job runs enrichment (public email and phone lookup, optional paid providers when keys exist).
4. The UI polls `GET /api/search-jobs/{id}` and shows one progress story: discover, qualify, find emails.

Search providers are the multi-engine public web dispatcher (`backend/services/search_engines`). Email finding and verification live in `backend/services/EmailModule` (public pages plus Hunter, Findymail, LeadMagic, Prospeo, Tomba when configured). Company lookup is `CompanyModule`. None of these are replaced by the cache.

### What is already shared, and what is private

| Store | Shared? | Holds |
| --- | --- | --- |
| `search_query_cache` | Yes, by query text only | Raw public SERP hits. No owner. |
| `email_lookup_cache` | Yes, by person key + domain | Email-module results. Not a prospect directory. |
| `domain_email_patterns` | Yes | Learned address patterns. |
| `lead_records` | No | One search's people, plus notes, tags, CRM stage, assignment. |
| `search_candidates` | No | Raw hits for one search. |
| `crm_contacts`, notes, tasks, activities | No | Explicit CRM. Discovery does not fill it. |
| `provider_usage` | Accounting | Calls, not people. |
| `job_queue` | Process | Discovery and enrichment for a user search. Not a standing crawl. |

Identity dedup today is per search: normalized profile URL, or a hash of name + company + title. A name alone is never a key (`backend/services/identity.py`).

Authorization is session JWT plus `owner_id` / organization membership. There is no platform-admin role separate from organization admin. Cache reads require a logged-in user. There is no public write API for the cache.

### Security gaps (not closed by Phase 1)

- The inline worker still runs inside the web process when `MARK_INLINE_WORKER` is on. That is not a multi-instance scheduler.
- Paid providers can be called from a user search when keys exist. Phase 1 does not add budget reservation. Unknown cost is not yet a hard stop.
- LinkedIn session cookies are stored encrypted and are not used by public search.
- No suppression list, retention job, or GDPR deletion workflow. Do not describe the product as legally compliant.
- No admin RBAC for cache correction, export, or provider credentials.
- External fetches used by existing email/company modules need to stay on their current robots and URL checks. Phase 1 does not add a crawler.

## Boundary

`global_people` and `global_companies` are the shared cache. `lead_records` stays the customer's copy for a search. Notes, tags, CRM stage, assignments, tasks, and provider credentials never move into the cache.

`refuse_workspace_lead()` always raises. A migration of an existing database creates the cache tables and leaves them empty. Anna Lind's old row stays on `lead_records`.

Only `share_scope=public` evidence is accepted. A phone number is stored only when the caller marks it as evidenced. Unknown industry, employee count, and seniority stay null. Email statuses stay distinct: `found`, `verified`, `invalid`, `accept_all`, `unknown`, `not_found`, `inferred`, plus the existing `unverified` and `verify_uncertain`. Inferred is not verified. `accept_all` is not verified unless a policy says so. The default policy does not.

Quality tiers (`basic`, `enriched`, `contactable`, `verified`) come from `cache_policies`. They rank records. They are not a promise that an address is deliverable.

## Phase 1 behavior

- Normalize names, titles, companies, domains, profile URLs, country codes, and technology labels.
- Dedup by profile URL, email, provider id, and name+domain. Never by name alone.
- If a second record has a different name or a different profile URL, it is not merged into the first. The conflicting email is kept off the new row and stored as ineligible evidence.
- Companies with the same domain are one row. Two public names for one domain are not blended.
- `GET /api/cache/people` filters, sorts, and pages in SQL. It requires authentication. It does not return another workspace's notes.
- A customer search checks the cache first. Enough matches are copied onto that search and live search is skipped. A short cache still runs the existing live search, and new hits are deduped against the copy. Progress and the email-lookup choice are unchanged.
- Technologies such as Postgres are stored as labels. Nothing connects to those products.

## Migration

`upgrade_database()` is the supported entry point (also used by `init_db()`):

1. `create_all` for missing tables.
2. Normalize legacy status strings.
3. `ALTER TABLE` for columns added after a table already existed, including `search_jobs.cache_served`.
4. Create missing indexes.
5. Apply `schema_migrations` version 1, which ensures the cache tables and the default policy row.

The step does not read `lead_records`. Running it twice does not duplicate people.

## Later phases (not implemented)

### Phase 2 — scheduler and worker

A database-backed schedule and queue that does not depend on a browser session or an in-memory timer. One lock so two app instances do not run the same tick. Leases, crash recovery, bounded concurrency, exponential backoff with jitter, and idempotency keys. Pipeline stages: discover, normalize, dedup, validate, associate company, optional enrichment, existing email find/verify, cache write, evidence, analytics. Each stage can be retried on its own. Pause keeps queued work. Cancel is a different action from pause and resume.

### Phase 3 — admin screens

Real data only: Overview, Cached leads, Cached companies, Jobs, and start/pause/resume/run-now. Later navigation: Overview, Discovery Engine, Targeting Configurations, Field Priorities, Cached Leads, Cached Companies, Discovery Jobs, Provider Management, Usage and Costs, Cache Health, System Settings. Organization admin is not the same as a platform operator. Add that role before these screens.

### Phase 4 — targeting

Saved target configs, each with priority, schedule, and limits. Include and exclude geography, industry, subindustry, titles, synonyms, exclusions, function, seniority, department, employee min/max, company type, and optional technology labels (AWS, Azure, GCP, Salesforce, HubSpot, Shopify, React, Angular, Kubernetes, Docker, Postgres, Mongo are values, not integrations). Required, preferred, and optional fields. A missing optional field does not drop a good prospect. Schedules: daily, hourly, weekdays, timezone, interval. No hardcoded industry service.

Initial defaults, when that phase is built: United States (states and territories the providers actually support) and these countries individually, not as one EU blob: GB, DE, FR, NL, IE, SE, NO, DK, FI, CH, AT, BE, ES, IT, PT, PL, CZ, EE, LT, LV, RO. Industries: Software, SaaS, AI/ML, cloud, cybersecurity, FinTech, HealthTech, EdTech, analytics, IT services, dev tools, enterprise software, internet/ecommerce technology, robotics, telecom. Functions: engineering, management, IT, product, data/AI, security, DevOps/cloud, sales/BD, marketing, operations. Titles such as CTO, CIO, CEO, founder, VP, director, manager, engineer, developer, product, head of IT, and head of sales are editable, with synonyms and exclusions.

### Phase 5 — enrichment budgets

Reuse the current email and company providers. Skip fresh fields. Order and enable providers explicitly. Reserve daily, monthly, global, and per-provider budget atomically before a paid call. Record requests, successes, failures, rate limits, credits, and estimated cost. A missing price fails closed. Credentials and a spending limit are both required. Metrics come from the database: people, companies, today, week, email status, country, industry, function, seniority, provider, stale rows, prevented duplicates, jobs, and cost.

### Phase 6 — production readiness

Tenant and auth tests that try to cross workspaces. Performance of cache match. Restart recovery. Health, logs, and metrics. A worker process that is not the web server's background task. Admin browse with server-side filter, sort, page, and export. Evidence and dedup history. Archive or correct instead of bulk delete. Suppression at ingest and at serve. Retention and deletion. Secret handling. SSRF-safe fetches and robots rules. No CAPTCHA or login bypass. No claim of automatic GDPR compliance.

## Acceptance checklist

### Phase 1

- [x] Shared people and companies are separate from `lead_records` and CRM.
- [x] Existing private leads are not backfilled.
- [x] Public upsert refuses a non-public share scope and refuses workspace leads.
- [x] Nulls stay null. Phone without evidence is dropped.
- [x] Inferred email is not labeled verified or contactable under the default policy.
- [x] Verified email with an identity is the verified tier.
- [x] Same domain reuses one company. Conflicting name or profile is not merged.
- [x] Repeat upsert of the same profile is one row.
- [x] Match and pagination run in SQL, including technology.
- [x] Authenticated cache read. Anonymous request is rejected.
- [x] A covered search does not call live search. A short cache still does.
- [x] Copied search leads do not gain CRM notes.
- [x] Versioned migration runs on a legacy populated database and is idempotent.
- [ ] Multi-threaded upsert under Postgres load. SQLite coverage is the single-process retry path.
- [ ] Admin UI, scheduler, budgets, and suppression. Those are later phases.

### Phase 2

- [ ] Schedule rows survive a process restart.
- [ ] Two workers cannot claim the same tick.
- [ ] Pause does not delete queued jobs. Cancel does not pretend to be pause.
- [ ] A failed stage retries with backoff and does not rerun completed stages.

### Phase 3

- [ ] Admin pages read persisted counts and job rows.
- [ ] Start, pause, resume, and run-now call real APIs.
- [ ] A non-admin cannot open them.

### Phase 4

- [ ] Several saved targets, each with its own schedule and limits.
- [ ] Include and exclude rules, synonyms, and optional technologies.
- [ ] Optional blanks do not discard a prospect that meets required fields.

### Phase 5

- [ ] Paid calls do not run without a key and a spending limit.
- [ ] Unknown price fails closed.
- [ ] Usage rows explain requests, failures, rate limits, and cost.

### Phase 6

- [ ] Cross-workspace tests fail closed.
- [ ] Worker restart resumes leases.
- [ ] Cache browse is filtered in SQL.
- [ ] Suppression applies when writing and when serving.
- [ ] Deployment docs describe a separate worker, not only the web process.
