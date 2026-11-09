"""Stage A + B orchestration.

Discovery walks the query plan, persisting candidates as each query returns and
recording each query's outcome. One failing query never fails the search: the job
finishes `completed` when enough queries succeeded, `partially_completed` when
some failed but a pool was still collected, and `failed` only when nothing
useful was gathered.

Enrichment is queued separately at the end and never blocks this stage.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime

from sqlalchemy.orm import Session

from backend.config import QUERY_PARALLELISM
from backend.models import (
    CandidateStatus,
    LeadRecord,
    QueryRunStatus,
    SearchCandidate,
    SearchJob,
    SearchJobStatus,
    SearchQueryRun,
)
from backend.observability import log_event
from backend.services import search_state
from backend.services.search_engines import SearchOutcome, SearchResponse, search_text
from backend.services.discovery import cache
from backend.services.discovery.extract import extract_candidates
from backend.services.discovery.promote import promote_qualified
from backend.services.discovery.qualify import qualify_candidates, rejection_breakdown
from backend.services.discovery.queries import QuerySpec, build_query_plan, second_page
from backend.services.discovery.store import CandidateWriter

logger = logging.getLogger(__name__)

#: Hits requested per query. A productive page earns a follow-up page.
#: 25 keeps a 2,000-lead search inside a few hundred queries instead of stalling
#: in the low hundreds after qualification.
RESULTS_PER_QUERY = 25
SECOND_PAGE_THRESHOLD = 8

#: Pages to walk for a query that keeps producing. Stopping at page 2 left a lot
#: of the index unread for the queries that were working best.
MAX_PAGES_PER_QUERY = 5

#: A follow-up page is only worth fetching if the previous one yielded this many
#: candidates that were not already in the pool.
NEW_CANDIDATE_THRESHOLD = 2

#: Consecutive rate-limit responses before discovery stops asking.
RATE_LIMIT_STREAK_LIMIT = 5

#: Attempts per query before giving up on it. Rate limits and timeouts are
#: transient — on live runs more than half the queries failed this way, and
#: abandoning them on the first error threw away most of the candidate pool.
MAX_QUERY_ATTEMPTS = 3

#: Only transient outcomes are worth retrying. A genuine error is not.
_RETRYABLE_OUTCOMES = frozenset({SearchOutcome.RATE_LIMITED, SearchOutcome.TIMEOUT})

#: Backoff before a retried query is re-issued, indexed by attempt number.
_RETRY_BACKOFF_SECONDS = (4.0, 10.0)

#: Roughly half of a candidate pool survives qualification, and a chunk of the
#: rest are duplicates, so the pool has to be several times the wanted lead count.
CANDIDATE_POOL_MULTIPLIER = 5

#: Floor on the pool regardless of ``max_results``, which is a display preference
#: rather than a statement about how widely to search. Sized so a default 1000-lead
#: target still has room after qualification and dedup.
MIN_CANDIDATE_POOL = 2500

#: Ceiling, so one search cannot run away with the worker.
MAX_CANDIDATE_POOL = 15000


def candidate_pool_target(filters: dict) -> int:
    """How many raw candidates discovery should gather for this search.

    ``max_results`` used to cap the pool directly, which quietly limited a search
    to ~50 qualified leads: the cap was reached long before the query plan was
    exhausted.
    """
    wanted = int(filters.get("max_results") or 1000)
    requested = int(filters.get("max_candidates") or 0)
    target = requested or max(MIN_CANDIDATE_POOL, wanted * CANDIDATE_POOL_MULTIPLIER)
    return max(1, min(MAX_CANDIDATE_POOL, target))


def _plan_rows(db: Session, job: SearchJob, specs: list[QuerySpec]) -> None:
    for spec in specs:
        db.add(
            SearchQueryRun(
                search_job_id=job.id,
                query_text=spec.text,
                query_hash=spec.hash,
                dimension=spec.dimension,
                page=spec.page,
                status=QueryRunStatus.PENDING.value,
            )
        )
    job.total_queries = len(specs)
    db.commit()


def _record_run(
    db: Session,
    job: SearchJob,
    spec: QuerySpec,
    *,
    status: str,
    hits: int = 0,
    candidates_found: int = 0,
    duplicates: int = 0,
    error_kind: str | None = None,
    duration_ms: int = 0,
    from_cache: bool = False,
) -> None:
    run = (
        db.query(SearchQueryRun)
        .filter(
            SearchQueryRun.search_job_id == job.id,
            SearchQueryRun.query_hash == spec.hash,
        )
        .first()
    )
    if run is None:
        run = SearchQueryRun(
            search_job_id=job.id,
            query_text=spec.text,
            query_hash=spec.hash,
            dimension=spec.dimension,
            page=spec.page,
        )
        db.add(run)
        job.total_queries = int(job.total_queries or 0) + 1
    run.status = status
    run.attempts = int(run.attempts or 0) + 1
    run.hits = hits
    run.candidates_found = candidates_found
    run.duplicates = duplicates
    run.error_kind = error_kind
    run.duration_ms = duration_ms
    run.from_cache = from_cache
    run.finished_at = datetime.utcnow()
    if run.started_at is None:
        run.started_at = run.finished_at
    db.commit()


def _issue_query(spec: QuerySpec, prior_attempts: int) -> tuple[QuerySpec, SearchResponse]:
    """Run one public search. Safe to call from a worker thread (no DB)."""
    if prior_attempts:
        backoff = _RETRY_BACKOFF_SECONDS[
            min(prior_attempts - 1, len(_RETRY_BACKOFF_SECONDS) - 1)
        ]
        time.sleep(backoff)
    return spec, search_text(spec.text, max_results=RESULTS_PER_QUERY, page=spec.page)


def _accept_hits(
    db: Session,
    job: SearchJob,
    writer: CandidateWriter,
    spec: QuerySpec,
    response: SearchResponse,
    *,
    from_cache: bool,
    queued_pages: set[str],
) -> QuerySpec | None:
    """Persist candidates for a successful query. Returns a follow-up page, if any."""
    extracted = extract_candidates(response.hits)
    inserted, duplicates = writer.add_many(extracted, query_text=spec.text)
    _record_run(
        db,
        job,
        spec,
        status=(
            QueryRunStatus.SUCCEEDED.value
            if response.hits
            else QueryRunStatus.EMPTY.value
        ),
        hits=len(response.hits),
        candidates_found=inserted,
        duplicates=duplicates,
        duration_ms=response.duration_ms,
        from_cache=from_cache,
    )
    search_state.bump(
        db,
        job,
        completed_queries=1,
        candidates_found=inserted,
        candidates_deduplicated=duplicates,
    )
    search_state.set_progress(
        db,
        job,
        progress=search_state.discovery_progress(job),
        message=f"{writer.known_count} candidates found",
    )
    log_event(
        logger,
        "query.done",
        trace_id=job.trace_id,
        search_id=job.id,
        dimension=spec.dimension,
        page=spec.page,
        hits=len(response.hits),
        candidates=inserted,
        duplicates=duplicates,
        duration_ms=response.duration_ms,
        from_cache=from_cache,
        engine=response.engine or None,
    )
    if (
        len(response.hits) >= SECOND_PAGE_THRESHOLD
        and inserted >= NEW_CANDIDATE_THRESHOLD
        and spec.page < MAX_PAGES_PER_QUERY
    ):
        follow_up = second_page(spec)
        if follow_up.hash not in queued_pages:
            queued_pages.add(follow_up.hash)
            return follow_up
    return None


def _accept_failure(
    db: Session,
    job: SearchJob,
    spec: QuerySpec,
    response: SearchResponse,
    *,
    used: int,
) -> bool:
    """Record a failed attempt. Returns True when the query should be retried."""
    retryable = response.outcome in _RETRYABLE_OUTCOMES and used < MAX_QUERY_ATTEMPTS
    _record_run(
        db,
        job,
        spec,
        status=(
            QueryRunStatus.RUNNING.value if retryable else QueryRunStatus.FAILED.value
        ),
        error_kind=response.outcome.value,
        duration_ms=response.duration_ms,
    )
    log_event(
        logger,
        "query.failed",
        level=logging.WARNING,
        trace_id=job.trace_id,
        search_id=job.id,
        dimension=spec.dimension,
        outcome=response.outcome.value,
        detail=response.detail,
        attempt=used,
        will_retry=retryable,
        engine=response.engine or None,
    )
    return retryable


def run_discovery(db: Session, job: SearchJob, *, final_attempt: bool = True) -> None:
    """Execute discovery and qualification for one SearchJob."""
    if search_state.is_terminal(job):
        log_event(
            logger,
            "discovery.skipped_terminal",
            trace_id=job.trace_id,
            search_id=job.id,
            status=job.status.value,
        )
        return

    started = time.monotonic()
    search_state.begin_stage(
        db,
        job,
        SearchJobStatus.DISCOVERING,
        "discovery",
        message="Searching public profiles…",
    )

    filters = dict(job.filters or {})
    max_candidates = candidate_pool_target(filters)
    specs = build_query_plan(job.prompt, filters)
    if not specs:
        search_state.finish(
            db,
            job,
            SearchJobStatus.FAILED,
            message="Could not build a search from that prompt. Try naming a role and a place.",
            error_message="empty_query_plan",
        )
        return

    _plan_rows(db, job, specs)
    log_event(
        logger,
        "discovery.plan",
        trace_id=job.trace_id,
        search_id=job.id,
        queries=len(specs),
        max_candidates=max_candidates,
    )

    writer = CandidateWriter(db, job.id)
    pending: list[QuerySpec] = list(specs)
    queued_pages: set[str] = set()
    rate_limit_streak = 0
    attempted = 0
    tries: dict[str, int] = {}
    width = max(1, QUERY_PARALLELISM)

    try:
        while pending:
            if writer.known_count >= max_candidates:
                break
            search_state.check_cancelled(db, job)

            batch: list[QuerySpec] = []
            while pending and len(batch) < width:
                if writer.known_count >= max_candidates:
                    break
                batch.append(pending.pop(0))
            if not batch:
                break

            fetched: list[tuple[QuerySpec, SearchResponse, bool]] = []
            work: list[QuerySpec] = []
            for spec in batch:
                attempted += 1
                cached = cache.get_cached(db, spec.hash)
                if cached is not None:
                    fetched.append(
                        (spec, SearchResponse(SearchOutcome.OK, hits=cached), True)
                    )
                else:
                    work.append(spec)

            if width == 1 or len(work) == 1:
                for spec in work:
                    try:
                        issued, response = _issue_query(spec, tries.get(spec.hash, 0))
                    except Exception as exc:
                        issued, response = spec, SearchResponse(
                            SearchOutcome.ERROR, detail=type(exc).__name__
                        )
                    fetched.append((issued, response, False))
            elif work:
                slots: list[tuple[QuerySpec, SearchResponse] | BaseException | None] = [
                    None
                ] * len(work)

                def _run(index: int, spec: QuerySpec) -> None:
                    try:
                        slots[index] = _issue_query(spec, tries.get(spec.hash, 0))
                    except Exception as exc:  # noqa: BLE001 — converted to a query error
                        slots[index] = exc

                threads = [
                    threading.Thread(
                        target=_run,
                        args=(index, spec),
                        name=f"mark-search-{index}",
                        daemon=True,
                    )
                    for index, spec in enumerate(work)
                ]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join()
                for spec, slot in zip(work, slots):
                    if isinstance(slot, BaseException):
                        fetched.append(
                            (
                                spec,
                                SearchResponse(
                                    SearchOutcome.ERROR, detail=type(slot).__name__
                                ),
                                False,
                            )
                        )
                    elif slot is None:
                        fetched.append(
                            (
                                spec,
                                SearchResponse(SearchOutcome.ERROR, detail="thread_lost"),
                                False,
                            )
                        )
                    else:
                        fetched.append((*slot, False))

            for spec, response, from_cache in fetched:
                if response.outcome == SearchOutcome.OK and not from_cache:
                    cache.store(
                        db,
                        query_hash=spec.hash,
                        query_text=spec.text,
                        page=spec.page,
                        hits=response.hits,
                    )

            stop = False
            prev_known = writer.known_count
            for spec, response, from_cache in fetched:
                if response.ok:
                    rate_limit_streak = 0
                    follow_up = _accept_hits(
                        db,
                        job,
                        writer,
                        spec,
                        response,
                        from_cache=from_cache,
                        queued_pages=queued_pages,
                    )
                    if follow_up is not None:
                        pending.append(follow_up)
                    continue

                tries[spec.hash] = tries.get(spec.hash, 0) + 1
                used = tries[spec.hash]
                if _accept_failure(db, job, spec, response, used=used):
                    pending.append(spec)
                    continue

                search_state.bump(db, job, failed_queries=1, errors_count=1)
                if response.outcome == SearchOutcome.RATE_LIMITED:
                    rate_limit_streak += 1
                if rate_limit_streak >= RATE_LIMIT_STREAK_LIMIT:
                    log_event(
                        logger,
                        "discovery.rate_limit_stop",
                        level=logging.WARNING,
                        trace_id=job.trace_id,
                        search_id=job.id,
                        remaining=len(pending),
                    )
                    stop = True
                    break
            if writer.known_count > prev_known and not job.cancel_requested:
                qualify_candidates(
                    db,
                    search_job_id=job.id,
                    prompt=job.prompt,
                    filters=filters,
                )
                promoted_now = promote_qualified(db, job)
                job.qualified_leads = promoted_now
                job.message = f"{writer.known_count} candidates found · {promoted_now} relevant leads"
                db.commit()
            if stop:
                break

    except search_state.SearchCancelled:
        search_state.finish(
            db,
            job,
            SearchJobStatus.CANCELLED,
            message=f"Search cancelled · {writer.known_count} candidates kept",
        )
        return

    discovery_ms = int((time.monotonic() - started) * 1000)
    log_event(
        logger,
        "discovery.complete",
        trace_id=job.trace_id,
        search_id=job.id,
        queries_attempted=attempted,
        queries_succeeded=job.completed_queries,
        queries_failed=job.failed_queries,
        candidates=job.candidates_found,
        duplicates=job.candidates_deduplicated,
        parallelism=width,
        duration_ms=discovery_ms,
    )

    _qualify_and_finish(db, job, discovery_ms=discovery_ms)


def _qualify_and_finish(db: Session, job: SearchJob, *, discovery_ms: int) -> None:
    search_state.begin_stage(
        db, job, SearchJobStatus.FILTERING, "qualification", message="Scoring candidates…"
    )

    stats = qualify_candidates(
        db,
        search_job_id=job.id,
        prompt=job.prompt,
        filters=dict(job.filters or {}),
    )
    promoted = promote_qualified(db, job)
    job.qualified_leads = promoted
    job.emails_found = (
        db.query(LeadRecord)
        .filter(LeadRecord.search_job_id == job.id, LeadRecord.email.isnot(None))
        .count()
    )
    job.phones_found = (
        db.query(LeadRecord)
        .filter(LeadRecord.search_job_id == job.id, LeadRecord.phone.isnot(None))
        .count()
    )
    db.commit()

    breakdown = rejection_breakdown(db, job.id)
    log_event(
        logger,
        "qualification.complete",
        trace_id=job.trace_id,
        search_id=job.id,
        evaluated=stats["evaluated"],
        qualified=stats["qualified"],
        rejected=stats["rejected"],
        promoted=promoted,
        top_rejection=max(breakdown, key=breakdown.get) if breakdown else None,
    )

    discovered = int(job.candidates_found or 0)
    failed = int(job.failed_queries or 0)
    succeeded = int(job.completed_queries or 0)

    if discovered == 0 and succeeded == 0:
        search_state.finish(
            db,
            job,
            SearchJobStatus.FAILED,
            message=(
                "The public search engine did not respond. Wait a moment and try again — "
                "no sample leads were invented."
            ),
            error_message="all_queries_failed",
        )
        return

    summary = (
        f"{discovered} candidates found · {promoted} relevant leads"
        if discovered
        else "No candidates matched that prompt. Try a simpler prompt or fewer filters."
    )

    if failed and succeeded:
        search_state.finish(
            db,
            job,
            SearchJobStatus.PARTIALLY_COMPLETED,
            message=f"{summary} · {failed} of {failed + succeeded} queries failed",
        )
    else:
        search_state.finish(db, job, SearchJobStatus.COMPLETED, message=summary)

    if promoted:
        enrichment_mode = str((job.filters or {}).get("enrichment_mode") or "after_discovery").lower()
        if enrichment_mode != "selected":
            from backend.services.search_jobs import enqueue_enrichment

            enqueue_enrichment(db, job)


def qualified_candidate_query(db: Session, search_job_id: int):
    return db.query(SearchCandidate).filter(
        SearchCandidate.search_job_id == search_job_id,
        SearchCandidate.status == CandidateStatus.QUALIFIED.value,
    )
