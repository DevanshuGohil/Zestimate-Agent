"""Stage 1 normalize tests."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from zestimate_agent.models import AmbiguousAddressError, Confidence, NormalizedAddress
from zestimate_agent.normalize import (
    _extract_zip,
    _parse_nominatim,
    _split_number_name,
    _state_abbr,
    _try_regex,
    normalize_address,
)


# ---------------------------------------------------------------------------
# Unit tests — pure helpers (sync)
# ---------------------------------------------------------------------------


def test_extract_zip_finds_5digit() -> None:
    assert _extract_zip("123 Main St, Springfield, IL 62701") == "62701"
    assert _extract_zip("no zip here") is None
    assert _extract_zip("12345-6789") == "12345"


def test_split_number_name_standard() -> None:
    assert _split_number_name("101 Lombard St") == ("101", "Lombard St")
    assert _split_number_name("233 East Erie Street") == ("233", "East Erie Street")


def test_split_number_name_no_number() -> None:
    n, name = _split_number_name("Main St")
    assert n is None


def test_state_abbr_full_name() -> None:
    assert _state_abbr("California") == "CA"
    assert _state_abbr("Illinois") == "IL"
    assert _state_abbr("District of Columbia") == "DC"


def test_state_abbr_already_short() -> None:
    assert _state_abbr("CA") == "CA"
    assert _state_abbr("IL") == "IL"


def test_state_abbr_unknown() -> None:
    assert _state_abbr("") is None
    assert _state_abbr("Narnia") is None


# ---------------------------------------------------------------------------
# Regex fallback (sync)
# ---------------------------------------------------------------------------


def test_try_regex_full_address() -> None:
    result = _try_regex("101 Lombard St, San Francisco, CA 94111")
    assert result is not None
    assert result.street_number == "101"
    assert result.street_name == "Lombard St"
    assert result.city == "San Francisco"
    assert result.state == "CA"
    assert result.zip5 == "94111"
    assert result.confidence == Confidence.MEDIUM


def test_try_regex_no_commas() -> None:
    result = _try_regex("101 Lombard St San Francisco CA 94111")
    assert result is not None
    assert result.zip5 == "94111"


def test_try_regex_missing_zip_returns_none() -> None:
    result = _try_regex("101 Lombard St, San Francisco, CA")
    assert result is None


def test_try_regex_missing_state_returns_none() -> None:
    result = _try_regex("101 Lombard St, San Francisco, 94111")
    assert result is None


# ---------------------------------------------------------------------------
# Nominatim result parser (offline, pure — sync)
# ---------------------------------------------------------------------------


def _nominatim_hit(
    house_number: str = "101",
    road: str = "Lombard Street",
    city: str = "San Francisco",
    state: str = "California",
    postcode: str = "94113",
    cls: str = "place",
    typ: str = "house",
    lat: str = "37.8038",
    lon: str = "-122.4040",
) -> dict[str, Any]:
    return {
        "class": cls,
        "type": typ,
        "lat": lat,
        "lon": lon,
        "address": {
            "house_number": house_number,
            "road": road,
            "city": city,
            "state": state,
            "postcode": postcode,
            "country_code": "us",
        },
    }


def test_parse_nominatim_high_confidence_house() -> None:
    hit = _nominatim_hit(cls="place", typ="house")
    result = _parse_nominatim(hit, input_zip="94111")
    assert result.street_number == "101"
    assert result.street_name == "Lombard Street"
    assert result.city == "San Francisco"
    assert result.state == "CA"
    assert result.zip5 == "94111"
    assert result.confidence == Confidence.HIGH
    assert result.lat == pytest.approx(37.8038)


def test_parse_nominatim_prefers_input_zip() -> None:
    hit = _nominatim_hit(postcode="94113")
    result = _parse_nominatim(hit, input_zip="94111")
    assert result.zip5 == "94111"


def test_parse_nominatim_uses_geocoder_zip_when_no_input() -> None:
    hit = _nominatim_hit(postcode="94113")
    result = _parse_nominatim(hit, input_zip=None)
    assert result.zip5 == "94113"


def test_parse_nominatim_building_class_high_confidence() -> None:
    hit = _nominatim_hit(cls="building", typ="apartments")
    result = _parse_nominatim(hit, input_zip=None)
    assert result.confidence == Confidence.HIGH


def test_parse_nominatim_other_type_medium_confidence() -> None:
    hit = _nominatim_hit(cls="amenity", typ="restaurant")
    result = _parse_nominatim(hit, input_zip=None)
    assert result.confidence == Confidence.MEDIUM


# ---------------------------------------------------------------------------
# normalize_address integration (offline — async, Nominatim mocked)
# ---------------------------------------------------------------------------


def _mock_async_client(hits: list[dict[str, Any]]) -> AsyncMock:
    """Build an AsyncMock that acts as `async with httpx.AsyncClient() as c: r = await c.get(...)`."""
    mock_resp = MagicMock()
    mock_resp.json.return_value = hits
    mock_resp.raise_for_status = MagicMock()

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=mock_resp)
    # Make __aenter__ return mock_client itself so `async with ... as client` gives
    # us the same object whose `.get` we configured above.
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    return mock_client


async def test_normalize_uses_nominatim_when_no_usps_key() -> None:
    hit = _nominatim_hit()
    mock_client = _mock_async_client([hit])
    with (
        patch("zestimate_agent.normalize.httpx.AsyncClient", return_value=mock_client),
        patch("zestimate_agent.normalize.asyncio.sleep", new_callable=AsyncMock),
        patch("zestimate_agent.normalize.get_settings") as mock_settings,
    ):
        mock_settings.return_value = MagicMock(usps_user_id=None, google_maps_api_key=None)
        result = await normalize_address("101 Lombard St, San Francisco, CA 94111")

    assert result.state == "CA"
    assert result.street_number == "101"
    assert result.confidence == Confidence.HIGH


async def test_normalize_raises_on_ambiguous_states() -> None:
    hits = [
        _nominatim_hit(city="Springfield", state="Illinois", postcode="62701"),
        _nominatim_hit(city="Springfield", state="Oregon", postcode="97477"),
    ]
    mock_client = _mock_async_client(hits)
    with (
        patch("zestimate_agent.normalize.httpx.AsyncClient", return_value=mock_client),
        patch("zestimate_agent.normalize.asyncio.sleep", new_callable=AsyncMock),
        patch("zestimate_agent.normalize.get_settings") as mock_settings,
    ):
        mock_settings.return_value = MagicMock(usps_user_id=None, google_maps_api_key=None)
        with pytest.raises(AmbiguousAddressError, match="multiple states"):
            await normalize_address("123 Main St, Springfield")


async def test_normalize_falls_back_to_regex() -> None:
    mock_client = _mock_async_client([])
    with (
        patch("zestimate_agent.normalize.httpx.AsyncClient", return_value=mock_client),
        patch("zestimate_agent.normalize.asyncio.sleep", new_callable=AsyncMock),
        patch("zestimate_agent.normalize.get_settings") as mock_settings,
    ):
        mock_settings.return_value = MagicMock(usps_user_id=None, google_maps_api_key=None)
        result = await normalize_address("101 Lombard St, San Francisco, CA 94111")

    assert result.confidence == Confidence.MEDIUM
    assert result.zip5 == "94111"


async def test_normalize_raises_when_all_fail() -> None:
    mock_client = _mock_async_client([])
    with (
        patch("zestimate_agent.normalize.httpx.AsyncClient", return_value=mock_client),
        patch("zestimate_agent.normalize.asyncio.sleep", new_callable=AsyncMock),
        patch("zestimate_agent.normalize.get_settings") as mock_settings,
    ):
        mock_settings.return_value = MagicMock(usps_user_id=None, google_maps_api_key=None)
        with pytest.raises(AmbiguousAddressError):
            await normalize_address("somewhere")


# ---------------------------------------------------------------------------
# Live tests
# ---------------------------------------------------------------------------


@pytest.mark.live
async def test_normalize_live_specific_address() -> None:
    """Live: normalize a known SF address via real Nominatim."""
    import os

    os.environ.setdefault("MISTRAL_API_KEY", "test")
    os.environ.setdefault("RAPIDAPI_KEY", "test")
    from zestimate_agent.config import get_settings
    get_settings.cache_clear()

    result = await normalize_address(
        "101 Lombard St, San Francisco, CA 94111",
        settings=get_settings(),
    )
    assert result.street_number == "101"
    assert "Lombard" in result.street_name
    assert result.state == "CA"
    assert result.zip5 == "94111"
    assert result.confidence in (Confidence.HIGH, Confidence.MEDIUM)
    assert result.lat is not None


@pytest.mark.live
async def test_normalize_live_ambiguous_raises() -> None:
    """Live: ambiguous address with no zip should raise AmbiguousAddressError."""
    import os

    os.environ.setdefault("MISTRAL_API_KEY", "test")
    os.environ.setdefault("RAPIDAPI_KEY", "test")
    from zestimate_agent.config import get_settings
    get_settings.cache_clear()

    with pytest.raises(AmbiguousAddressError):
        await normalize_address("123 Main St, Springfield", settings=get_settings())
