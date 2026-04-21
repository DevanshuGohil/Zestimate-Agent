"""Stage 3 fetch tests."""

from __future__ import annotations

import pytest

from zestimate_agent.fetch import fetch_property
from zestimate_agent.models import PropertyDetail, ProviderError
from zestimate_agent.providers.base import Provider
from zestimate_agent.providers.direct import DirectProvider


class _StubProvider(Provider):
    name = "stub"

    def __init__(self, detail: PropertyDetail) -> None:
        self._detail = detail
        self.calls: list[str] = []

    async def search(self, normalized):  # type: ignore[override]
        raise NotImplementedError

    async def get_property(self, zpid: str) -> PropertyDetail:  # type: ignore[override]
        self.calls.append(zpid)
        return self._detail


async def test_fetch_property_forwards_provider_result() -> None:
    expected = PropertyDetail(
        zpid_echo="12345",
        zestimate=500_000,
        rent_zestimate=2500,
        full_address="1 Example St, Nowhere, CA 90000",
    )
    provider = _StubProvider(expected)
    got = await fetch_property("12345", provider)
    assert got is expected
    assert provider.calls == ["12345"]


async def test_fetch_property_propagates_provider_error() -> None:
    class _Failing(Provider):
        name = "failing"

        async def search(self, normalized):  # type: ignore[override]
            raise NotImplementedError

        async def get_property(self, zpid):  # type: ignore[override]
            raise ProviderError("boom")

    with pytest.raises(ProviderError):
        await fetch_property("1", _Failing())


@pytest.mark.live
async def test_direct_provider_get_property_live() -> None:
    """Live: fetch zpid 2101967478 (101 Lombard St, San Francisco, CA 94111)."""
    provider = DirectProvider()
    detail = await provider.get_property("2101967478")

    assert detail.zpid_echo == "2101967478"
    assert detail.zestimate is not None
    assert 100_000 < detail.zestimate < 50_000_000
    assert "Lombard" in detail.full_address
    assert "San Francisco" in detail.full_address
