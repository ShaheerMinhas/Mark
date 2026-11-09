from __future__ import annotations

import re

_CITY = r"(?:(?:St|Mt|Ft)\.\s+)?[A-Z][a-z]+(?:[ -][A-Z][a-z]+){0,2}"
_STATE_NAME = r"[A-Z][a-z]+(?:[ -][A-Z][a-z]+)?"
_STATE_ABBR = (
    r"A[LKZR]|C[AOT]|D[EC]|FL|GA|HI|I[ADLN]|K[SY]|LA|M[ADEINOST]|"
    r"N[CDEHJMVY]|O[HKR]|PA|RI|S[CD]|T[NX]|UT|V[AT]|W[AIVY]"
)
_HQ_RE = re.compile(
    r"(?i)(?:headquartered in|headquarters(?:\s+in|\s+are in)?|head office|"
    r"\bhq\b|based in|located in)\s*[:\-]?\s*"
    r"([A-Z][A-Za-z '-]{2,40}(?:,\s*[A-Z][A-Za-z ']{1,30}){0,2})"
)
_US_GEO_RE = re.compile(rf"\b({_CITY},\s*{_STATE_NAME},\s*United States)\b")
_CITY_ST_RE = re.compile(rf"\b({_CITY},\s*(?:{_STATE_ABBR}))\b")
_METRO_RE = re.compile(rf"\b({_CITY} Metropolitan Area)\b")
_LABELED_RE = re.compile(
    r"(?i)(?:Location|Standort|Based in)\s*[:\-]?\s*([^·|\n]{2,50})"
)
_JUNK = re.compile(
    r"(?i)\b(linkedin|wikipedia|facebook|zoominfo|crunchbase|indeed|glassdoor|"
    r"click|website|official|profile|search|university|college|education|"
    r"experience|followers|connections|founded|as of|office locations|"
    r"will put|learn more|click here|drive|street|avenue|road|blvd|suite)\b"
)
_NOT_GEO = re.compile(
    r"(?i)\b(Inc\.?|LLC|Ltd\.?|LLP|Corp\.?|Co\.?|Director|Manager|Officer|"
    r"President|Engineer|Marketing|Chief|Founder|Advisor|SEO|PPC|CMO|CRM|"
    r"Brand|Growth)\b"
)
_NOT_PLACE = re.compile(
    r"(?i)^(united states|europe|asia|africa|remote|trinidad|tobago|nigeria|"
    r"india|pakistan|brazil|brasil|australia|germany|china|mexico|spain)$"
)
_CITY_WORDS = re.compile(r"^[A-Z][a-z]+(?:[ -][A-Z][a-z]+){0,2}$")


def _clean_location(raw: str) -> str:
    loc = re.sub(r"\s+", " ", raw or "").strip(" .;:-")
    loc = loc.split(".")[0].strip()
    loc = re.split(r"\s+[|·]\s+", loc)[0].strip()
    loc = re.split(r"\s+-\s+", loc)[0].strip()
    loc = re.split(r"\s+\b(?:and|with|where|which|that|for)\b", loc, maxsplit=1, flags=re.I)[0].strip()
    if len(loc) < 4 or len(loc) > 60:
        return ""
    if loc[:1].islower():
        return ""
    if loc.count(" ") >= 6 and "," not in loc:
        return ""
    if _JUNK.search(loc) or _NOT_GEO.search(loc) or _NOT_PLACE.search(loc):
        return ""
    if re.search(r"https?://|www\.", loc, re.I):
        return ""
    # SERP paste-ups like "Pakistan Jersey City, New Jersey, United States".
    countries = re.findall(
        r"(?i)\b(pakistan|india|united states|usa|canada|namibia|australia|"
        r"united kingdom|\buk\b|germany|china|brazil)\b",
        loc,
    )
    if len({c.lower() for c in countries}) >= 2:
        return ""
    return loc


def extract_profile_geo(text: str) -> str:
    """City/state from a LinkedIn SERP snippet, e.g. 'Decatur, Georgia, United States'."""
    if not text:
        return ""
    match = _LABELED_RE.search(text)
    if match:
        labeled = match.group(1)
        nested = extract_profile_geo(labeled) if labeled else ""
        if nested:
            return nested
        cleaned = _clean_location(labeled.split(".")[0])
        if cleaned and ("," in cleaned or _CITY_WORDS.match(cleaned)):
            return cleaned
    match = _US_GEO_RE.search(text)
    if match:
        cleaned = _clean_location(match.group(1))
        if cleaned:
            return cleaned
    match = _METRO_RE.search(text)
    if match:
        cleaned = _clean_location(match.group(1))
        if cleaned:
            return cleaned
    match = _CITY_ST_RE.search(text)
    if match:
        cleaned = _clean_location(match.group(1))
        if cleaned:
            return cleaned
    return ""


def extract_location(text: str) -> str:
    if not text:
        return ""
    match = _HQ_RE.search(text)
    if match:
        cleaned = _clean_location(match.group(1))
        if cleaned:
            return cleaned
    return extract_profile_geo(text)
