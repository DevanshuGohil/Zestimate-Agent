"""Stage 2 resolve tests."""

from __future__ import annotations

from typing import Any

import pytest

from zestimate_agent.models import (
    AmbiguousAddressError,
    Candidate,
    Confidence,
    NormalizedAddress,
    PropertyDetail,
)
from zestimate_agent.providers.base import Provider
from zestimate_agent.providers.direct import DirectProvider
from zestimate_agent.resolve import resolve_zpid


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _norm(
    number: str = "101",
    street: str = "Lombard St",
    city: str = "San Francisco",
    state: str = "CA",
    zip5: str = "94111",
    confidence: Confidence = Confidence.HIGH,
) -> NormalizedAddress:
    return NormalizedAddress(
        street_number=number,
        street_name=street,
        city=city,
        state=state,
        zip5=zip5,
        confidence=confidence,
    )


def _candidate(**kwargs: Any) -> Candidate:
    defaults: dict[str, Any] = {
        "zpid": "999",
        "street_number": "101",
        "street_name": "Lombard St",
        "city": "San Francisco",
        "state": "CA",
        "zip5": "94111",
    }
    defaults.update(kwargs)
    return Candidate(**defaults)


class _StubProvider(Provider):
    name = "stub"

    def __init__(self, candidates: list[Candidate]) -> None:
        self._candidates = candidates

    async def search(self, normalized: NormalizedAddress) -> list[Candidate]:  # type: ignore[override]
        return self._candidates

    async def get_property(self, zpid: str) -> PropertyDetail:  # type: ignore[override]
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Offline tests
# ---------------------------------------------------------------------------


async def test_single_candidate_returns_high_confidence() -> None:
    c = _candidate(zpid="42")
    result = await resolve_zpid(_norm(), _StubProvider([c]))
    assert result.zpid == "42"
    assert result.confidence == Confidence.HIGH


async def test_no_candidates_raises_ambiguous() -> None:
    with pytest.raises(AmbiguousAddressError) as exc_info:
        await resolve_zpid(_norm(), _StubProvider([]))
    assert exc_info.value.candidates == []


async def test_multiple_candidates_exact_match_high_confidence() -> None:
    match = _candidate(zpid="correct", street_number="101", zip5="94111", street_name="Lombard St")
    noise = _candidate(zpid="wrong", street_number="200", zip5="94111", street_name="Filbert St")
    result = await resolve_zpid(_norm(), _StubProvider([match, noise]))
    assert result.zpid == "correct"
    assert result.confidence == Confidence.HIGH


async def test_multiple_candidates_fuzzy_match() -> None:
    c1 = _candidate(zpid="a", street_name="Lombard Street", zip5="94111")
    c2 = _candidate(zpid="b", street_name="Filbert Street", zip5="94111")
    result = await resolve_zpid(_norm(), _StubProvider([c1, c2]))
    assert result.zpid == "a"
    assert result.confidence in (Confidence.HIGH, Confidence.MEDIUM)


async def test_wrong_street_number_filtered_out() -> None:
    wrong = _candidate(zpid="x", street_number="200", zip5="94111", street_name="Lombard St")
    with pytest.raises(AmbiguousAddressError):
        await resolve_zpid(_norm(), _StubProvider([wrong]))


async def test_two_high_confidence_matches_raises_ambiguous() -> None:
    c1 = _candidate(zpid="unit-a", street_name="Lombard St", street_number="101", zip5="94111")
    c2 = _candidate(zpid="unit-b", street_name="Lombard St", street_number="101", zip5="94111")
    with pytest.raises(AmbiguousAddressError) as exc_info:
        await resolve_zpid(_norm(), _StubProvider([c1, c2]))
    assert len(exc_info.value.candidates) == 2


async def test_medium_confidence_when_street_name_partial_match() -> None:
    c = _candidate(zpid="m", street_name="Lombard Street", zip5="94111")
    result = await resolve_zpid(_norm(), _StubProvider([c]))
    assert result.confidence in (Confidence.MEDIUM, Confidence.HIGH)
    assert result.zpid == "m"


# ---------------------------------------------------------------------------
# Live tests
# ---------------------------------------------------------------------------


@pytest.mark.live
async def test_resolve_zpid_live_specific_address() -> None:
    """Live: resolve 101 Lombard St, San Francisco, CA 94111 → zpid 2101967478."""
    normalized = _norm()
    provider = DirectProvider()
    result = await resolve_zpid(normalized, provider)

    assert result.zpid == "2101967478"
    assert result.confidence == Confidence.HIGH
    assert "Lombard" in result.matched_address


@pytest.mark.live
async def test_resolve_zpid_live_different_address() -> None:
    """Live: resolve a mid-market Chicago condo."""
    normalized = NormalizedAddress(
        street_number="233",
        street_name="E Erie St",
        city="Chicago",
        state="IL",
        zip5="60611",
        confidence=Confidence.HIGH,
    )
    provider = DirectProvider()
    result = await resolve_zpid(normalized, provider)

    assert result.zpid  # non-empty
    assert result.confidence in (Confidence.HIGH, Confidence.MEDIUM)
