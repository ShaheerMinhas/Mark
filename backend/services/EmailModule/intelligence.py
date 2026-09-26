"""EmailEnrichmentService — evidence-based waterfall. Does not invent addresses."""

from __future__ import annotations

from backend.services.CompanyModule.web import company_website
from backend.services.EmailModule.extract import email_fits_person, is_plausible_email, name_parts
from backend.services.EmailModule.patterns import detect_pattern, generate_candidates, match_pattern
from backend.services.EmailModule.providers import configured_providers
from backend.services.EmailModule.public import discover_public_emails
from backend.services.EmailModule.store import EmailStore, MemoryEmailStore
from backend.services.EmailModule.types import (
    Confidence,
    EmailResult,
    EmailStatus,
    Evidence,
    PersonInput,
    Verification,
    VerifyStatus,
    not_found,
    utc_now,
)
from backend.services.EmailModule.verify import local_verify, normalize_provider_verify

_STRONG = {EmailStatus.FOUND.value, EmailStatus.VERIFIED.value}


def _confidence(*, status: str, samples: int = 0, verified: str = "", catch_all: bool = False) -> str:
    if status == EmailStatus.VERIFIED.value and not catch_all:
        return Confidence.HIGH.value
    if status == EmailStatus.FOUND.value:
        return Confidence.HIGH.value if verified != VerifyStatus.INVALID.value else Confidence.LOW.value
    if status == EmailStatus.INFERRED.value:
        if samples >= 3:
            return Confidence.MEDIUM.value
        if samples == 2:
            return Confidence.MEDIUM.value
        return Confidence.LOW.value
    if status == EmailStatus.VERIFY_UNCERTAIN.value:
        return Confidence.LOW.value
    if status == EmailStatus.UNVERIFIED.value:
        return Confidence.MEDIUM.value
    return Confidence.LOW.value


def _apply_verify(result: EmailResult, payload: dict[str, str], *, from_provider: bool) -> EmailResult:
    result.verification = Verification(
        status=payload.get("status") or VerifyStatus.UNKNOWN.value,
        checked_at=payload.get("checked_at") or utc_now(),
        provider=payload.get("provider") or "",
        mx=payload.get("mx") or "",
        detail=payload.get("detail") or "",
    )
    result.last_verified_at = result.verification.checked_at
    vstat = result.verification.status
    if vstat == VerifyStatus.VALID.value and from_provider:
        result.status = EmailStatus.VERIFIED.value
    elif vstat == VerifyStatus.ACCEPT_ALL.value:
        if result.status != EmailStatus.FOUND.value:
            result.status = EmailStatus.VERIFY_UNCERTAIN.value
        result.notes = (result.notes + " catch-all domain").strip()
    elif vstat == VerifyStatus.INVALID.value:
        if result.status == EmailStatus.FOUND.value:
            result.confidence = Confidence.LOW.value
            result.notes = (result.notes + " verifier said invalid").strip()
        else:
            result.email = None
            result.status = EmailStatus.INVALID.value
    elif vstat in {VerifyStatus.DISPOSABLE.value, VerifyStatus.ROLE_BASED.value}:
        result.email = None
        result.status = EmailStatus.INVALID.value
    elif result.status == EmailStatus.FOUND.value:
        result.status = EmailStatus.FOUND.value
    elif result.status == EmailStatus.INFERRED.value:
        result.status = EmailStatus.INFERRED.value
    elif result.status == EmailStatus.VERIFIED.value:
        result.status = EmailStatus.VERIFIED.value
    else:
        result.status = EmailStatus.UNVERIFIED.value
    return result


class EmailEnrichmentService:
    def __init__(
        self,
        store: EmailStore | None = None,
        providers: list | None = None,
        *,
        cache_days: int = 30,
    ) -> None:
        self.store = store or MemoryEmailStore(cache_days=cache_days)
        self._providers = providers

    def providers(self):
        if self._providers is not None:
            return [p for p in self._providers if p.available()]
        return configured_providers()

    def enrich(
        self,
        person: PersonInput,
        *,
        use_providers: bool = False,
        use_public_web: bool = True,
        snippets_only: bool = False,
        verify: bool = True,
        force: bool = False,
    ) -> EmailResult:
        first, last = name_parts(person.name)
        first = person.first_name or first
        last = person.last_name or last
        domain = (person.company_domain or "").strip().lower().removeprefix("www.")

        if not force:
            cached = self.store.get_cached(person.name, person.company_name, domain)
            if cached and cached.status == EmailStatus.VERIFIED.value:
                return cached
            prior = self.store.prior_person_email(person.name, person.company_name)
            if prior and prior.status == EmailStatus.VERIFIED.value:
                prior.cached = True
                return prior

        result = not_found()
        result.domain = domain
        result.discovered_at = utc_now()

        if not domain and person.company_name and use_public_web:
            domain, website = company_website(person.company_name)
            result.domain = domain
            if website:
                result.evidence.append(Evidence(type="COMPANY_DOMAIN", url=website, detail="company website"))

        public_hits = []
        if person.snippet:
            public_hits = discover_public_emails(
                person.name,
                company=person.company_name,
                domain=domain,
                snippet=person.snippet,
                snippets_only=True,
            )
        if use_public_web:
            public_hits.extend(
                discover_public_emails(
                    person.name,
                    company=person.company_name,
                    domain=domain,
                    snippet="",
                    snippets_only=False,
                )
            )
        for hit in public_hits:
            email = hit["email"]
            if not is_plausible_email(email):
                continue
            if person.name and not email_fits_person(email, person.name):
                continue
            result.email = email
            result.status = EmailStatus.FOUND.value
            result.source = "public_web" if hit.get("url") else "snippet"
            result.source_url = hit.get("url") or ""
            result.evidence.append(
                Evidence(
                    type="PUBLIC_WEB" if hit.get("url") else "SNIPPET",
                    url=hit.get("url") or "",
                    observed_at=utc_now(),
                    detail=hit.get("detail") or "",
                )
            )
            self.store.add_evidence(
                lead_id=person.lead_id,
                email=email,
                type=result.evidence[-1].type,
                url=result.source_url,
                detail=hit.get("detail") or "",
            )
            patt = match_pattern(email, first, last)
            if patt and domain:
                result.pattern = patt
                self._remember_pattern(domain, person.name, email)
            break

        if result.status not in _STRONG:
            inferred = self._infer(person.name, domain, first, last)
            if inferred:
                result = inferred

        if result.status not in _STRONG and use_providers:
            provided = self._from_providers(person, first, last, domain)
            if provided:
                result = provided

        if result.email and verify:
            local = local_verify(result.email)
            result = _apply_verify(result, local, from_provider=False)
            if (
                use_providers
                and result.email
                and result.source != "findymail"
                and result.status
                not in {EmailStatus.INVALID.value, EmailStatus.NOT_FOUND.value, EmailStatus.VERIFIED.value}
            ):
                result = self._provider_verify(result, person)

        if result.email and result.status != EmailStatus.INVALID.value:
            samples = 0
            if domain:
                row = self.store.get_pattern(domain) or {}
                samples = int(row.get("sample_count") or 0)
            catch_all = bool(
                result.verification and result.verification.status == VerifyStatus.ACCEPT_ALL.value
            )
            result.confidence = _confidence(
                status=result.status,
                samples=samples,
                verified=(result.verification.status if result.verification else ""),
                catch_all=catch_all,
            )
        else:
            if result.status == EmailStatus.INVALID.value:
                result.email = None
                result.status = EmailStatus.NOT_FOUND.value
                result.notes = (result.notes + " dropped invalid candidate").strip()
            result.confidence = None

        self.store.save_cached(person.name, person.company_name, domain, result)
        return result

    def _remember_pattern(self, domain: str, name: str, email: str) -> None:
        examples = self.store.domain_examples(domain) + [(name, email)]
        pattern, confidence, count = detect_pattern(examples)
        if pattern:
            self.store.observe_pattern(domain, pattern, email, confidence, count)

    def _infer(self, name: str, domain: str, first: str, last: str) -> EmailResult | None:
        if not domain or not first:
            return None
        row = self.store.get_pattern(domain)
        examples = self.store.domain_examples(domain)
        pattern, confidence, count = detect_pattern(examples)
        if row and not pattern:
            pattern = str(row.get("pattern") or "")
            confidence = float(row.get("confidence") or 0)
            count = int(row.get("sample_count") or 0)
        if not pattern:
            return None
        candidates = generate_candidates(name, domain, [(pattern, confidence)])
        if not candidates:
            return None
        item = candidates[0]
        status = EmailStatus.INFERRED.value
        if count < 1:
            return None
        result = EmailResult(
            email=str(item["email"]),
            status=status,
            source="pattern",
            pattern=pattern,
            pattern_confidence=float(item["pattern_confidence"]),
            domain=domain,
            discovered_at=utc_now(),
            notes=f"inferred from {count} matching employee(s)",
            evidence=[
                Evidence(
                    type="PATTERN",
                    observed_at=utc_now(),
                    detail=f"{pattern} samples={count} confidence={confidence}",
                )
            ],
        )
        result.confidence = _confidence(status=status, samples=count)
        return result

    def _from_providers(self, person: PersonInput, first: str, last: str, domain: str) -> EmailResult | None:
        target = (domain or person.company_name or "").strip()
        if not target:
            return None
        for provider in self.providers():
            found = provider.find_email(
                name=person.name,
                first=first,
                last=last,
                domain=target,
                company=person.company_name,
            )
            self.store.log_usage(
                provider=provider.name,
                operation="find",
                success=bool(found.email),
                credits=found.credits,
                lead_id=person.lead_id,
                search_id=person.search_id,
                http_status=found.http_status,
                detail=found.detail,
            )
            if found.http_status == 429:
                continue
            if not found.email or not is_plausible_email(found.email):
                continue
            host = found.email.split("@", 1)[-1].lower() if "@" in found.email else domain
            already_verified = provider.name == "findymail"
            result = EmailResult(
                email=found.email,
                status=EmailStatus.VERIFIED.value if already_verified else EmailStatus.UNVERIFIED.value,
                source=provider.name,
                domain=host or domain,
                discovered_at=utc_now(),
                evidence=[
                    Evidence(
                        type="PROVIDER",
                        provider=provider.name,
                        observed_at=utc_now(),
                        detail=found.detail or ("verified finder" if already_verified else "finder"),
                    )
                ],
            )
            patt = match_pattern(found.email, first, last)
            if patt:
                result.pattern = patt
                self._remember_pattern(result.domain, person.name, found.email)
            return result
        return None

    def _provider_verify(self, result: EmailResult, person: PersonInput) -> EmailResult:
        if not result.email:
            return result
        for provider in self.providers():
            checked = provider.verify_email(result.email)
            self.store.log_usage(
                provider=provider.name,
                operation="verify",
                success=checked.status == VerifyStatus.VALID.value,
                credits=checked.credits,
                lead_id=person.lead_id,
                search_id=person.search_id,
                http_status=checked.http_status,
                detail=checked.detail,
            )
            if checked.http_status == 429:
                continue
            payload = {
                "status": normalize_provider_verify(checked.status),
                "checked_at": utc_now(),
                "provider": provider.name,
                "detail": checked.detail,
            }
            self.store.save_verification(
                email=result.email,
                lead_id=person.lead_id,
                provider=provider.name,
                status=payload["status"],
                detail=checked.detail,
            )
            return _apply_verify(result, payload, from_provider=True)
        return result


def enrich_person(person: PersonInput, **kwargs) -> EmailResult:
    return EmailEnrichmentService().enrich(person, **kwargs)
