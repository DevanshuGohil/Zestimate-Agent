"""Stage 1: free-text US address → NormalizedAddress.

Provider pipeline (first success wins):
  1. Nominatim (OpenStreetMap)    — free, no key, 1 req/s rate limit
  2. Google Maps Geocoding API    — requires GOOGLE_MAPS_API_KEY in env
  3. Regex fallback               — offline, returns MEDIUM confidence

If all providers yield LOW confidence or fail, AmbiguousAddressError is raised
with any candidates found so the agent can ask the user to clarify.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

import httpx
import structlog

# Shared httpx client — set by the API lifespan for connection reuse.
# None (default) causes _try_nominatim to open a fresh client per call,
# which is fine for CLI/tests but wasteful under server load.
_shared_http_client: httpx.AsyncClient | None = None


def set_shared_http_client(client: httpx.AsyncClient | None) -> None:
    global _shared_http_client
    _shared_http_client = client

from .config import Settings, get_settings
from .models import (
    AmbiguousAddressError,
    Confidence,
    NormalizedAddress,
)

log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

_ZIP_RE = re.compile(r"\b(\d{5})(?:-\d{4})?\b")

# Shared by resolve.py and validate.py for consistent street-name comparison.
_SUFFIX_EXPAND: dict[str, str] = {
    "ST": "STREET", "AVE": "AVENUE", "BLVD": "BOULEVARD", "DR": "DRIVE",
    "LN": "LANE", "RD": "ROAD", "CT": "COURT", "PL": "PLACE",
    "CIR": "CIRCLE", "TRL": "TRAIL", "PKWY": "PARKWAY", "HWY": "HIGHWAY",
    "FWY": "FREEWAY", "WAY": "WAY", "EXPY": "EXPRESSWAY", "SQ": "SQUARE",
    "BLDG": "BUILDING", "APT": "APARTMENT",
}


def expand_suffixes(name: str) -> str:
    """Expand USPS street-suffix abbreviations for consistent fuzzy comparison."""
    return " ".join(_SUFFIX_EXPAND.get(t, t) for t in name.upper().split())


async def normalize_address(
    raw: str,
    settings: Settings | None = None,
) -> NormalizedAddress:
    """Normalize a free-text US address.

    Returns a NormalizedAddress with confidence HIGH or MEDIUM.
    Raises AmbiguousAddressError if the address is ambiguous or unparseable.
    """
    if settings is None:
        settings = get_settings()

    raw = raw.strip()
    log.info("normalize.start", raw=raw)

    # Extract any zip from the raw string; we'll prefer it over geocoder output
    # because geocoders sometimes assign a nearby zip rather than the exact one.
    input_zip = _extract_zip(raw)

    # 1. Nominatim
    try:
        result = await _try_nominatim(raw, input_zip)
        if result:
            if result.confidence == Confidence.LOW:
                raise AmbiguousAddressError(
                    f"Nominatim returned LOW confidence for {raw!r}; "
                    "address too vague to resolve",
                )
            log.info("normalize.ok", provider="nominatim", confidence=result.confidence)
            return result
    except AmbiguousAddressError:
        raise
    except Exception as e:
        log.warning("normalize.nominatim_failed", error=str(e))

    # 2. Google Maps (skip if not configured)
    if settings.google_maps_api_key:
        try:
            result = await _try_google(raw, settings.google_maps_api_key.get_secret_value(), input_zip)
            if result:
                log.info("normalize.ok", provider="google", confidence=result.confidence)
                return result
        except Exception as e:
            log.warning("normalize.google_failed", error=str(e))

    # 3. Regex fallback
    result = _try_regex(raw)
    if result:
        log.info("normalize.ok", provider="regex", confidence=result.confidence)
        return result

    raise AmbiguousAddressError(
        f"all providers failed to normalize {raw!r}",
        candidates=[],
    )


# ---------------------------------------------------------------------------
# Provider: Nominatim (OpenStreetMap)
# ---------------------------------------------------------------------------

_NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
_NOMINATIM_HEADERS = {
    "User-Agent": "ZestimateAgent/0.1 (educational/research project)"
}
_NOMINATIM_RATE_SLEEP = 1.0  # seconds — Nominatim ToS: max 1 req/s

# Types that indicate a specific house/building address
_HIGH_TYPES = {("place", "house"), ("building", None)}


async def _try_nominatim(raw: str, input_zip: str | None = None) -> NormalizedAddress | None:
    await asyncio.sleep(_NOMINATIM_RATE_SLEEP)
    params = {
        "q": raw,
        "addressdetails": "1",
        "format": "json",
        "countrycodes": "us",
        "limit": "5",
    }
    if _shared_http_client is not None:
        resp = await _shared_http_client.get(
            _NOMINATIM_URL, params=params, headers=_NOMINATIM_HEADERS, timeout=15
        )
        resp.raise_for_status()
    else:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                _NOMINATIM_URL, params=params, headers=_NOMINATIM_HEADERS, timeout=15
            )
            resp.raise_for_status()
    results: list[dict[str, Any]] = resp.json()

    if not results:
        log.debug("normalize.nominatim.empty")
        return None

    first = results[0]
    if not first.get("address", {}).get("house_number"):
        log.debug("normalize.nominatim.no_house_number", display=first.get("display_name"))
        return None

    # Detect multi-state ambiguity
    states = {
        _state_abbr(r.get("address", {}).get("state", ""))
        for r in results
        if r.get("address", {}).get("house_number")
    }
    states.discard(None)
    if len(states) > 1:
        log.debug("normalize.nominatim.ambiguous_states", states=states)
        raise AmbiguousAddressError(
            f"address matches properties in multiple states ({', '.join(sorted(states))}); "
            "please include zip code to disambiguate",
        )

    return _parse_nominatim(first, input_zip)


def _parse_nominatim(hit: dict[str, Any], input_zip: str | None) -> NormalizedAddress:
    addr = hit.get("address", {})
    house = addr.get("house_number", "")
    road = addr.get("road", "")
    city = (
        addr.get("city")
        or addr.get("town")
        or addr.get("village")
        or addr.get("suburb")
        or ""
    )
    state_full = addr.get("state", "")
    state = _state_abbr(state_full) or state_full[:2].upper()
    zip5 = input_zip or (addr.get("postcode") or "")[:5]

    cls = hit.get("class", "")
    typ = hit.get("type", "")
    if (cls, typ) in _HIGH_TYPES or (cls, None) in _HIGH_TYPES or typ == "house":
        confidence = Confidence.HIGH
    elif house:
        confidence = Confidence.MEDIUM
    else:
        confidence = Confidence.LOW

    number, name = _split_number_name(f"{house} {road}".strip())

    return NormalizedAddress(
        street_number=number or house,
        street_name=name or road,
        city=city,
        state=state,
        zip5=zip5 or "00000",
        lat=float(hit["lat"]) if hit.get("lat") else None,
        lon=float(hit["lon"]) if hit.get("lon") else None,
        confidence=confidence,
    )


# ---------------------------------------------------------------------------
# Provider: Google Maps Geocoding API
# ---------------------------------------------------------------------------

_GOOGLE_GEOCODE_URL = "https://maps.googleapis.com/maps/api/geocode/json"


async def _try_google(
    raw: str, api_key: str, input_zip: str | None = None
) -> NormalizedAddress | None:
    params = {"address": raw, "key": api_key, "components": "country:US"}
    if _shared_http_client is not None:
        resp = await _shared_http_client.get(_GOOGLE_GEOCODE_URL, params=params, timeout=10)
        resp.raise_for_status()
    else:
        async with httpx.AsyncClient() as client:
            resp = await client.get(_GOOGLE_GEOCODE_URL, params=params, timeout=10)
            resp.raise_for_status()

    data = resp.json()
    api_status = data.get("status")

    if api_status == "ZERO_RESULTS":
        log.debug("normalize.google.no_results", raw=raw)
        return None
    if api_status != "OK":
        raise ValueError(f"Google Geocoding API returned status={api_status!r}")

    results = data.get("results", [])
    if not results:
        return None

    parsed = _parse_google(results[0], input_zip)
    if parsed.street_number is None:
        log.debug("normalize.google.no_street_number", raw=raw)
        return None
    return parsed


def _parse_google(result: dict[str, Any], input_zip: str | None) -> NormalizedAddress:
    by_type: dict[str, dict[str, str]] = {}
    for comp in result.get("address_components", []):
        for t in comp.get("types", []):
            if t not in by_type:
                by_type[t] = comp  # type: ignore[assignment]

    street_number = (by_type.get("street_number") or {}).get("long_name")
    route = (by_type.get("route") or {}).get("long_name")
    city = (
        by_type.get("locality")
        or by_type.get("sublocality_level_1")
        or by_type.get("administrative_area_level_3")
        or {}
    ).get("long_name")
    state = (by_type.get("administrative_area_level_1") or {}).get("short_name")
    postal_code = ((by_type.get("postal_code") or {}).get("short_name") or "")[:5]

    zip5 = input_zip or postal_code or "00000"
    location = result.get("geometry", {}).get("location", {})

    types = result.get("types", [])
    confidence = Confidence.HIGH if "street_address" in types or "premise" in types else Confidence.MEDIUM

    return NormalizedAddress(
        street_number=street_number,
        street_name=route,
        city=city,
        state=state,
        zip5=zip5,
        lat=location.get("lat"),
        lon=location.get("lng"),
        confidence=confidence,
    )


# ---------------------------------------------------------------------------
# Provider: Regex fallback
# ---------------------------------------------------------------------------

# Matches: "123 Main St, Springfield, IL 62701"
# Also: "123 Main St Springfield IL 62701" (no commas)
_ADDR_RE = re.compile(
    r"^(?P<number>\d+[\w-]?)\s+"
    r"(?P<street>[^,]+?)\s*[,\s]\s*"
    r"(?P<city>[^,]+?)\s*[,\s]\s*"
    r"(?P<state>[A-Za-z]{2})\s+"
    r"(?P<zip>\d{5})(?:-\d{4})?\s*$",
    re.IGNORECASE,
)


def _try_regex(raw: str) -> NormalizedAddress | None:
    m = _ADDR_RE.match(raw.strip())
    if not m:
        return None
    number = m.group("number")
    street = m.group("street").strip()
    city = m.group("city").strip()
    state = m.group("state").upper()
    zip5 = m.group("zip")

    if state not in _STATE_TO_ABBR.values() and state not in _STATE_TO_ABBR:
        return None

    return NormalizedAddress(
        street_number=number,
        street_name=street,
        city=city,
        state=state,
        zip5=zip5,
        confidence=Confidence.MEDIUM,
    )


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _extract_zip(raw: str) -> str | None:
    m = _ZIP_RE.search(raw)
    return m.group(1) if m else None


def _split_number_name(addr: str) -> tuple[str | None, str | None]:
    parts = addr.strip().split(None, 1)
    if not parts:
        return None, None
    number = parts[0] if parts[0][:1].isdigit() else None
    name = parts[1] if len(parts) > 1 else (None if number else parts[0])
    return number, name


_STATE_TO_ABBR: dict[str, str] = {
    "Alabama": "AL", "Alaska": "AK", "Arizona": "AZ", "Arkansas": "AR",
    "California": "CA", "Colorado": "CO", "Connecticut": "CT",
    "Delaware": "DE", "Florida": "FL", "Georgia": "GA", "Hawaii": "HI",
    "Idaho": "ID", "Illinois": "IL", "Indiana": "IN", "Iowa": "IA",
    "Kansas": "KS", "Kentucky": "KY", "Louisiana": "LA", "Maine": "ME",
    "Maryland": "MD", "Massachusetts": "MA", "Michigan": "MI",
    "Minnesota": "MN", "Mississippi": "MS", "Missouri": "MO",
    "Montana": "MT", "Nebraska": "NE", "Nevada": "NV",
    "New Hampshire": "NH", "New Jersey": "NJ", "New Mexico": "NM",
    "New York": "NY", "North Carolina": "NC", "North Dakota": "ND",
    "Ohio": "OH", "Oklahoma": "OK", "Oregon": "OR", "Pennsylvania": "PA",
    "Rhode Island": "RI", "South Carolina": "SC", "South Dakota": "SD",
    "Tennessee": "TN", "Texas": "TX", "Utah": "UT", "Vermont": "VT",
    "Virginia": "VA", "Washington": "WA", "West Virginia": "WV",
    "Wisconsin": "WI", "Wyoming": "WY",
    "District of Columbia": "DC", "Washington DC": "DC",
    "Washington D.C.": "DC", "Puerto Rico": "PR",
}


def _state_abbr(name: str) -> str | None:
    if not name:
        return None
    if len(name) == 2:
        return name.upper()
    return _STATE_TO_ABBR.get(name)
