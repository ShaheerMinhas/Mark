"""Combined progress for one search run, including email lookup.

The percent is a share of work the pipeline has actually counted:

* discovery — finished queries / planned queries
* qualification — done, or not done; there is no partial score
* email lookup — people checked / people in the pass, and only when that pass
  was requested or is actually running

A run with nothing measured yet has no percent. Email lookup is never described
as running, finished, or skipped unless the queue, the enrichment counters, or
a Find-emails job say so.
"""

from __future__ import annotations

from dataclasses import dataclass

from backend.models import SearchJobStatus

OPEN_STATUSES = {
    SearchJobStatus.PENDING.value,
    SearchJobStatus.RUNNING.value,
    SearchJobStatus.DISCOVERING.value,
    SearchJobStatus.FILTERING.value,
    SearchJobStatus.ENRICHING.value,
}

TERMINAL_SUCCESS = {
    SearchJobStatus.COMPLETED.value,
    SearchJobStatus.PARTIALLY_COMPLETED.value,
}

DISCOVERY_WITH_EMAIL = 60
QUAL_SHARE = 10
EMAIL_SHARE = 30
DISCOVERY_WITHOUT_EMAIL = 90


def normalize_enrichment_mode(raw: object) -> str:
    mode = str(raw or "after_discovery").strip().lower()
    if mode in {"after_discovery", "auto", "selected"}:
        return mode
    return "after_discovery"


def email_lookup_enabled(mode: str) -> bool:
    """True when this search asked to look up emails after discovery."""
    return normalize_enrichment_mode(mode) != "selected"


@dataclass
class ManualEmailJob:
    status: str
    processed: int = 0
    total: int = 0
    found: int = 0
    inferred: int = 0
    verified: int = 0
    not_found: int = 0
    message: str = ""

    @property
    def recorded(self) -> int:
        return int(self.found or 0) + int(self.inferred or 0) + int(self.verified or 0)


@dataclass
class EmailLookup:
    enabled: bool
    mode: str
    state: str
    source: str
    reason: str = ""
    processed: int = 0
    total: int = 0
    found: int = 0
    detail: str = ""


@dataclass
class RunStage:
    key: str
    label: str
    state: str


@dataclass
class SearchRun:
    active: bool
    stage: str
    stage_label: str
    message: str
    percent: int | None
    determinate: bool
    email_lookup: EmailLookup
    stages: list[RunStage]

    def as_dict(self) -> dict:
        return {
            "active": self.active,
            "stage": self.stage,
            "stage_label": self.stage_label,
            "message": self.message,
            "percent": self.percent,
            "determinate": self.determinate,
            "email_lookup": {
                "enabled": self.email_lookup.enabled,
                "mode": self.email_lookup.mode,
                "state": self.email_lookup.state,
                "source": self.email_lookup.source,
                "reason": self.email_lookup.reason,
                "processed": self.email_lookup.processed,
                "total": self.email_lookup.total,
                "found": self.email_lookup.found,
                "detail": self.email_lookup.detail,
            },
            "stages": [{"key": item.key, "label": item.label, "state": item.state} for item in self.stages],
        }


def _status(value: object) -> str:
    return str(value or "").strip().lower()


def _plural(count: int, one: str, many: str) -> str:
    count = int(count or 0)
    return f"{count} {one if count == 1 else many}"


def _query_ratio(total: int, completed: int, failed: int) -> float | None:
    total = int(total or 0)
    if total <= 0:
        return None
    done = int(completed or 0) + int(failed or 0)
    return max(0.0, min(1.0, done / total))


def _view(
    *,
    enabled: bool,
    mode: str,
    state: str,
    source: str,
    reason: str = "",
    processed: int = 0,
    total: int = 0,
    found: int = 0,
    detail: str = "",
) -> EmailLookup:
    return EmailLookup(
        enabled=enabled,
        mode=mode,
        state=state,
        source=source,
        reason=reason,
        processed=int(processed or 0),
        total=int(total or 0),
        found=int(found or 0),
        detail=detail,
    )


def resolve_email_lookup(
    *,
    status: str,
    enrichment_mode: object,
    qualified_leads: int = 0,
    enrichment_attempted: int = 0,
    enrichment_completed: int = 0,
    emails_found: int = 0,
    cancel_requested: bool = False,
    queue_status: str | None = None,
    queue_error: str | None = None,
    manual: ManualEmailJob | None = None,
) -> EmailLookup:
    """What email lookup is doing, using only persisted job state."""
    mode = normalize_enrichment_mode(enrichment_mode)
    enabled = email_lookup_enabled(mode)
    current = _status(status)
    qualified = int(qualified_leads or 0)
    completed = int(enrichment_completed or 0)
    attempted = int(enrichment_attempted or 0)
    found = int(emails_found or 0)
    queue = _status(queue_status) or ""

    if manual is not None and _status(manual.status) == "running":
        return _view(
            enabled=enabled,
            mode=mode,
            state="running",
            source="manual",
            reason="manual",
            processed=manual.processed,
            total=manual.total,
            found=manual.recorded,
            detail="Looking up emails now",
        )

    if current == SearchJobStatus.ENRICHING.value or queue == "running":
        return _view(
            enabled=enabled,
            mode=mode,
            state="running",
            source="search",
            reason="search",
            processed=completed,
            total=qualified,
            found=found,
            detail="Looking up emails now",
        )

    if cancel_requested and queue == "queued" and current in TERMINAL_SUCCESS:
        return _view(
            enabled=enabled,
            mode=mode,
            state="stopped",
            source="search",
            reason="stopped",
            processed=completed,
            total=qualified,
            found=found,
            detail="Email lookup was stopped before it started",
        )

    if queue == "queued":
        waiting = "Email lookup is queued and has not started"
        if (queue_error or "").strip():
            waiting = "Email lookup hit a problem and is waiting to retry. It is not running right now."
        return _view(
            enabled=enabled,
            mode=mode,
            state="pending",
            source="search",
            reason="retry" if (queue_error or "").strip() else "queued",
            processed=completed,
            total=qualified,
            found=found,
            detail=waiting,
        )

    if queue == "failed":
        return _view(
            enabled=enabled,
            mode=mode,
            state="failed",
            source="search",
            reason="failed",
            processed=attempted or completed,
            total=qualified,
            found=found,
            detail="Email lookup failed",
        )

    if (
        queue == "done"
        and cancel_requested
        and qualified > 0
        and completed < qualified
        and current != SearchJobStatus.CANCELLED.value
    ):
        return _view(
            enabled=enabled,
            mode=mode,
            state="stopped",
            source="search",
            reason="stopped",
            processed=completed,
            total=qualified,
            found=found,
            detail=(
                "Email lookup was stopped after checking "
                f"{_plural(completed, 'person', 'people')}"
            ),
        )

    if queue == "done" and qualified > 0 and completed == 0 and attempted > 0:
        return _view(
            enabled=enabled,
            mode=mode,
            state="failed",
            source="search",
            reason="failed",
            processed=attempted,
            total=qualified,
            found=found,
            detail="Email lookup failed",
        )

    if queue == "done" or (enabled and current in TERMINAL_SUCCESS and completed > 0):
        return _view(
            enabled=enabled,
            mode=mode,
            state="finished",
            source="search",
            reason="finished",
            processed=completed or qualified,
            total=qualified or completed,
            found=found,
            detail="Email lookup finished",
        )

    if manual is not None and _status(manual.status) == "completed":
        return _view(
            enabled=enabled,
            mode=mode,
            state="finished",
            source="manual",
            reason="manual",
            processed=manual.processed,
            total=manual.total,
            found=manual.recorded,
            detail="Email lookup finished",
        )
    if manual is not None and _status(manual.status) == "failed":
        return _view(
            enabled=enabled,
            mode=mode,
            state="failed",
            source="manual",
            reason="manual",
            processed=manual.processed,
            total=manual.total,
            found=manual.recorded,
            detail="Email lookup failed",
        )

    if current in OPEN_STATUSES and enabled:
        return _view(
            enabled=enabled,
            mode=mode,
            state="pending",
            source="search",
            reason="waiting",
            processed=0,
            total=qualified,
            found=found,
            detail="Email lookup waits until results are in",
        )

    if not enabled:
        if current in OPEN_STATUSES:
            return _view(
                enabled=False,
                mode=mode,
                state="not_requested",
                source="none",
                reason="off",
                found=found,
                detail="Email lookup is off for this search",
            )
        return _view(
            enabled=False,
            mode=mode,
            state="skipped",
            source="none",
            reason="off",
            found=found,
            detail="Email lookup was turned off, so addresses were not searched",
        )

    if current in {SearchJobStatus.FAILED.value, SearchJobStatus.CANCELLED.value}:
        return _view(
            enabled=enabled,
            mode=mode,
            state="skipped",
            source="none",
            reason="search_stopped",
            found=found,
            detail="Email lookup did not run",
        )

    if qualified == 0 and current in TERMINAL_SUCCESS:
        return _view(
            enabled=enabled,
            mode=mode,
            state="skipped",
            source="none",
            reason="no_leads",
            found=found,
            detail="There were no leads to look up emails for",
        )

    return _view(
        enabled=enabled,
        mode=mode,
        state="skipped",
        source="none",
        reason="not_run",
        found=found,
        detail="Email lookup did not run",
    )


def _email_band(email: EmailLookup) -> bool:
    """Whether the unfinished-or-finished email pass is part of this run's bar."""
    if email.source == "manual":
        return False
    if email.state in {"pending", "running", "finished", "failed", "stopped"}:
        return True
    if email.enabled and email.state == "skipped" and email.reason in {"not_run", "search_stopped"}:
        return True
    return False


def _discovery_fraction(status: str, ratio: float | None) -> float | None:
    if ratio is not None and status in {
        SearchJobStatus.PENDING.value,
        SearchJobStatus.DISCOVERING.value,
        SearchJobStatus.RUNNING.value,
        SearchJobStatus.FAILED.value,
        SearchJobStatus.CANCELLED.value,
    }:
        return ratio
    if status in {
        SearchJobStatus.FILTERING.value,
        SearchJobStatus.ENRICHING.value,
        *TERMINAL_SUCCESS,
    }:
        return 1.0
    if ratio is not None:
        return ratio
    if status in OPEN_STATUSES:
        return None
    return 0.0


def _qualification_done(status: str, email: EmailLookup) -> bool:
    if status in {SearchJobStatus.ENRICHING.value, *TERMINAL_SUCCESS}:
        return True
    if email.source == "search" and email.state in {"running", "finished", "failed", "stopped"}:
        return True
    if email.source == "manual" and email.state in {"running", "finished", "failed"}:
        return True
    return False


def _email_fraction(email: EmailLookup) -> float:
    if email.state == "finished":
        return 1.0
    if email.state in {"running", "failed", "stopped"} and email.total > 0:
        return max(0.0, min(1.0, email.processed / email.total))
    return 0.0


def combined_percent(
    *,
    status: str,
    email: EmailLookup,
    total_queries: int,
    completed_queries: int,
    failed_queries: int,
) -> tuple[int | None, bool]:
    """Return (percent, determinate). Percent is null when no work has been measured."""
    current = _status(status)
    if email.source == "manual" and email.state == "running":
        if email.total <= 0:
            return None, False
        return min(99, int(round(100 * email.processed / max(email.total, 1)))), True
    if email.source == "manual" and email.state == "finished":
        return 100, True
    if email.source == "manual" and email.state == "failed":
        if email.total <= 0:
            return 0, True
        return min(99, int(round(100 * email.processed / email.total))), True

    ratio = _query_ratio(total_queries, completed_queries, failed_queries)
    disc = _discovery_fraction(current, ratio)
    if disc is None:
        return None, False

    include_email = _email_band(email)
    discovery_weight = DISCOVERY_WITH_EMAIL if include_email else DISCOVERY_WITHOUT_EMAIL
    qual = 1.0 if _qualification_done(current, email) else 0.0
    email_ratio = _email_fraction(email) if include_email else 0.0
    raw = discovery_weight * disc + QUAL_SHARE * qual + (EMAIL_SHARE * email_ratio if include_email else 0)
    percent = int(round(raw))

    if current in TERMINAL_SUCCESS and email.state == "finished":
        return 100, True
    if current in TERMINAL_SUCCESS and not include_email:
        return 100, True
    if email.state in {"running", "pending", "failed", "stopped"} or current in {
        SearchJobStatus.FAILED.value,
        SearchJobStatus.CANCELLED.value,
    }:
        return min(99, max(0, percent)), True
    return max(0, min(100, percent)), True


def _checked_sentence(email: EmailLookup) -> str:
    if email.total > 0:
        message = (
            f"Looking up emails · checked {email.processed} of {email.total} "
            f"{'person' if email.total == 1 else 'people'}"
        )
    else:
        message = "Looking up emails"
    if email.source == "manual":
        message += f" · {_plural(email.found, 'email', 'emails')} recorded in this pass"
    else:
        message += f" · {_plural(email.found, 'lead', 'leads')} with an email"
    return message


def _finished_sentence(email: EmailLookup) -> str:
    if email.source == "manual":
        return f"Email lookup finished · {_plural(email.found, 'email', 'emails')} recorded in this pass."
    if email.found <= 0:
        return "Email lookup finished · no emails found."
    return f"Email lookup finished · {_plural(email.found, 'lead has', 'leads have')} an email."


def narrate(
    *,
    status: str,
    email: EmailLookup,
    total_queries: int,
    completed_queries: int,
    failed_queries: int,
    job_message: str = "",
) -> tuple[str, str, str]:
    current = _status(status)
    query_note = ""
    if int(total_queries or 0) > 0:
        done = int(completed_queries or 0) + int(failed_queries or 0)
        query_note = f" · {done} of {int(total_queries)} queries"

    if current == SearchJobStatus.CANCELLED.value:
        return "cancelled", "Search stopped", f"Search stopped. {email.detail}."
    if current == SearchJobStatus.FAILED.value and email.state != "failed":
        base = (job_message or "Search failed.").rstrip(".")
        return "failed", "Search failed", f"{base}. {email.detail}."
    if email.state == "running":
        return "email_lookup", "Looking up emails", _checked_sentence(email)
    if email.state == "failed":
        return "email_lookup", "Email lookup failed", "Email lookup failed. Leads already saved are still here."
    if email.state == "stopped":
        return "email_lookup", "Email lookup stopped", email.detail
    if email.state == "pending" and current in TERMINAL_SUCCESS:
        if email.reason == "retry":
            return "email_lookup", "Email lookup waiting to retry", email.detail
        return (
            "email_lookup",
            "Email lookup queued",
            "Results are in. Email lookup is queued and has not started.",
        )
    if current == SearchJobStatus.FILTERING.value:
        if email.state == "pending":
            extra = " Email lookup has not started."
        elif email.state == "not_requested":
            extra = " Email lookup is off."
        else:
            extra = ""
        return "qualification", "Scoring candidates", f"Scoring which people match this search.{extra}"
    if current in {SearchJobStatus.DISCOVERING.value, SearchJobStatus.RUNNING.value}:
        if email.state == "pending":
            extra = " Email lookup starts after results are in."
        elif email.state == "not_requested":
            extra = " Email lookup is off."
        else:
            extra = ""
        return "discovery", "Searching public profiles", f"Searching public profiles{query_note}.{extra}"
    if current == SearchJobStatus.PENDING.value:
        if email.enabled:
            extra = " Email lookup is on and starts after results are in."
        else:
            extra = " Email lookup is off."
        return "queued", "Queued", f"Queued. Discovery has not started.{extra}"
    if email.state == "finished":
        return "done", "Search finished", f"Search finished. {_finished_sentence(email)}"
    if email.state in {"skipped", "not_requested"}:
        return "done", "Search finished", f"Search finished. {email.detail}."
    base = job_message or "Search finished."
    return "done", "Search finished", base


def _stage_states(status: str, email: EmailLookup, ratio: float | None) -> list[RunStage]:
    current = _status(status)
    qual_done = _qualification_done(current, email)
    if current in {SearchJobStatus.DISCOVERING.value, SearchJobStatus.RUNNING.value} or (
        current == SearchJobStatus.PENDING.value and ratio is not None
    ):
        discovery = "active"
    elif current == SearchJobStatus.PENDING.value:
        discovery = "pending"
    elif current == SearchJobStatus.FAILED.value and not qual_done:
        discovery = "failed"
    elif current == SearchJobStatus.CANCELLED.value and not qual_done:
        discovery = "cancelled"
    else:
        discovery = "done"

    if current == SearchJobStatus.FILTERING.value:
        qualification = "active"
    elif qual_done:
        qualification = "done"
    elif current in {SearchJobStatus.FAILED.value, SearchJobStatus.CANCELLED.value}:
        qualification = "skipped"
    else:
        qualification = "waiting"

    email_state = {
        "not_requested": "off",
        "pending": "waiting",
        "running": "active",
        "finished": "done",
        "skipped": "skipped",
        "failed": "failed",
        "stopped": "stopped",
    }.get(email.state, "waiting")
    return [
        RunStage("discovery", "Discover", discovery),
        RunStage("qualification", "Qualify", qualification),
        RunStage("email", "Find emails", email_state),
    ]


def build_search_run(
    *,
    status: str,
    enrichment_mode: object,
    total_queries: int = 0,
    completed_queries: int = 0,
    failed_queries: int = 0,
    qualified_leads: int = 0,
    enrichment_attempted: int = 0,
    enrichment_completed: int = 0,
    emails_found: int = 0,
    cancel_requested: bool = False,
    job_message: str = "",
    queue_status: str | None = None,
    queue_error: str | None = None,
    manual: ManualEmailJob | None = None,
) -> SearchRun:
    email = resolve_email_lookup(
        status=status,
        enrichment_mode=enrichment_mode,
        qualified_leads=qualified_leads,
        enrichment_attempted=enrichment_attempted,
        enrichment_completed=enrichment_completed,
        emails_found=emails_found,
        cancel_requested=cancel_requested,
        queue_status=queue_status,
        queue_error=queue_error,
        manual=manual,
    )
    percent, determinate = combined_percent(
        status=status,
        email=email,
        total_queries=total_queries,
        completed_queries=completed_queries,
        failed_queries=failed_queries,
    )
    stage, stage_label, message = narrate(
        status=status,
        email=email,
        total_queries=total_queries,
        completed_queries=completed_queries,
        failed_queries=failed_queries,
        job_message=job_message,
    )
    current = _status(status)
    active = current in OPEN_STATUSES or email.state in {"pending", "running"}
    return SearchRun(
        active=active,
        stage=stage,
        stage_label=stage_label,
        message=message,
        percent=percent,
        determinate=determinate,
        email_lookup=email,
        stages=_stage_states(status, email, _query_ratio(total_queries, completed_queries, failed_queries)),
    )
