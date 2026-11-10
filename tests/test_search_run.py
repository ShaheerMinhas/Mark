"""Combined search progress must not invent email lookup or a percent."""

from backend.models import SearchJobStatus
from backend.services.search_run import ManualEmailJob, build_search_run


def _run(**overrides):
    params = {
        "status": SearchJobStatus.DISCOVERING.value,
        "enrichment_mode": "after_discovery",
        "total_queries": 10,
        "completed_queries": 4,
        "failed_queries": 0,
    }
    params.update(overrides)
    return build_search_run(**params)


def test_queued_search_has_no_percent_and_does_not_claim_email_lookup():
    run = _run(status="pending", total_queries=0, completed_queries=0)
    assert run.determinate is False
    assert run.percent is None
    assert run.email_lookup.state == "pending"
    assert "not started" in run.message.lower()
    assert "looking up" not in run.message.lower()


def test_discovery_percent_follows_finished_queries_when_email_is_on():
    run = _run()
    assert run.percent == 24  # 4/10 of the discovery share, email still waiting
    assert run.email_lookup.state == "pending"
    assert run.active is True
    assert "looking up emails" not in run.message.lower()
    assert "starts after results" in run.message.lower()
    assert run.stages[-1].state == "waiting"


def test_discovery_with_email_lookup_off_does_not_reserve_an_email_pass():
    run = _run(enrichment_mode="selected")
    assert run.email_lookup.enabled is False
    assert run.email_lookup.state == "not_requested"
    assert run.percent == 36  # 4/10 of a run that ends at qualification
    assert "off" in run.message.lower()
    assert run.stages[-1].state == "off"


def test_completed_search_with_email_queued_is_still_active_and_not_at_100():
    run = _run(
        status="completed",
        completed_queries=10,
        qualified_leads=8,
        queue_status="queued",
    )
    assert run.active is True
    assert run.email_lookup.state == "pending"
    assert run.percent == 70
    assert run.stage_label == "Email lookup queued"
    assert "has not started" in run.message.lower()
    assert "looking up" not in run.message.lower()


def test_stopping_a_queued_email_lookup_does_not_leave_it_pending():
    run = _run(
        status="completed",
        completed_queries=10,
        qualified_leads=8,
        queue_status="queued",
        cancel_requested=True,
    )
    assert run.active is False
    assert run.email_lookup.state == "stopped"
    assert "stopped before it started" in run.message.lower()
    assert "looking up" not in run.message.lower()


def test_email_retry_is_waiting_not_running():
    run = _run(
        status="completed",
        completed_queries=10,
        qualified_leads=8,
        queue_status="queued",
        queue_error="RuntimeError('provider')",
    )
    assert run.email_lookup.state == "pending"
    assert run.email_lookup.reason == "retry"
    assert "not running" in run.message.lower()
    assert run.percent == 70


def test_running_email_lookup_uses_people_checked():
    run = _run(
        status="enriching",
        completed_queries=10,
        qualified_leads=20,
        enrichment_completed=5,
        emails_found=2,
        queue_status="running",
    )
    assert run.email_lookup.state == "running"
    assert run.percent == 78  # 70 + 30 * 5/20
    assert "checked 5 of 20" in run.message.lower()
    assert "looking up emails" in run.message.lower()
    assert run.stages[-1].state == "active"


def test_finished_email_lookup_is_100_and_reports_the_real_count():
    run = _run(
        status="completed",
        completed_queries=10,
        qualified_leads=8,
        enrichment_completed=8,
        emails_found=3,
        queue_status="done",
    )
    assert run.active is False
    assert run.email_lookup.state == "finished"
    assert run.percent == 100
    assert "3 leads have an email" in run.message.lower()


def test_finished_lookup_with_no_addresses_does_not_invent_any():
    run = _run(
        status="completed",
        completed_queries=10,
        qualified_leads=4,
        enrichment_completed=4,
        emails_found=0,
        queue_status="done",
    )
    assert "no emails found" in run.message.lower()
    assert run.percent == 100


def test_email_off_completed_search_says_it_was_not_searched():
    run = _run(
        status="completed",
        enrichment_mode="selected",
        completed_queries=10,
        qualified_leads=4,
    )
    assert run.active is False
    assert run.email_lookup.state == "skipped"
    assert run.email_lookup.reason == "off"
    assert run.percent == 100
    assert "turned off" in run.message.lower()
    assert "looking up" not in run.message.lower()


def test_requested_email_lookup_that_never_started_is_not_called_finished():
    run = _run(
        status="completed",
        completed_queries=10,
        qualified_leads=4,
        enrichment_completed=0,
    )
    assert run.email_lookup.state == "skipped"
    assert run.email_lookup.reason == "not_run"
    assert run.percent == 70
    assert "did not run" in run.message.lower()
    assert run.percent != 100


def test_failed_search_does_not_report_full_progress_or_a_finished_lookup():
    run = _run(
        status="failed",
        completed_queries=2,
        failed_queries=0,
        total_queries=10,
        job_message="Search could not be completed. Please try again.",
    )
    assert run.percent == 12
    assert run.email_lookup.state == "skipped"
    assert "did not run" in run.message.lower()
    assert run.stage_label == "Search failed"


def test_cancelled_search_stops_without_pretending_email_lookup_ran():
    run = _run(status="cancelled", completed_queries=2, total_queries=10, cancel_requested=True)
    assert run.active is False
    assert run.email_lookup.state == "skipped"
    assert run.percent == 12
    assert run.stage_label == "Search stopped"


def test_stopping_email_lookup_early_keeps_a_partial_percent():
    run = _run(
        status="completed",
        completed_queries=10,
        qualified_leads=10,
        enrichment_completed=4,
        emails_found=1,
        cancel_requested=True,
        queue_status="done",
    )
    assert run.email_lookup.state == "stopped"
    assert run.percent == 82  # 70 + 30 * 4/10
    assert run.active is False
    assert "stopped" in run.message.lower()


def test_manual_find_emails_is_its_own_running_pass():
    run = _run(
        status="completed",
        enrichment_mode="selected",
        completed_queries=10,
        qualified_leads=10,
        manual=ManualEmailJob(status="running", processed=3, total=10, found=1),
    )
    assert run.active is True
    assert run.email_lookup.state == "running"
    assert run.email_lookup.source == "manual"
    assert run.email_lookup.enabled is False
    assert run.percent == 30
    assert "checked 3 of 10" in run.message.lower()
    assert "recorded in this pass" in run.message.lower()


def test_failed_email_job_is_not_marked_finished():
    run = _run(
        status="completed",
        completed_queries=10,
        qualified_leads=6,
        queue_status="failed",
        enrichment_attempted=2,
        enrichment_completed=0,
    )
    assert run.email_lookup.state == "failed"
    assert run.percent == 80  # 70 + 30 * 2/6
    assert run.percent != 100
    assert run.stage_label == "Email lookup failed"
