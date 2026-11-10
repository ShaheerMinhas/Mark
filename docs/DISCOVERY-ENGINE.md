# Discovery engine

This document is the audit, the target design, and the acceptance checklist for a persistent B2B discovery cache. Phases 1 through 6 are implemented with the gaps named in the later sections. Autonomous search and paid global calls stay off until an operator opens the gates below.

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

Implemented as a separate process, `python -m backend.discovery_worker`. The API process does not start it. Customer searches still use `python -m backend.worker`.

What runs: due **active** targets become `discovery_runs` plus a `discovery_pipeline` queue row. The worker claims those rows with the existing lease and heartbeat. Stages are recorded. With the defaults, every stage is a recorded skip. Live public search is a hard-off constant (`LIVE_PUBLIC_SEARCH_ENABLED`). Paid enrich and email stages always skip because there is no budget ledger.

What does not run: no crawl, no paid provider, no admin UI. A platform admin can store targets and suppression rules. New targets are created **paused**. Start or resume is what makes them eligible. Pause and stop scheduling do not delete queued runs. Cancel marks one run cancelled and leaves the queue row in place.

Schedules: `interval` (at least 15 minutes), `daily` at an hour and minute, and `weekdays` (Monday–Friday) in an IANA timezone. Stored instants are naive UTC. SQLite claims are compare-and-set. Postgres queue claims use `FOR UPDATE SKIP LOCKED`. The planner lock is a lease row, not SKIP LOCKED. Do not treat SQLite as safe for many discovery workers at once.

Operators are not created by registration. Grant one with `python -m backend.platform_admin grant person@example.com` after the account exists. Workspace owners are not platform admins.

Suppression is checked on cache write and on cache serve. Deleting a workspace lead does not delete or alter the shared cache. To stop a public identity from being stored or served, a platform admin adds a suppression row. That is the retention hook until a later deletion workflow exists.

### Phase 3 — admin screens

Implemented for a platform operator only: Overview, Discovery engine, Cached leads, Cached companies, and Discovery jobs. Counts and groupings are SQL aggregates. Estimated cost is `unavailable`. Worker state is the planner lease, or `unknown` when no worker has written one. Ordinary users and workspace owners get 403. The customer navigation does not show the console unless `is_platform_admin` is true, and the APIs still reject everyone else.

Archive sets `archived_at` and keeps evidence. Corrections keep the previous value as evidence. CSV export is admin-only and prefixes formula characters. Provider management, usage costs, and cache health are not pages.

Scheduling can be saved as enabled. A target that is not autonomous still records a skipped discover stage. Pause does not delete queued runs. Paid calls remain off until their own gates pass.

### Phase 4 — targeting

Implemented. Each target stores structured criteria: ISO country include and exclude, state and city include or exclude, industry and subindustry include and exclude, title exact values, synonyms, and exclusions, function, seniority, department, employee min and max, size, company type, and technology labels. Technology names are catalogue values. They do not connect a cloud or a plugin. Title matching is normalized equality against the exact list plus synonyms. An excluded title is removed by equality, not by a substring search.

A paused preset named “US and Europe technology leaders” is seeded with the United States and GB, DE, FR, NL, IE, SE, NO, DK, FI, CH, AT, BE, ES, IT, PT, PL, CZ, EE, LT, LV, RO as separate countries, plus the technology industry, function, and title labels from the brief. It has no next run and it does not create people. Discovery stays off.

Field rules store collection, required / preferred / optional, priority, refresh hours, and a verification flag. Required fields decide eligibility. Preferred fields change the stricter count only. A missing optional field, including a person with no technology rows when technology is optional, stays eligible. People who do not match are left in the cache. Unknown values stay null. An inferred email is not marked verified or contactable. Evidence, identity keys, and suppression still apply when collection for a field is off. Enrichment source is not applied; the API reports it as unavailable.

The validate stage counts eligible and preferred-complete cached people with the same SQL. It does not call a provider and it does not delete rows. Customer cache search is unchanged.

### Phase 5 — enrichment budgets

Implemented for global jobs only. Customer searches still use their existing environment keys and do not open a global reservation.

Gates, all required before a call: the paid switch is on (default off), the email or verify channel is not paused (default paused), the provider is enabled, a platform credential exists, an approved maximum price is at least 1 minor unit, a rate cap is configured and not exhausted, and global daily, global monthly, provider daily, and provider monthly budgets can reserve that maximum. The reservation is a conditional SQL update. The same idempotency key does not reserve twice. A retry uses a new attempt and a new reservation. A failed or crashed call keeps the maximum when the final charge is unknown. A known actual at or below the maximum settles that amount and releases the rest. An actual above the maximum is not recorded as known and the reserved maximum stays held. Credits are stored as credits and are not converted to money. Empty ledgers are null, not zero.

Existing Hunter, Tomba, LeadMagic, Prospeo, and Findymail classes can be constructed with the stored platform key. Tests replace that call with a fixture. Company research has no adapter and the enrich stage reports `unavailable`. Live public search stays off. Fresh email fields are skipped. Suppression is checked before a call. An inferred result is not marked verified or contactable. Field freshness stores source, enriched, verified, and next-due timestamps from the field-priority refresh hours.

What this checkpoint does not prove: Postgres multi-worker reservation locking under load, DNS-rebinding protection for outbound URLs, a real provider invoice, or a currency conversion. SQLite coverage is two connections racing one conditional update. Current adapters do not return a currency amount, so a real call stays an unknown charge at the reserved maximum.

### Phase 6 — production hardening

Local PostgreSQL 16 was used for migration and race tests. No production database was contacted.

What now works: a fresh Postgres database and a legacy database that already has a private lead both upgrade without copying that lead. Two Postgres sessions claim two queue rows with `FOR UPDATE SKIP LOCKED`. Two sessions cannot both reserve the last budget unit, and repeating the winning reservation key does not reserve again. Two sessions upserting the same profile end as one person; a conflicting identity key rolls back and retries instead of leaving an unkeyed row. A worker heartbeat is a separate table from the planner lease. `GET /api/health` reports the database and that heartbeat. Retention archives a shared person and suppresses the email, profile, and domain. It does not delete workspace leads and it is not a GDPR deletion. Customer routes stay on the owner. Operator cost, credentials, and export are rejected for ordinary users and workspace owners. New outbound fetches go through an address and redirect check. Logger lines redact secret-shaped values. A credential that cannot be decrypted is not used.

There is no separate System Settings navigation. The switches that exist are on Providers: paid calls, the three pauses, public-search intent, and retention days. Phone numbers still require evidence. Unknown provider charges stay reserved at the maximum. Admin list, sort, export, correction, and archive were already backed by the database; export stays capped at 100 rows.

An active autonomous technology target, with scheduling enabled, asks `html.duckduckgo.com` for one varied query every 120 minutes through the pinned fetch guard. A fresh cache does not skip that run. It does not open the result pages and it does not invent an email or a phone. The browser does not run that loop. `python -m backend.discovery_worker` does, and only after an operator starts the target. Saving the target keeps it paused. The interactive customer search can still use the older multi-engine client, which is not DNS-pinned. That client is not the autonomous path.

Replacement looks in the cache first. The original lead, its notes, and its CRM stay. When scheduling is enabled and an autonomous target is active, one pinned query can run if the cache has nobody else eligible. Otherwise the answer is exhausted. A profile learned later for the same person keeps the recipient count already recorded. The customer is the organization on the search when there is one, otherwise the individual account.

A shared identity can be given to three distinct customers by default, or two if an operator lowers the limit. The customer is the organization on the search when there is one, otherwise the individual account. Teammates and repeat searches do not add a recipient. After the limit, new customers do not receive that identity. Customers who already have a copy keep it, including notes and CRM. Replacing a lead adds another person and keeps the original. Lowering the limit does not delete anyone. Raising it requires confirmation and does not recycle an identity.

What still does not discover on the network during development: nothing is activated by default. The interactive multi-engine customer search still uses the existing search client, which is not DNS-pinned. Company research remains unavailable. Paid provider HTTP and invoices are not part of this checkpoint. A fixture source can pass people through normalize, dedup, company association, evidence, and a customer search. The suite passing is not a production-ready claim.

A 2,000-row country and title count on the local SQLite file is bounded by the test at under 5 seconds. That is a regression guard, not a throughput number.

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
- [x] Two Postgres sessions upserting one profile finish as one person. This is not a load test.
- [x] Admin UI, scheduler, budgets, and suppression exist. See the later checklists for what is still blocked.

### Phase 2

- [x] Schedule and run rows survive a new database session.
- [x] A second planner tick does not create a second run for the same slot.
- [x] Two claimers cannot hold the same queue row. An expired lease can be claimed again.
- [x] Pause and stop scheduling leave queued runs in place. Cancel marks the run and does not delete the queue row.
- [x] Retry delay uses capped exponential backoff and explicit jitter. The last attempt is not requeued.
- [x] Customers and workspace owners receive 403. A platform admin can store a paused target.
- [x] Suppression blocks ingest and serving.
- [x] A disabled run does not call public search or paid email providers.
- [x] Fresh cache rows are not refreshed.
- [x] Overview says "Email lookup queued" when discovery is finished and the enrichment job is only queued.
- [x] Paid enrichment has a budget ledger and stays off unless every gate passes. The legacy live-search flag stays off. An autonomous target uses the pinned HTML search only after an operator enables scheduling and starts the target.
- [x] Two Postgres sessions claim two queue rows with SKIP LOCKED. Multi-worker SQLite is still not a supported production lock.

### Phase 3

- [x] Overview, engine controls, cached leads, cached companies, and jobs read the database.
- [x] Start, pause, resume, stop scheduling, run-now, and cancel call the operator APIs.
- [x] A customer or workspace owner is rejected by the API.
- [x] Cost is reported as unavailable. Worker state is unknown without a persisted lease.
- [x] Archive and CSV export do not delete evidence. CSV formula characters are prefixed.
- [x] Targeting and field priorities are operator pages backed by the database.
- [x] Provider, usage, and cache-health pages read the database. Company research stays unavailable.

### Phase 4

- [x] Several saved targets, each with its own schedule, timezone, and limits. Invalid schedules and bounds are rejected.
- [x] Include and exclude rules, title synonyms and exclusions, and optional technology labels.
- [x] A missing optional field does not discard a prospect. A missing required field is ineligible and the row remains.
- [x] The paused US and Europe technology preset does not create people and is not due.
- [x] Ordinary users and workspace owners receive 403. Criteria survive a new database session.
- [x] Email enrichment source can be stored and is used only when the paid gates pass. Other fields stay unavailable.

### Phase 5

- [x] A paid call is refused before the adapter runs when the credential, price, rate cap, or budget is missing. A zero price is rejected.
- [x] Unknown and crashed charges keep the reserved maximum. A retry reserves again. The same key does not reserve twice.
- [x] Two SQLite connections cannot both reserve the last unit. Postgres concurrency is not tested here.
- [x] A fresh email field skips the call. Inferred is not verified or contactable. Suppression skips the call.
- [x] Customer enrichment does not create a global reservation. Workspace environment keys are not a global credential.
- [x] Usage separates estimated, actual, and unknown. An empty ledger is null, not zero. Cache health counts come from SQL.
- [ ] A real provider HTTP call and invoice reconciliation. Adapters still do not return a currency amount, so a real call would stay an unknown charge at the reserved maximum.
- [x] Two Postgres sessions cannot both reserve the last budget unit. This is not a production load test.

### Phase 6

These are the seventeen checks for this checkpoint.

- [x] 1. Fresh local Postgres migration applies every schema version and leaves the shared cache empty.
- [x] 2. Legacy local Postgres upgrade keeps the private lead and does not copy it into `global_people`.
- [x] 3. Two Postgres workers claim different queue rows with `FOR UPDATE SKIP LOCKED`.
- [x] 4. Postgres budget reservation is atomic, and the same idempotency key does not reserve twice.
- [x] 5. Postgres profile upsert race ends as one person. A conflicting identity key retries instead of saving an unkeyed row.
- [x] 6. Worker heartbeat is stored separately from the planner lease. Health reports `unknown` when no worker has written one.
- [x] 7. Queue lease recovery and cancel-without-delete remain covered. A stopping heartbeat is distinct from a stale one.
- [x] 8. A customer cannot read another customer's search, operator usage, provider configuration, or export.
- [x] 9. Cache and operator list endpoints reject a page size above 100.
- [x] 10. Retention archives and suppresses shared identities at the next ingest. Workspace leads stay. This is not GDPR compliance.
- [x] 11. An unreadable credential fails closed. Re-encryption with the wrong old key does not replace the stored secret.
- [x] 12. Log lines redact secret-shaped values.
- [x] 13. New fetches check scheme, port, userinfo, and every resolved address and redirect. Legacy search and public-email fetchers do not use this guard.
- [x] 14. An operator-started autonomous target fetches only `html.duckduckgo.com` through the pinned guard, and only while scheduling is enabled. Defaults stay off. The interactive multi-engine client is still not DNS-pinned.
- [x] 15. Fixture hits pass normalize, dedup, company, evidence, and into a customer search. A private source URL is dropped. A phone without evidence is dropped.
- [x] 16. Company research stays `unavailable`.
- [x] 17. Deployment notes describe separate web and worker processes, backup and restore, and the gates that stay off.
