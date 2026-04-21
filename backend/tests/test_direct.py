"""Unit tests for DirectProvider — all offline, no network calls."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from zestimate_agent.models import NormalizedAddress, Confidence, ProviderError
from zestimate_agent.providers.direct import (
    DirectProvider,
    _candidate_from_list_result,
    _candidate_from_property,
    _search_url,
    _split_street,
)


# ---------------------------------------------------------------------------
# HTML fixture helpers
# ---------------------------------------------------------------------------


def _next_data_html(data: dict[str, Any]) -> str:
    """Wrap a dict as a __NEXT_DATA__ script tag.

    Pads the page to > 2000 chars so _fetch_html's size guard passes.
    """
    padding = "<!-- " + "x" * 2100 + " -->"
    return (
        f'<html><head>{padding}'
        '<script id="__NEXT_DATA__" type="application/json">'
        f'{json.dumps(data)}'
        '</script></head></html>'
    )


def _detail_html(
    zpid: int = 2101967478,
    street: str = "101 Lombard St",
    city: str = "San Francisco",
    state: str = "CA",
    zipcode: str = "94111",
    zestimate: int | None = 799_900,
) -> str:
    prop: dict[str, Any] = {
        "zpid": zpid,
        "streetAddress": street,
        "city": city,
        "state": state,
        "zipcode": zipcode,
        "zestimate": zestimate,
        "latitude": 37.8038,
        "longitude": -122.4040,
    }
    cache_key = f"ForSaleDoubleScrollFullRenderQuery{zpid}"
    cache = {cache_key: {"property": prop}}
    data = {
        "props": {
            "pageProps": {
                "componentProps": {"gdpClientCache": json.dumps(cache)}
            }
        }
    }
    return _next_data_html(data)


def _search_html(results: list[dict[str, Any]]) -> str:
    data = {
        "props": {
            "pageProps": {
                "searchPageState": {
                    "cat1": {"searchResults": {"listResults": results}}
                }
            }
        }
    }
    return _next_data_html(data)


def _mock_session(
    status_code: int = 200,
    text: str = "",
    url: str = "https://www.zillow.com/homedetails/123_zpid/",
) -> MagicMock:
    """Return an AsyncSession mock with a configurable response."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = text
    resp.url = url

    session = AsyncMock()
    session.get = AsyncMock(return_value=resp)
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=None)
    return session


# ---------------------------------------------------------------------------
# _split_street
# ---------------------------------------------------------------------------


def test_split_street_with_number():
    assert _split_street("101 Lombard St") == ("101", "Lombard St")


def test_split_street_no_number():
    n, name = _split_street("Main Street")
    assert n is None


def test_split_street_empty():
    assert _split_street("") == (None, None)


# ---------------------------------------------------------------------------
# _search_url
# ---------------------------------------------------------------------------


def test_search_url_replaces_spaces_with_hyphens():
    norm = NormalizedAddress(
        street_number="101",
        street_name="Lombard St",
        city="San Francisco",
        state="CA",
        zip5="94111",
        confidence=Confidence.HIGH,
    )
    url = _search_url(norm)
    assert "zillow.com/homes/" in url
    assert " " not in url
    assert url.endswith("_rb/")


# ---------------------------------------------------------------------------
# _candidate_from_property
# ---------------------------------------------------------------------------


def test_candidate_from_property_standard():
    p = {
        "zpid": 42,
        "streetAddress": "101 Lombard St",
        "city": "San Francisco",
        "state": "CA",
        "zipcode": "94111",
        "latitude": 37.8,
        "longitude": -122.4,
    }
    c = _candidate_from_property(p)
    assert c.zpid == "42"
    assert c.street_number == "101"
    assert c.street_name == "Lombard St"
    assert c.city == "San Francisco"
    assert c.zip5 == "94111"


def test_candidate_from_property_missing_fields():
    c = _candidate_from_property({"zpid": 99})
    assert c.zpid == "99"
    assert c.street_number is None
    assert c.city is None


# ---------------------------------------------------------------------------
# _candidate_from_list_result
# ---------------------------------------------------------------------------


def test_candidate_from_list_result_standard():
    r = {
        "zpid": "7",
        "addressStreet": "233 E Erie St",
        "addressCity": "Chicago",
        "addressState": "IL",
        "addressZipcode": "60611",
        "latLong": {"latitude": 41.89, "longitude": -87.62},
    }
    c = _candidate_from_list_result(r)
    assert c.zpid == "7"
    assert c.street_number == "233"
    assert c.city == "Chicago"
    assert c.zip5 == "60611"
    assert c.lat == pytest.approx(41.89)


def test_candidate_from_list_result_no_latlong():
    r = {"zpid": "8", "addressStreet": "10 Main St", "addressCity": "Boston"}
    c = _candidate_from_list_result(r)
    assert c.lat is None
    assert c.lon is None


def test_candidate_from_list_result_latlong_not_dict():
    r = {"zpid": "9", "addressStreet": "5 Oak Ave", "latLong": "bad"}
    c = _candidate_from_list_result(r)
    assert c.lat is None


def test_candidate_from_list_result_zip_truncated():
    r = {"zpid": "10", "addressZipcode": "941110000"}
    c = _candidate_from_list_result(r)
    assert c.zip5 == "94111"


# ---------------------------------------------------------------------------
# _extract_next_data
# ---------------------------------------------------------------------------


def test_extract_next_data_valid():
    provider = DirectProvider()
    html = _next_data_html({"foo": "bar"})
    result = provider._extract_next_data(html)
    assert result == {"foo": "bar"}


def test_extract_next_data_missing_script_tag():
    provider = DirectProvider()
    with pytest.raises(ProviderError, match="__NEXT_DATA__"):
        provider._extract_next_data("<html><body>no script</body></html>")


def test_extract_next_data_malformed_json():
    provider = DirectProvider()
    html = '<script id="__NEXT_DATA__">{bad json}</script>'
    with pytest.raises(ProviderError, match="invalid JSON"):
        provider._extract_next_data(html)


# ---------------------------------------------------------------------------
# _parse_detail
# ---------------------------------------------------------------------------


def test_parse_detail_standard():
    provider = DirectProvider()
    detail = provider._parse_detail(_detail_html())
    assert detail.zpid_echo == "2101967478"
    assert detail.zestimate == 799_900
    assert "Lombard" in detail.full_address
    assert detail.raw["city"] == "San Francisco"


def test_parse_detail_zestimate_none():
    provider = DirectProvider()
    detail = provider._parse_detail(_detail_html(zestimate=None))
    assert detail.zestimate is None


def test_parse_detail_missing_gdp_client_cache():
    provider = DirectProvider()
    html = _next_data_html({"props": {"pageProps": {"componentProps": {}}}})
    with pytest.raises(ProviderError, match="gdpClientCache"):
        provider._parse_detail(html)


def test_parse_detail_empty_cache():
    provider = DirectProvider()
    data = {
        "props": {
            "pageProps": {
                "componentProps": {"gdpClientCache": json.dumps({})}
            }
        }
    }
    with pytest.raises(ProviderError, match="empty"):
        provider._parse_detail(_next_data_html(data))


def test_parse_detail_missing_property_key():
    provider = DirectProvider()
    data = {
        "props": {
            "pageProps": {
                "componentProps": {
                    "gdpClientCache": json.dumps({"key": {"no_property": True}})
                }
            }
        }
    }
    with pytest.raises(ProviderError, match="'property'"):
        provider._parse_detail(_next_data_html(data))


def test_parse_detail_missing_zpid():
    provider = DirectProvider()
    cache = {"k": {"property": {"streetAddress": "101 Main St"}}}
    data = {
        "props": {
            "pageProps": {
                "componentProps": {"gdpClientCache": json.dumps(cache)}
            }
        }
    }
    with pytest.raises(ProviderError, match="zpid missing"):
        provider._parse_detail(_next_data_html(data))


def test_parse_detail_full_address_construction():
    provider = DirectProvider()
    detail = provider._parse_detail(
        _detail_html(street="500 5th Ave", city="New York", state="NY", zipcode="10110")
    )
    assert detail.full_address == "500 5th Ave, New York, NY 10110"


# ---------------------------------------------------------------------------
# _parse_search_results
# ---------------------------------------------------------------------------


def test_parse_search_results_standard():
    results = [
        {
            "zpid": "1",
            "addressStreet": "101 Lombard St",
            "addressCity": "San Francisco",
            "addressState": "CA",
            "addressZipcode": "94111",
        }
    ]
    provider = DirectProvider()
    candidates = provider._parse_search_results(_search_html(results))
    assert len(candidates) == 1
    assert candidates[0].zpid == "1"


def test_parse_search_results_filters_out_missing_zpid():
    results = [{"addressStreet": "No ZPID St"}, {"zpid": "2", "addressStreet": "Has ZPID Ave"}]
    provider = DirectProvider()
    candidates = provider._parse_search_results(_search_html(results))
    assert len(candidates) == 1
    assert candidates[0].zpid == "2"


def test_parse_search_results_empty_list():
    provider = DirectProvider()
    assert provider._parse_search_results(_search_html([])) == []


def test_parse_search_results_missing_search_page_state():
    provider = DirectProvider()
    html = _next_data_html({"props": {"pageProps": {}}})
    assert provider._parse_search_results(html) == []


# ---------------------------------------------------------------------------
# _fetch_html — status code handling (retries patched out)
# ---------------------------------------------------------------------------


async def test_fetch_html_success():
    html = _detail_html()
    session = _mock_session(200, html, "https://www.zillow.com/homedetails/123_zpid/")
    with (
        patch("zestimate_agent.providers.direct.AsyncSession", return_value=session),
        patch("asyncio.sleep"),
    ):
        provider = DirectProvider()
        text, url = await provider._fetch_html("https://www.zillow.com/homedetails/123_zpid/")
    assert "__NEXT_DATA__" in text
    assert "zillow.com" in url


async def test_fetch_html_403_raises_provider_error():
    session = _mock_session(403, "blocked")
    with (
        patch("zestimate_agent.providers.direct.AsyncSession", return_value=session),
        patch("asyncio.sleep"),
    ):
        provider = DirectProvider()
        with pytest.raises(ProviderError, match="blocked"):
            await provider._fetch_html("https://www.zillow.com/")


async def test_fetch_html_429_raises_provider_error():
    session = _mock_session(429, "rate limited")
    with (
        patch("zestimate_agent.providers.direct.AsyncSession", return_value=session),
        patch("asyncio.sleep"),
    ):
        provider = DirectProvider()
        with pytest.raises(ProviderError, match="blocked"):
            await provider._fetch_html("https://www.zillow.com/")


async def test_fetch_html_non_200_raises_provider_error():
    session = _mock_session(500, "server error")
    with (
        patch("zestimate_agent.providers.direct.AsyncSession", return_value=session),
        patch("asyncio.sleep"),
    ):
        provider = DirectProvider()
        with pytest.raises(ProviderError, match="unexpected status"):
            await provider._fetch_html("https://www.zillow.com/")


async def test_fetch_html_empty_response_raises_provider_error():
    session = _mock_session(200, "tiny")
    with (
        patch("zestimate_agent.providers.direct.AsyncSession", return_value=session),
        patch("asyncio.sleep"),
    ):
        provider = DirectProvider()
        with pytest.raises(ProviderError, match="bot-gated"):
            await provider._fetch_html("https://www.zillow.com/")


async def test_fetch_html_network_error_raises_provider_error():
    session = AsyncMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=None)
    session.get = AsyncMock(side_effect=ConnectionError("timeout"))
    with (
        patch("zestimate_agent.providers.direct.AsyncSession", return_value=session),
        patch("asyncio.sleep"),
    ):
        provider = DirectProvider()
        with pytest.raises(ProviderError, match="network error"):
            await provider._fetch_html("https://www.zillow.com/")


# ---------------------------------------------------------------------------
# get_property — zpid echo mismatch
# ---------------------------------------------------------------------------


async def test_get_property_zpid_mismatch_raises():
    provider = DirectProvider()
    provider._fetch_html = AsyncMock(  # type: ignore[method-assign]
        return_value=(_detail_html(zpid=999), "https://www.zillow.com/homedetails/999_zpid/")
    )
    with pytest.raises(ProviderError, match="zpid echo mismatch"):
        await provider.get_property("12345")


async def test_get_property_returns_detail_on_match():
    provider = DirectProvider()
    provider._fetch_html = AsyncMock(  # type: ignore[method-assign]
        return_value=(
            _detail_html(zpid=2101967478),
            "https://www.zillow.com/homedetails/2101967478_zpid/",
        )
    )
    detail = await provider.get_property("2101967478")
    assert detail.zpid_echo == "2101967478"
    assert detail.zestimate == 799_900
