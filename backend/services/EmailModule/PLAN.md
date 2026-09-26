"""Email intelligence plan (Mark)

Keep lead discovery as-is. EmailModule becomes the only place that decides
an address. Search still finds people; this layer fills email *after*, with
status so inferred is never shown as real.

Waterfall (stop on strong result, skip providers without keys):

1. Cache / existing lead_records for same person+company
2. Snippet / already-known public evidence
3. Company domain (CompanyModule)
4. Public web (DDG snippets + robots-respecting pages)
5. Domain pattern (INFERRED only; needs evidence)
6. Configured providers in EMAIL_PROVIDER order
7. Verify (syntax, MX, disposable, provider)
8. Persist evidence, pattern, usage

Search-time: snippet + cache + strong patterns only. No paid APIs.
"Find Emails": full waterfall in a background thread (same style as search).

SQLite: create_all + ALTER, matching existing init_db. No new framework.
"""
