"""Contact enrichment for people a discovery run just stored.

Public company pages come first, through the pinned fetch and a fail-closed
robots.txt check. A pattern is stored only when the domain has MX records, and
it is never labeled verified. Paid adapters run only after that, and only when
the existing credential and budget gates already allow a call.
"""

from __future__ import annotations

import os
import re
from datetime import datetime
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.models import ContactEnrichmentJob, GlobalEvidence, GlobalPerson
from backend.services.discovery_engine.ssrf import SsrfError, pinned_opener, public_http_url, safe_fetch
from backend.services.discovery_engine.suppression import is_suppressed
from backend.services.global_cache.policy import load_rules, quality_tier

_PATHS = ("/contact", "/contact-us", "/about", "/about-us", "/team")
_UA = "MarkBot/1.0"
_EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_PHONE = re.compile(r"(?:\+\d{1,3}[\s.\-]?)?(?:\(?\d{2,4}\)?[\s.\-]?)\d{3,4}[\s.\-]\d{3,4}")
_ROLE_LOCALS = {
    "info",
    "hello",
    "contact",
    "sales",
    "support",
    "admin",
    "office",
    "team",
    "enquiries",
    "careers",
    "press",
    "media",
    "billing",
    "hr",
    "jobs",
}
_BLOCKED_HOSTS = {
    "linkedin.com",
    "facebook.com",
    "instagram.com",
    "twitter.com",
    "x.com",
    "duckduckgo.com",
    "google.com",
}


def network_disabled() -> bool:
    return os.environ.get("MARK_DISABLE_CONTACT_NETWORK") == "1"


def lookup_mx(domain: str) -> list[str]:
    """Return MX hostnames. An empty list means the domain did not prove it accepts mail."""
    if network_disabled():
        return []
    import dns.resolver

    try:
        answers = dns.resolver.resolve(domain, "MX")
    except Exception:
        return []
    return [str(item.exchange).rstrip(".") for item in answers]


def _company_hosts(domain: str) -> set[str] | None:
    host = (domain or "").strip().lower().removeprefix("www.").strip(".")
    if not host or "." not in host:
        return None
    if any(host == blocked or host.endswith("." + blocked) for blocked in _BLOCKED_HOSTS):
        return None
    return {host, f"www.{host}"}


def pinned_site_fetch(url: str, allowed_hosts: set[str]) -> bytes:
    if network_disabled():
        raise SsrfError("contact fetch is disabled in this process")
    return safe_fetch(
        url,
        opener=pinned_opener,
        allowed_hosts=allowed_hosts,
        timeout=8,
        max_bytes=200_000,
    )


def _allowed(robots_body: str, url: str) -> bool:
    parser = RobotFileParser()
    parser.parse((robots_body or "").splitlines())
    try:
        return bool(parser.can_fetch(_UA, url))
    except Exception:
        return False


def _digits(value: str) -> str:
    return re.sub(r"\D", "", value or "")


def _phones_near(text: str, name: str) -> str | None:
    lowered = text.lower()
    needle = " ".join(name.lower().split())
    if not needle or needle not in lowered:
        return None
    at = lowered.find(needle)
    window = text[max(0, at - 240) : at + len(needle) + 240]
    for match in _PHONE.finditer(window):
        if 10 <= len(_digits(match.group(0))) <= 15:
            return " ".join(match.group(0).split())
    return None


def _local_matches(local: str, first: str, last: str) -> bool:
    local = re.sub(r"[^a-z0-9]", "", local.lower())
    first = re.sub(r"[^a-z]", "", first.lower())
    last = re.sub(r"[^a-z]", "", last.lower())
    if not first or not last or not local:
        return False
    candidates = {
        f"{first}{last}",
        f"{first[0]}{last}",
        f"{first}{last[0]}",
        f"{last}{first}",
    }
    return local in candidates


def published_contact(html: str, *, name: str, domain: str) -> tuple[str | None, str | None]:
    """Return a published personal email and phone. Role inboxes are not a person's email."""
    text = re.sub(r"<[^>]+>", " ", html or "")
    text = " ".join(text.split())
    parts = [part for part in re.split(r"\s+", name.strip()) if part]
    first = parts[0] if parts else ""
    last = parts[-1] if len(parts) > 1 else ""
    email = None
    host = domain.lower().removeprefix("www.")
    if first and last and f"{first} {last}".lower() in text.lower():
        for match in _EMAIL.findall(text):
            local, _, mail_host = match.partition("@")
            if mail_host.lower().removeprefix("www.") != host:
                continue
            if local.lower().split(".")[0] in _ROLE_LOCALS:
                continue
            if _local_matches(local, first, last):
                email = match.lower()
                break
    phone = _phones_near(text, f"{first} {last}".strip()) if first and last else None
    return email, phone


def collect_published(domain: str, name: str, fetch) -> tuple[str | None, str | None, str, str]:
    hosts = _company_hosts(domain)
    if hosts is None:
        return None, None, "", "No company domain was stored, so no website was fetched."
    origin = f"https://{domain.lower().removeprefix('www.')}"
    try:
        robots = fetch(f"{origin}/robots.txt")
    except SsrfError as exc:
        return None, None, "", f"robots.txt was not fetched ({exc}). No contact page was opened."
    robots_text = robots.decode("utf-8", errors="replace") if isinstance(robots, bytes) else str(robots)
    if not robots_text.strip():
        return None, None, "", "robots.txt was empty. No contact page was opened."
    for path in _PATHS:
        url = origin + path
        if not _allowed(robots_text, url):
            continue
        try:
            page = fetch(url)
        except SsrfError:
            continue
        html = page.decode("utf-8", errors="replace") if isinstance(page, bytes) else str(page)
        email, phone = published_contact(html, name=name, domain=domain)
        if email or phone:
            return email, phone, url, "Published on the company website. Not a provider verification."
    return None, None, "", "Company pages did not publish an email or phone for this person."


def _pattern_email(name: str, domain: str) -> str | None:
    parts = [re.sub(r"[^a-z]", "", part.lower()) for part in name.split() if part.strip()]
    if len(parts) < 2:
        return None
    first, last = parts[0], parts[-1]
    if not first or not last:
        return None
    return f"{first}.{last}@{domain.lower().removeprefix('www.')}"


def _label(person: GlobalPerson, *, email: str | None, status: str | None, confidence: str | None, note: str, phone: str | None, source_url: str | None, provider: str, db: Session) -> None:
    if is_suppressed(db, email=email, domain=person.domain, profile_url=person.profile_url):
        return
    now = datetime.utcnow()
    if email and status:
        person.email = email
        person.email_status = status
        person.email_confidence = confidence
        person.contact_note = note
        if status == "verified":
            person.verified_at = now
    if phone and not person.phone:
        person.phone = phone
    rules = load_rules(db)
    tier, score = quality_tier(
        rules=rules,
        email=person.email,
        email_status=person.email_status,
        has_identity=bool(person.profile_url_normalized or person.email),
        has_role=bool(person.title or person.job_function),
    )
    if person.email_status == "inferred" and tier in {"verified", "contactable"}:
        tier, score = "enriched", score
    person.quality_tier = tier
    person.quality_score = score
    person.enriched_at = now
    person.updated_at = now
    if email or phone:
        db.add(
            GlobalEvidence(
                subject_type="person",
                subject_id=person.id,
                field_name="email" if email else "phone",
                value_text=note[:1000],
                source_url=public_http_url(source_url),
                provider=provider,
                share_scope="public",
                eligible=True,
                observed_at=now,
            )
        )


def enrich_person(db: Session, person: GlobalPerson, *, fetch, mx_lookup) -> str:
    if is_suppressed(db, email=person.email, domain=person.domain, profile_url=person.profile_url):
        return "Suppressed. No page was fetched and no provider was called."
    if person.email and person.email_status == "verified":
        return "Already verified. No further guess was stored."
    email, phone, source, note = collect_published(person.domain or "", person.full_name or "", fetch)
    if email or phone:
        _label(
            person,
            email=email,
            status="found" if email else person.email_status,
            confidence="published" if email else person.email_confidence,
            note=note,
            phone=phone,
            source_url=source or None,
            provider="company_site",
            db=db,
        )
        return note
    if person.email and person.email_status not in {None, "", "unknown", "not_found"}:
        return "An address was already stored. No pattern was guessed."
    guessed = _pattern_email(person.full_name or "", person.domain or "")
    if guessed is None:
        return "No published contact, and the name or domain is not enough for a pattern."
    if is_suppressed(db, email=guessed, domain=person.domain, profile_url=person.profile_url):
        return "The pattern address is suppressed. It was not stored."
    hosts = mx_lookup(person.domain or "")
    if not hosts:
        return "No published contact. The domain has no MX records, so the pattern was not stored."
    _label(
        person,
        email=guessed,
        status="inferred",
        confidence="mx-pattern",
        note="Pattern guess. The domain has MX records. The mailbox was not verified.",
        phone=None,
        source_url=None,
        provider="mx_pattern",
        db=db,
    )
    return "Pattern guess stored as inferred. The mailbox was not verified."


def queue_contact_jobs(db: Session, person_ids: list[int], run_id: int | None) -> int:
    created = 0
    for person_id in person_ids:
        if db.query(ContactEnrichmentJob).filter(
            ContactEnrichmentJob.person_id == person_id,
            ContactEnrichmentJob.run_id == run_id,
        ).first():
            continue
        try:
            with db.begin_nested():
                db.add(ContactEnrichmentJob(person_id=person_id, run_id=run_id, status="queued"))
                db.flush()
            created += 1
        except IntegrityError:
            continue
    db.commit()
    return created


def _default_fetch_for(domain: str):
    hosts = _company_hosts(domain) or set()

    def fetch(url: str) -> bytes:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        if host not in hosts:
            raise SsrfError("host is outside the company site")
        return pinned_site_fetch(url, hosts)

    return fetch


def process_contact_jobs(db: Session, run_id: int | None, *, fetch=None, mx_lookup=None) -> str:
    """Run queued public enrichment. Does not call a paid provider."""
    from backend.services.discovery_engine.settings import ensure_settings

    jobs = (
        db.query(ContactEnrichmentJob)
        .filter(ContactEnrichmentJob.run_id == run_id, ContactEnrichmentJob.status == "queued")
        .all()
    )
    if not jobs:
        return "No contact jobs were queued."
    settings = ensure_settings(db)
    published = inferred = empty = 0
    for job in jobs:
        person = db.get(GlobalPerson, job.person_id)
        if person is None:
            job.status = "skipped"
            job.detail = "Person missing."
            job.finished_at = datetime.utcnow()
            continue
        reader = fetch or _default_fetch_for(person.domain or "")
        detail = enrich_person(db, person, fetch=reader, mx_lookup=mx_lookup or lookup_mx)
        if person.email_status == "found" and person.email_confidence == "published":
            published += 1
        elif person.email_status == "inferred":
            inferred += 1
        else:
            empty += 1
        if not settings.store_name_only and not person.email and not person.phone and person.archived_at is None:
            person.archived_at = datetime.utcnow()
            person.correction_note = "Name-only storage is off. No contact was published."
            detail = detail + " Name-only storage is off, so this person was archived."
        job.status = "done"
        job.detail = detail[:2000]
        job.finished_at = datetime.utcnow()
    db.commit()
    return (
        f"Contact jobs {len(jobs)}. Published {published}. Inferred after MX {inferred}. "
        f"Still without a new address {empty}. Paid providers were not called by this step."
    )
