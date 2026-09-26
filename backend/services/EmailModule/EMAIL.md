# Email intelligence (EmailModule)

Mark still finds people first. Emails are a separate evidence waterfall.

## Configure a provider

Put a key in the environment (see `.env.example`). Only keys that exist are called.

```
EMAIL_PROVIDER=hunter,tomba
HUNTER_API_KEY=...
```

Order is the waterfall. No key → that provider is skipped. No keys at all → public web + patterns only.

## Waterfall

1. Cache / same person already in `lead_records`
2. Snippet text
3. Company domain
4. Public web (robots.txt respected, no LinkedIn login)
5. Domain pattern → **INFERRED** (never labeled found)
6. Providers in `EMAIL_PROVIDER` order; stop on first email
7. Verify (syntax, MX, disposable, then provider verify)
8. Persist evidence + usage

Search-time uses steps 2–5 only (no paid APIs). **Find emails** runs the full waterfall in a background thread.

## Status

| Status | Meaning |
| --- | --- |
| found | Exact address on a public page/snippet |
| inferred | Generated from a company pattern |
| verified | A provider said valid |
| unverified | Candidate exists, verify unknown |
| verify_uncertain | Catch-all / accept-all domain |
| not_found | Nothing reliable |

Inferred is never shown as verified.

## Confidence

HIGH: public found or provider-verified. MEDIUM: unverified provider or 2+ pattern samples. LOW: one-example pattern or catch-all.

## API

- `POST /api/searches/{id}/emails` — bulk Find Emails
- `GET /api/searches/{id}/emails` — poll job + leads
- `POST /api/leads/{id}/email` — one lead

Example bulk response includes `email_job` plus each lead’s `email`, `email_status`, `email_confidence`, `email_source`, `email_evidence`.

## Tests

```
.venv/bin/pip install pytest
.venv/bin/pytest tests/test_email_intelligence.py -q
```

Providers are mocked. No live API calls.

## Schema

`init_db()` `create_all` + `ALTER` on `lead_records` (no Alembic). New tables: `domain_email_patterns`, `email_evidence`, `email_verification_events`, `provider_usage`, `email_lookup_cache`, `email_enrichment_jobs`.

## Limits

Public web rarely lists personal work emails. Without a provider key, most rows stay NOT_FOUND or INFERRED. Cost per verified email is `provider_usage` credits / verified count — prices are not hard-coded.
