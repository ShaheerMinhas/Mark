"""Multi-engine search: rotation, fallback, and per-engine health.

No test here touches the network. The dispatcher is driven with stub engines, and
``DdgsEngine`` is given a fake client.
"""

from __future__ import annotations

import pytest

from backend.services.search_engines import health
from backend.services.search_engines.base import (
    SearchOutcome,
    SearchResponse,
    Throttle,
    classify_exception,
    classify_status,
    clean_redirect,
    dedupe_hits,
    strip_tags,
)
from backend.services.search_engines.dispatcher import EngineDispatcher
from backend.services.search_engines.engines import (
    DEFAULT_BACKENDS,
    DdgsEngine,
    default_engines,
)


@pytest.fixture(autouse=True)
def _clean_health():
    health.reset()
    yield
    health.reset()


class StubEngine:
    """An engine that returns a scripted sequence of responses."""

    min_interval = 0.0

    def __init__(self, name, responses):
        self.name = name
        self.throttle = Throttle(0.0)
        self._responses = list(responses)
        self.calls = 0

    def fetch(self, query, *, max_results, page):
        self.calls += 1
        response = self._responses[min(self.calls - 1, len(self._responses) - 1)]
        return SearchResponse(
            response.outcome,
            hits=list(response.hits),
            detail=response.detail,
            engine=self.name,
        )


def ok(n=2, engine=""):
    return SearchResponse(
        SearchOutcome.OK,
        hits=[{"title": f"r{i}", "href": f"https://example.com/{engine}{i}", "body": ""} for i in range(n)],
    )


def limited():
    return SearchResponse(SearchOutcome.RATE_LIMITED, detail="http_429")


def errored():
    return SearchResponse(SearchOutcome.ERROR, detail="http_500")


class TestHelpers:
    def test_strip_tags_unescapes_and_collapses(self):
        assert strip_tags("<b>Anna</b>&nbsp;Lind  <i>AB</i>") == "Anna Lind AB"

    def test_duckduckgo_redirect_is_unwrapped(self):
        wrapped = "//duckduckgo.com/l/?uddg=https%3A%2F%2Fwww.linkedin.com%2Fin%2Fanna&rut=x"
        assert clean_redirect(wrapped) == "https://www.linkedin.com/in/anna"

    def test_plain_urls_pass_through(self):
        assert clean_redirect("https://www.linkedin.com/in/anna") == "https://www.linkedin.com/in/anna"

    def test_protocol_relative_urls_get_a_scheme(self):
        assert clean_redirect("//example.com/x") == "https://example.com/x"

    def test_garbage_is_dropped(self):
        assert clean_redirect("") == ""
        assert clean_redirect("javascript:void(0)") == ""

    def test_dedupe_keeps_first_and_preserves_order(self):
        hits = [
            {"href": "https://a.com/1", "title": "first"},
            {"href": "https://a.com/1/", "title": "dupe"},
            {"href": "https://b.com/2", "title": "second"},
        ]
        out = dedupe_hits(hits)
        assert [h["title"] for h in out] == ["first", "second"]

    @pytest.mark.parametrize(
        "status,expected",
        [
            (200, SearchOutcome.OK),
            (429, SearchOutcome.RATE_LIMITED),
            (403, SearchOutcome.RATE_LIMITED),
            (401, SearchOutcome.RATE_LIMITED),
            (500, SearchOutcome.ERROR),
            (404, SearchOutcome.ERROR),
        ],
    )
    def test_status_classification(self, status, expected):
        assert classify_status(status)[0] == expected

    def test_rate_limit_exceptions_are_recognised(self):
        assert classify_exception(Exception("429 Too Many Requests"))[0] == SearchOutcome.RATE_LIMITED

    def test_timeouts_are_recognised(self):
        assert classify_exception(TimeoutError("read timeout"))[0] == SearchOutcome.TIMEOUT


class TestDispatcherRotation:
    def test_consecutive_queries_use_different_engines(self):
        a, b, c = StubEngine("a", [ok()]), StubEngine("b", [ok()]), StubEngine("c", [ok()])
        d = EngineDispatcher([a, b, c])
        for _ in range(3):
            d.search("q")
        assert [a.calls, b.calls, c.calls] == [1, 1, 1]

    def test_rotation_spreads_load_over_many_queries(self):
        engines = [StubEngine(f"e{i}", [ok()]) for i in range(4)]
        d = EngineDispatcher(engines)
        for _ in range(20):
            d.search("q")
        assert all(e.calls == 5 for e in engines)

    def test_a_single_engine_still_works(self):
        only = StubEngine("only", [ok()])
        assert EngineDispatcher([only]).search("q").ok
        assert only.calls == 1

    def test_no_engines_is_reported_not_raised(self):
        response = EngineDispatcher([]).search("q")
        assert response.outcome == SearchOutcome.ERROR
        assert response.detail == "no_engines_configured"

    def test_blank_query_short_circuits(self):
        engine = StubEngine("a", [ok()])
        assert EngineDispatcher([engine]).search("  ").outcome == SearchOutcome.EMPTY
        assert engine.calls == 0


class TestDispatcherFallback:
    def test_a_rate_limited_engine_falls_through_to_the_next(self):
        bad, good = StubEngine("bad", [limited()]), StubEngine("good", [ok()])
        response = EngineDispatcher([bad, good]).search("q")
        assert response.ok
        assert response.engine == "good"
        assert bad.calls == 1 and good.calls == 1

    def test_the_query_is_not_lost_when_the_first_engine_errors(self):
        bad, good = StubEngine("bad", [errored()]), StubEngine("good", [ok()])
        assert EngineDispatcher([bad, good]).search("q").ok

    def test_a_rate_limited_engine_is_benched_for_later_queries(self):
        bad, good = StubEngine("bad", [limited()]), StubEngine("good", [ok()])
        d = EngineDispatcher([bad, good])
        d.search("q1")
        before = bad.calls
        for _ in range(5):
            d.search("q")
        assert bad.calls == before, "benched engine was asked again"
        assert health.is_blocked("bad")

    def test_every_engine_failing_reports_the_last_failure(self):
        d = EngineDispatcher([StubEngine("a", [limited()]), StubEngine("b", [errored()])])
        response = d.search("q")
        assert not response.ok

    def test_one_query_benches_every_engine_that_throttled_it(self):
        a, b = StubEngine("a", [limited()]), StubEngine("b", [limited()])
        EngineDispatcher([a, b]).search("q")
        assert health.blocked_engines() == ["a", "b"]

    def test_a_query_is_still_attempted_when_every_engine_is_benched(self):
        """Rather than reporting a failure we never actually observed."""
        a, b = StubEngine("a", [limited()]), StubEngine("b", [limited()])
        health.record_failure("a", SearchOutcome.RATE_LIMITED)
        health.record_failure("b", SearchOutcome.RATE_LIMITED)
        assert health.blocked_engines() == ["a", "b"]

        EngineDispatcher([a, b]).search("q")
        assert a.calls + b.calls == 1, "a benched pool should still try exactly one engine"

    def test_a_benched_engine_recovers_once_it_answers_again(self):
        engine = StubEngine("a", [limited(), ok()])
        d = EngineDispatcher([engine])
        d.search("q1")
        assert health.is_blocked("a")
        assert d.search("q2").ok
        assert not health.is_blocked("a")

    def test_success_clears_a_previous_failure(self):
        engine = StubEngine("flaky", [errored(), ok()])
        d = EngineDispatcher([engine])
        d.search("q1")
        d.search("q2")
        assert not health.is_blocked("flaky")

    def test_empty_results_do_not_bench_an_engine(self):
        """A genuine zero-result query is not the engine's fault."""
        engine = StubEngine("a", [SearchResponse(SearchOutcome.EMPTY)])
        EngineDispatcher([engine]).search("q")
        assert not health.is_blocked("a")

    def test_empty_on_the_first_engine_tries_the_next(self):
        empty, good = (
            StubEngine("empty", [SearchResponse(SearchOutcome.EMPTY)]),
            StubEngine("good", [ok()]),
        )
        response = EngineDispatcher([empty, good]).search("q")
        assert response.ok
        assert response.engine == "good"
        assert empty.calls == 1 and good.calls == 1

    def test_email_lookups_stop_after_two_engines(self):
        """A miss must not waterfall through every replica or enrichment never finishes."""
        a, b, c = (
            StubEngine("a", [SearchResponse(SearchOutcome.EMPTY)]),
            StubEngine("b", [SearchResponse(SearchOutcome.EMPTY)]),
            StubEngine("c", [ok()]),
        )
        response = EngineDispatcher([a, b, c]).search("q", max_attempts=2)
        assert response.outcome == SearchOutcome.EMPTY
        assert a.calls == 1 and b.calls == 1 and c.calls == 0

    def test_every_engine_empty_is_reported_as_empty_not_failure(self):
        d = EngineDispatcher(
            [
                StubEngine("a", [SearchResponse(SearchOutcome.EMPTY)]),
                StubEngine("b", [SearchResponse(SearchOutcome.EMPTY)]),
            ]
        )
        response = d.search("q")
        assert response.outcome == SearchOutcome.EMPTY
        assert not health.blocked_engines()


class TestEngineHealth:
    def test_a_rate_limit_benches_immediately(self):
        health.record_failure("x", SearchOutcome.RATE_LIMITED)
        assert health.is_blocked("x")

    def test_a_single_error_does_not_bench(self):
        health.record_failure("x", SearchOutcome.ERROR)
        assert not health.is_blocked("x")

    def test_repeated_errors_bench(self):
        for _ in range(health.FAILURE_THRESHOLD):
            health.record_failure("x", SearchOutcome.ERROR)
        assert health.is_blocked("x")

    def test_cooldown_grows_on_repeat_offences(self):
        health.record_failure("x", SearchOutcome.RATE_LIMITED)
        first = health.retry_after("x")
        health.record_failure("x", SearchOutcome.RATE_LIMITED)
        assert health.retry_after("x") > first

    def test_cooldown_is_capped(self):
        for _ in range(20):
            health.record_failure("x", SearchOutcome.RATE_LIMITED)
        assert health.retry_after("x") <= health.MAX_COOLDOWN_SECONDS

    def test_success_resets_everything(self):
        health.record_failure("x", SearchOutcome.RATE_LIMITED)
        health.record_success("x")
        assert not health.is_blocked("x")
        assert health.retry_after("x") == 0

    def test_snapshot_reports_per_engine_counters(self):
        health.record_success("a")
        health.record_failure("b", SearchOutcome.RATE_LIMITED)
        snap = health.snapshot()
        assert snap["a"]["successes"] == 1
        assert snap["b"]["blocked_for"] > 0

    def test_engine_health_is_independent_of_email_provider_health(self):
        """The two registries must not share state or pollute each other's logs."""
        from backend.services import provider_health

        provider_health.reset()
        health.record_failure("bing", SearchOutcome.RATE_LIMITED)
        assert "bing" not in provider_health.open_providers()


class TestDefaultPool:
    def test_several_engines_are_configured(self):
        assert len(default_engines()) >= 6

    def test_engine_names_are_unique(self):
        names = [e.name for e in default_engines()]
        assert len(names) == len(set(names))

    def test_google_leads_the_rotation(self):
        """Live checks showed Google returning the cleanest LinkedIn hits."""
        assert default_engines()[0].backend == "google"

    def test_each_backend_is_replicated(self):
        """Several clients per backend is what makes parallel queries faster."""
        engines = default_engines()
        by_backend: dict[str, int] = {}
        for engine in engines:
            by_backend[engine.backend] = by_backend.get(engine.backend, 0) + 1
        assert all(count >= 2 for count in by_backend.values()), by_backend
        assert len(engines) == sum(by_backend.values())

    def test_each_engine_has_its_own_throttle(self):
        """A shared throttle would serialise every query behind one delay."""
        engines = default_engines()
        throttles = {id(e.throttle) for e in engines}
        assert len(throttles) == len(engines)

    def test_the_env_override_selects_a_subset(self, monkeypatch):
        monkeypatch.setenv("MARK_SEARCH_ENGINES", "yahoo,google")
        monkeypatch.setenv("MARK_ENGINE_REPLICAS", "1")
        names = [e.name for e in default_engines()]
        assert names == ["yahoo#0", "google#0"]

    def test_a_blank_env_override_falls_back_to_the_verified_set(self, monkeypatch):
        monkeypatch.setenv("MARK_SEARCH_ENGINES", "  ")
        monkeypatch.setenv("MARK_ENGINE_REPLICAS", "1")
        names = [e.name for e in default_engines()]
        assert names == [f"{backend}#0" for backend, _interval in DEFAULT_BACKENDS]

    def test_email_lookups_use_a_separate_named_pool(self):
        """Enrichment must not share discovery's throttle slots."""
        names = [e.name for e in default_engines(pool="email")]
        assert names
        assert all(name.startswith("email:") for name in names)
        assert [e.name for e in default_engines()][0] == "google#0"


class TestDdgsEngine:
    def test_hits_are_normalised_to_title_href_body(self, monkeypatch):
        engine = DdgsEngine("google", min_interval=0)

        class _Client:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def text(self, query, **kwargs):
                return [
                    {
                        "title": "Anna Lind - Marketing Manager",
                        "href": "https://www.linkedin.com/in/anna-lind",
                        "body": "Marketing Manager at Nordic Freight AB.",
                    },
                    {
                        "title": "Anna again",
                        "link": "https://www.linkedin.com/in/anna-lind/",
                        "snippet": "duplicate",
                    },
                ]

        monkeypatch.setattr(engine, "_client", lambda: _Client())
        response = engine.fetch("q", max_results=15, page=1)
        assert response.ok
        assert response.engine == "google#0"
        assert len(response.hits) == 1
        assert response.hits[0]["href"] == "https://www.linkedin.com/in/anna-lind"

    def test_no_results_found_is_empty_not_an_error(self, monkeypatch):
        engine = DdgsEngine("brave", min_interval=0)

        class _Client:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def text(self, query, **kwargs):
                raise Exception("No results found.")

        monkeypatch.setattr(engine, "_client", lambda: _Client())
        response = engine.fetch("q", max_results=10, page=1)
        assert response.outcome == SearchOutcome.EMPTY
        assert response.engine == "brave#0"

    def test_a_timeout_is_classified(self, monkeypatch):
        engine = DdgsEngine("yahoo", min_interval=0)

        class _Client:
            def __enter__(self):
                raise TimeoutError("read timeout")

            def __exit__(self, *_args):
                return False

        monkeypatch.setattr(engine, "_client", lambda: _Client())
        response = engine.fetch("q", max_results=10, page=1)
        assert response.outcome == SearchOutcome.TIMEOUT
        assert response.engine == "yahoo#0"
