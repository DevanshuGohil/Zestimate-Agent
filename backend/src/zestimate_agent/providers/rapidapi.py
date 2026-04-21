"""RapidAPIProvider — calls the Zillow56 wrapper on RapidAPI.

Used as fallback when DirectProvider is blocked (403/429). RapidAPI data may
lag Zillow's live display, so DirectProvider remains primary for accuracy.

Endpoints:
    GET https://{host}/search?location={address}
    GET https://{host}/property?zpid={zpid}
"""

from __future__ import annotations

from typing import Any

import httpx
import structlog
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from ..models import Candidate, NormalizedAddress, PropertyDetail, ProviderError
from .base import Provider

log = structlog.get_logger(__name__)

_DEFAULT_TIMEOUT = 15


class RapidAPIProvider(Provider):
    name = "rapidapi"

    def __init__(self, api_key: str, host: str = "zillow56.p.rapidapi.com") -> None:
        self._api_key = api_key
        self._host = host
        self._base = f"https://{host}"
        self._headers = {
            "x-rapidapi-key": api_key,
            "x-rapidapi-host": host,
        }

    # ------------------------------------------------------------------ public

    async def search(self, normalized: NormalizedAddress) -> list[Candidate]:
        location = normalized.single_line()
        log.debug("rapidapi.search.start", location=location)
        data = await self._get("/search", {"location": location})
        return _parse_search(data)

    async def get_property(self, zpid: str) -> PropertyDetail:
        log.debug("rapidapi.property.start", zpid=zpid)
        data = await self._get("/property", {"zpid": zpid})
        return _parse_property(zpid, data)

    # --------------------------------------------------------------- internals

    @retry(
        retry=retry_if_exception_type(ProviderError),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=8),
        reraise=True,
    )
    async def _get(self, path: str, params: dict[str, str]) -> Any:
        url = self._base + path
        try:
            async with httpx.AsyncClient(timeout=_DEFAULT_TIMEOUT) as client:
                r = await client.get(url, params=params, headers=self._headers)
        except Exception as e:
            raise ProviderError(f"rapidapi network error {url}: {e}") from e

        if r.status_code == 429:
            raise ProviderError(f"rapidapi rate-limited: {url}")
        if r.status_code in (401, 403):
            raise ProviderError(f"rapidapi auth error {r.status_code}: {url}")
        if r.status_code != 200:
            raise ProviderError(f"rapidapi unexpected status {r.status_code}: {url}")

        try:
            data = r.json()
        except Exception as e:
            raise ProviderError(f"rapidapi bad JSON from {url}: {e}") from e

        if not data:
            raise ProviderError(f"rapidapi empty response from {url}")

        log.debug("rapidapi.get.ok", path=path, status=r.status_code)
        return data


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------


def _split_street(addr: str) -> tuple[str | None, str | None]:
    parts = addr.strip().split(None, 1)
    if not parts:
        return None, None
    number = parts[0] if parts[0][:1].isdigit() else None
    name = parts[1] if len(parts) > 1 else None
    return number, name


def _parse_search(data: Any) -> list[Candidate]:
    if not isinstance(data, dict):
        return []

    # Single property redirect (API returns the property dict directly)
    if "zpid" in data and "results" not in data:
        return [_candidate_from_property(data)]

    results = data.get("results") or []
    if not isinstance(results, list):
        return []

    return [_candidate_from_result(r) for r in results if r.get("zpid") is not None]


def _candidate_from_result(r: dict[str, Any]) -> Candidate:
    street = r.get("streetAddress") or r.get("address") or ""
    number, name = _split_street(street)
    return Candidate(
        zpid=str(r["zpid"]),
        street_number=number,
        street_name=name,
        city=r.get("city"),
        state=r.get("state"),
        zip5=(r.get("zipcode") or "")[:5] or None,
        lat=r.get("latitude"),
        lon=r.get("longitude"),
        raw=r,
    )


def _candidate_from_property(p: dict[str, Any]) -> Candidate:
    street = p.get("streetAddress") or ""
    number, name = _split_street(street)
    return Candidate(
        zpid=str(p["zpid"]),
        street_number=number,
        street_name=name,
        city=p.get("city"),
        state=p.get("state"),
        zip5=(p.get("zipcode") or "")[:5] or None,
        lat=p.get("latitude"),
        lon=p.get("longitude"),
        raw=p,
    )


def _parse_property(zpid: str, data: Any) -> PropertyDetail:
    if not isinstance(data, dict):
        raise ProviderError(f"rapidapi property response not a dict for zpid={zpid}")

    returned_zpid = str(data.get("zpid") or zpid)

    street = data.get("streetAddress") or ""
    city = data.get("city") or ""
    state = data.get("state") or ""
    zipcode = data.get("zipcode") or ""
    full_address = ", ".join(
        p for p in (street, city, f"{state} {zipcode}".strip()) if p
    )

    raw_zestimate = data.get("zestimate")
    zestimate: int | None = int(raw_zestimate) if raw_zestimate is not None else None

    raw_rent = data.get("rentZestimate")
    rent_zestimate: int | None = int(raw_rent) if raw_rent is not None else None

    return PropertyDetail(
        zpid_echo=returned_zpid,
        zestimate=zestimate,
        rent_zestimate=rent_zestimate,
        full_address=full_address,
        raw=data,
    )
