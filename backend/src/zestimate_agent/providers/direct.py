"""DirectProvider — scrapes zillow.com via curl_cffi + parses __NEXT_DATA__.

This is the primary provider for the agent. It fetches the same HTML the
browser renders, so `property.zestimate` in the parsed payload is exactly
the value Zillow displays on its site — matching the spec's accuracy metric.

Zillow routes:
    /homes/{address-hyphenated}_rb/        — search / specific-address redirect
    /homedetails/{zpid}_zpid/              — by-zpid detail (auto-redirects
                                             to canonical slug URL)

The JSON blob embedded under `<script id="__NEXT_DATA__">` contains
`props.pageProps.componentProps.gdpClientCache` — itself a JSON string whose
single top-level value has `{property, viewer, abTests}`. Property holds
every field we care about.
"""

from __future__ import annotations

import json
import re
from typing import Any

import structlog
from curl_cffi.requests import AsyncSession
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from ..circuit_breaker import CircuitOpen, ProviderCircuitBreaker
from ..models import Candidate, NormalizedAddress, PropertyDetail, ProviderError
from .base import Provider

# Shared across all DirectProvider instances — reflects health of Zillow as a whole.
# Opens after 5 consecutive post-retry failures; fast-fails for 60 s before retrying.
_zillow_breaker = ProviderCircuitBreaker(fail_max=5, reset_timeout=60.0)

log = structlog.get_logger(__name__)

_NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.DOTALL
)

_DEFAULT_IMPERSONATE = "chrome124"
_DEFAULT_TIMEOUT = 20
_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Upgrade-Insecure-Requests": "1",
}


class DirectProvider(Provider):
    name = "direct"

    def __init__(
        self,
        proxy_url: str | None = None,
        timeout: int = _DEFAULT_TIMEOUT,
        impersonation: str = _DEFAULT_IMPERSONATE,
    ) -> None:
        self._proxies: dict[str, str] | None = (
            {"http": proxy_url, "https": proxy_url} if proxy_url else None
        )
        self._timeout = timeout
        self._impersonation = impersonation

    # ------------------------------------------------------------------ public

    async def search(self, normalized: NormalizedAddress) -> list[Candidate]:
        """Search by full address string.

        For a specific street address Zillow typically 301-redirects the
        `_rb` URL straight to the homedetails page — we detect that and
        return a single candidate. Otherwise we parse `listResults` from
        the search-results page.
        """
        try:
            _zillow_breaker.before_call()
        except CircuitOpen as e:
            raise ProviderError(f"zillow circuit open, fast-failing: {e}") from e
        try:
            url = _search_url(normalized)
            html, final_url = await self._fetch_html(url)
            if "/homedetails/" in final_url:
                detail = self._parse_detail(html)
                result = [_candidate_from_property(detail.raw)]
            else:
                result = self._parse_search_results(html)
            _zillow_breaker.on_success()
            return result
        except ProviderError:
            _zillow_breaker.on_failure()
            raise

    async def get_property(self, zpid: str) -> PropertyDetail:
        try:
            _zillow_breaker.before_call()
        except CircuitOpen as e:
            raise ProviderError(f"zillow circuit open, fast-failing: {e}") from e
        try:
            url = f"https://www.zillow.com/homedetails/{zpid}_zpid/"
            html, _ = await self._fetch_html(url)
            detail = self._parse_detail(html)
            if detail.zpid_echo != str(zpid):
                raise ProviderError(
                    f"zpid echo mismatch: requested={zpid} got={detail.zpid_echo}"
                )
            _zillow_breaker.on_success()
            return detail
        except ProviderError:
            _zillow_breaker.on_failure()
            raise

    # --------------------------------------------------------------- internals

    @retry(
        retry=retry_if_exception_type(ProviderError),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=8),
        reraise=True,
    )
    async def _fetch_html(self, url: str) -> tuple[str, str]:
        log.debug("direct.fetch.start", url=url)
        try:
            async with AsyncSession() as session:
                r = await session.get(
                    url,
                    impersonate=self._impersonation,
                    headers=_HEADERS,
                    proxies=self._proxies,
                    timeout=self._timeout,
                    allow_redirects=True,
                )
        except ProviderError:
            raise
        except Exception as e:
            raise ProviderError(f"network error fetching {url}: {e}") from e

        if r.status_code in (403, 429):
            raise ProviderError(f"zillow blocked: status={r.status_code} url={url}")
        if r.status_code != 200:
            raise ProviderError(f"unexpected status {r.status_code} for {url}")
        if len(r.text) < 2000 or "__NEXT_DATA__" not in r.text:
            raise ProviderError(f"empty or bot-gated response for {url}")
        log.debug("direct.fetch.ok", url=url, final_url=r.url, bytes=len(r.text))
        return r.text, r.url

    @staticmethod
    def _extract_next_data(html: str) -> dict[str, Any]:
        m = _NEXT_DATA_RE.search(html)
        if not m:
            raise ProviderError("__NEXT_DATA__ script tag not found")
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError as e:
            raise ProviderError(f"__NEXT_DATA__ invalid JSON: {e}") from e

    def _parse_detail(self, html: str) -> PropertyDetail:
        nd = self._extract_next_data(html)
        try:
            cache_str = nd["props"]["pageProps"]["componentProps"]["gdpClientCache"]
        except (KeyError, TypeError) as e:
            raise ProviderError(f"gdpClientCache missing from __NEXT_DATA__: {e}") from e
        try:
            cache = json.loads(cache_str)
        except json.JSONDecodeError as e:
            raise ProviderError(f"gdpClientCache not JSON: {e}") from e
        if not cache:
            raise ProviderError("gdpClientCache is empty")

        payload = next(iter(cache.values()))
        if not isinstance(payload, dict) or "property" not in payload:
            raise ProviderError("gdpClientCache payload missing 'property'")
        p = payload["property"]

        zpid = p.get("zpid")
        if zpid is None:
            raise ProviderError("property.zpid missing")

        street = p.get("streetAddress") or ""
        city = p.get("city") or ""
        state = p.get("state") or ""
        zipcode = p.get("zipcode") or ""
        full_address = ", ".join(
            part for part in (street, city, f"{state} {zipcode}".strip()) if part
        )

        return PropertyDetail(
            zpid_echo=str(zpid),
            zestimate=p.get("zestimate"),
            rent_zestimate=p.get("rentZestimate"),
            full_address=full_address,
            raw=p,
        )

    def _parse_search_results(self, html: str) -> list[Candidate]:
        nd = self._extract_next_data(html)
        try:
            sp = nd["props"]["pageProps"]["searchPageState"]
            list_results = sp["cat1"]["searchResults"]["listResults"] if sp else []
        except (KeyError, TypeError):
            return []
        return [
            _candidate_from_list_result(r)
            for r in list_results
            if r.get("zpid") is not None
        ]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _search_url(normalized: NormalizedAddress) -> str:
    # Zillow uses /homes/<address-with-spaces-as-hyphens>_rb/
    line = normalized.single_line().replace(" ", "-")
    return f"https://www.zillow.com/homes/{line}_rb/"


def _split_street(addr: str) -> tuple[str | None, str | None]:
    parts = addr.strip().split(None, 1)
    if not parts:
        return None, None
    number = parts[0] if parts[0][:1].isdigit() else None
    name = parts[1] if len(parts) > 1 else None
    return number, name


def _candidate_from_property(p: dict[str, Any]) -> Candidate:
    number, name = _split_street(p.get("streetAddress", ""))
    return Candidate(
        zpid=str(p.get("zpid")),
        street_number=number,
        street_name=name,
        city=p.get("city"),
        state=p.get("state"),
        zip5=(p.get("zipcode") or "")[:5] or None,
        lat=p.get("latitude"),
        lon=p.get("longitude"),
        raw=p,
    )


def _candidate_from_list_result(r: dict[str, Any]) -> Candidate:
    addr = r.get("addressStreet") or r.get("address") or ""
    number, name = _split_street(addr)
    latlon = r.get("latLong") if isinstance(r.get("latLong"), dict) else {}
    return Candidate(
        zpid=str(r.get("zpid")),
        street_number=number,
        street_name=name,
        city=r.get("addressCity"),
        state=r.get("addressState"),
        zip5=(r.get("addressZipcode") or "")[:5] or None,
        lat=latlon.get("latitude"),
        lon=latlon.get("longitude"),
        raw=r,
    )
