"""Pipeline integration tests."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from zestimate_agent.models import (
    Candidate,
    Confidence,
    NormalizedAddress,
    PropertyDetail,
    ResolvedProperty,
    ZestimateResult,
)
from zestimate_agent.pipeline import run_pipeline
from zestimate_agent.providers.base import Provider


# ---------------------------------------------------------------------------
# Stub provider (async)
# ---------------------------------------------------------------------------


class _StubProvider(Provider):
    name = "stub"

    def __init__(self, candidates: list[Candidate], detail: PropertyDetail) -> None:
        self._candidates = candidates
        self._detail = detail

    async def search(self, normalized: NormalizedAddress) -> list[Candidate]:  # type: ignore[override]
        return self._candidates

    async def get_property(self, zpid: str) -> PropertyDetail:  # type: ignore[override]
        return self._detail


def _make_stub() -> _StubProvider:
    candidate = Candidate(
        zpid="2101967478",
        street_number="101",
        street_name="Lombard St",
        city="San Francisco",
        state="CA",
        zip5="94111",
    )
    detail = PropertyDetail(
        zpid_echo="2101967478",
        zestimate=799_900,
        full_address="101 Lombard St, San Francisco, CA 94111",
        raw={
            "zpid": 2101967478,
            "streetAddress": "101 Lombard St",
            "city": "San Francisco",
            "state": "CA",
            "zipcode": "94111",
            "zestimate": 799_900,
        },
    )
    return _StubProvider([candidate], detail)


async def _mock_normalize(address: str, *args, **kwargs) -> NormalizedAddress:
    return NormalizedAddress(
        street_number="101",
        street_name="Lombard St",
        city="San Francisco",
        state="CA",
        zip5="94111",
        confidence=Confidence.HIGH,
    )


# ---------------------------------------------------------------------------
# Offline tests
# ---------------------------------------------------------------------------


async def test_pipeline_returns_zestimate_result(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = _make_stub()
    with patch("zestimate_agent.pipeline.normalize_address", side_effect=_mock_normalize):
        result = await run_pipeline(
            "101 Lombard St, San Francisco, CA 94111",
            provider=provider,
            settings=MagicMock(proxy_url=None),
        )
    assert isinstance(result, ZestimateResult)
    assert result.zestimate == 799_900
    assert result.zpid == "2101967478"
    assert result.provider_used == "stub"
    assert result.confidence == Confidence.HIGH


async def test_pipeline_propagates_provider_error(monkeypatch: pytest.MonkeyPatch) -> None:
    from zestimate_agent.models import ProviderError

    class _FailProvider(Provider):
        name = "fail"

        async def search(self, normalized):  # type: ignore[override]
            raise ProviderError("boom")

        async def get_property(self, zpid):  # type: ignore[override]
            raise ProviderError("boom")

    with (
        patch("zestimate_agent.pipeline.normalize_address", side_effect=_mock_normalize),
        pytest.raises(ProviderError),
    ):
        await run_pipeline(
            "101 Lombard St, San Francisco, CA 94111",
            provider=_FailProvider(),
            settings=MagicMock(proxy_url=None),
        )


# ---------------------------------------------------------------------------
# Live test
# ---------------------------------------------------------------------------


@pytest.mark.live
async def test_pipeline_live_end_to_end() -> None:
    """Live: full pipeline for 101 Lombard St, San Francisco, CA 94111."""
    os.environ.setdefault("MISTRAL_API_KEY", "test")
    os.environ.setdefault("RAPIDAPI_KEY", "test")
    from zestimate_agent.config import get_settings
    get_settings.cache_clear()

    result = await run_pipeline(
        "101 Lombard St, San Francisco, CA 94111",
        settings=get_settings(),
    )
    assert result.zpid == "2101967478"
    assert result.zestimate is not None
    assert 100_000 < result.zestimate < 50_000_000
    assert result.confidence in (Confidence.HIGH, Confidence.MEDIUM)
    assert result.provider_used == "direct"
    assert "Lombard" in result.address
