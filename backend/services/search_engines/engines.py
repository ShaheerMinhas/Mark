"""The individual public search engines.

All of them go through ``ddgs``, which is already a dependency and ships
maintained implementations for several backends. When ``ddgs``'s legacy
``google.com/wml/search`` endpoint returns 403/empty, or when ``search.yahoo.com``
(Bing) serves decoy Wikipedia/YouTube pages for ``site:linkedin.com/in`` queries,
we transparently query Google's index via ``search.yahoo.co.jp`` using ``primp``
and ``lxml`` (both already installed alongside ``ddgs``).

Each engine owns its own throttle, which is what makes a rotation faster than a
single engine: one shared delay serialised every query behind it.
"""

from __future__ import annotations

import os
import re
import time
from typing import Any

from backend.services.search_engines.base import (
    SearchOutcome,
    SearchResponse,
    Throttle,
    classify_exception,
    dedupe_hits,
)

REQUEST_TIMEOUT = 15.0

#: Backends verified to return usable results for ``site:linkedin.com/in``
#: queries. ``brave``, ``mojeek`` and ``startpage`` are supported by the library
#: but returned nothing for this query shape, so they are not in the rotation by
#: default; set MARK_SEARCH_ENGINES to try them.
DEFAULT_BACKENDS: tuple[tuple[str, float], ...] = (
    # (backend, minimum seconds between calls to that backend)
    ("google", 2.0),
    ("duckduckgo", 1.8),
    ("yahoo", 2.0),
)

#: Phrases these backends use for a genuine zero-result answer, which the library
#: raises as an exception rather than returning an empty list.
_EMPTY_MARKERS = ("no results found", "no results")


def _clean_title(text: str) -> str:
    cleaned = re.sub(r"\s+", " ", (text or "")).strip()
    cleaned = re.sub(r"\s*[\|\-–—]\s*LinkedIn\s*$", "", cleaned, flags=re.IGNORECASE)
    return cleaned


def _google_via_yahoo_jp(query: str, *, max_results: int = 10, page: int = 1) -> list[dict[str, str]]:
    """Query Google's web index via search.yahoo.co.jp (LY Corp)."""
    import primp
    from lxml import html

    client = primp.Client(
        impersonate="random",
        impersonate_os="random",
        timeout=int(REQUEST_TIMEOUT),
    )
    start_offset = max(1, (page - 1) * 10 + 1)
    resp = client.get(
        "https://search.yahoo.co.jp/search",
        params={"p": query, "b": str(start_offset)},
    )
    if resp.status_code >= 400 or not resp.text:
        return []
    tree = html.fromstring(resp.text)
    cards = tree.xpath("//div[contains(@class, 'sw-CardBase') and .//h3]")
    results: list[dict[str, str]] = []
    for card in cards:
        title = _clean_title(" ".join(card.xpath(".//h3//text()")))
        hrefs = card.xpath(".//a[.//h3]/@href") or card.xpath(".//a/@href")
        href = (hrefs[0] if hrefs else "").strip()
        if not title or not href.startswith("http"):
            continue
        if "search.yahoo.co.jp" in href:
            continue
        body = re.sub(
            r"\s+",
            " ",
            " ".join(card.xpath(".//*[contains(@class, 'sw-Card__summary')]//text()")),
        ).strip()
        results.append({"title": title, "href": href, "body": body})
    return results[:max_results]


def _has_valid_site_hits(query: str, hits: list[dict[str, Any]]) -> bool:
    """Reject Bing/Yahoo decoy SERPs (Wikipedia/YouTube) when site:linkedin.com was requested."""
    if "site:linkedin.com" not in (query or "").lower():
        return bool(hits)
    return any(
        "linkedin.com" in str(h.get("href") or h.get("link") or h.get("url") or "").lower()
        for h in hits
    )


class DdgsEngine:
    """One ``ddgs`` backend replica, with its own pacing and its own client.

    Several replicas of the same backend can run at once: each has a private
    throttle, so ten DuckDuckGo replicas are ten sessions, not one session
    asked ten times in a row.
    """

    def __init__(
        self,
        backend: str,
        min_interval: float = 2.0,
        replica: int = 0,
        *,
        pool: str = "",
    ) -> None:
        self.backend = backend
        self.replica = replica
        self.pool = pool
        self.name = f"{pool}:{backend}#{replica}" if pool else f"{backend}#{replica}"
        self.min_interval = min_interval
        self.throttle = Throttle(min_interval)

    def _client(self) -> Any:
        try:
            from ddgs import DDGS
        except ImportError:  # pragma: no cover
            from duckduckgo_search import DDGS  # type: ignore

        try:
            return DDGS(timeout=REQUEST_TIMEOUT)
        except TypeError:  # pragma: no cover - older library signature
            return DDGS()

    def _query(self, client: Any, query: str, max_results: int, page: int) -> list[dict]:
        is_real_ddgs = type(client).__module__.startswith(("ddgs", "duckduckgo_search"))

        if is_real_ddgs and self.backend == "google":
            try:
                jp_hits = _google_via_yahoo_jp(query, max_results=max_results, page=page)
                if jp_hits and _has_valid_site_hits(query, jp_hits):
                    return jp_hits
            except Exception:
                pass

        attempts = (
            {"backend": self.backend, "max_results": max_results, "page": page},
            {"backend": self.backend, "max_results": max_results},
        )
        last: Exception | None = None
        raw: list[dict] = []
        for kwargs in attempts:
            try:
                raw = list(client.text(query, **kwargs) or [])
                break
            except TypeError as exc:  # pragma: no cover - signature drift
                last = exc
            except Exception as exc:
                last = exc
                break

        if is_real_ddgs and (not raw or not _has_valid_site_hits(query, raw)):
            try:
                jp_hits = _google_via_yahoo_jp(query, max_results=max_results, page=page)
                if jp_hits and _has_valid_site_hits(query, jp_hits):
                    return jp_hits
            except Exception:
                pass

        if last is not None and not raw:
            raise last
        return raw

    def fetch(self, query: str, *, max_results: int, page: int) -> SearchResponse:
        started = time.monotonic()

        def elapsed() -> int:
            return int((time.monotonic() - started) * 1000)

        self.throttle.wait()
        try:
            with self._client() as client:
                raw = self._query(client, query, max_results, page)
        except Exception as exc:
            if any(marker in str(exc).lower() for marker in _EMPTY_MARKERS):
                # Not a failure: the engine answered, it just had nothing.
                return SearchResponse(
                    SearchOutcome.EMPTY, duration_ms=elapsed(), engine=self.name
                )
            outcome, detail = classify_exception(exc)
            return SearchResponse(
                outcome, detail=detail, duration_ms=elapsed(), engine=self.name
            )

        hits = dedupe_hits(
            {
                "title": hit.get("title") or "",
                "href": hit.get("href") or hit.get("link") or hit.get("url") or "",
                "body": hit.get("body") or hit.get("snippet") or hit.get("description") or "",
            }
            for hit in raw
        )[:max_results]

        if "site:linkedin.com" in (query or "").lower():
            hits = [h for h in hits if "linkedin.com" in (h.get("href") or "").lower()]

        if not hits:
            return SearchResponse(
                SearchOutcome.EMPTY, duration_ms=elapsed(), engine=self.name
            )
        return SearchResponse(
            SearchOutcome.OK, hits=hits, duration_ms=elapsed(), engine=self.name
        )


def _configured_backends() -> tuple[tuple[str, float], ...]:
    """Rotation from MARK_SEARCH_ENGINES, falling back to the verified set."""
    raw = (os.getenv("MARK_SEARCH_ENGINES") or "").strip()
    if not raw:
        return DEFAULT_BACKENDS
    known = dict(DEFAULT_BACKENDS)
    chosen: list[tuple[str, float]] = []
    for name in (part.strip().lower() for part in raw.split(",")):
        if name:
            chosen.append((name, known.get(name, 2.0)))
    return tuple(chosen) or DEFAULT_BACKENDS


def default_engines(*, pool: str = "") -> list[DdgsEngine]:
    """One throttle and one client per replica, so N copies really run in parallel.

    ``pool`` namespaces health and pacing so email lookups can run on their own
    clients instead of sharing discovery's throttle slots.
    """
    raw = os.getenv("MARK_ENGINE_REPLICAS", "3")
    try:
        replicas = max(1, min(8, int(raw)))
    except ValueError:
        replicas = 3
    engines: list[DdgsEngine] = []
    for backend, interval in _configured_backends():
        for replica in range(replicas):
            engines.append(DdgsEngine(backend, interval, replica=replica, pool=pool))
    return engines
