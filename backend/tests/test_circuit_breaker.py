"""Unit tests for ProviderCircuitBreaker + integration with DirectProvider."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from zestimate_agent.circuit_breaker import CircuitOpen, ProviderCircuitBreaker
from zestimate_agent.models import Confidence, NormalizedAddress, ProviderError
from zestimate_agent.providers.direct import DirectProvider, _zillow_breaker


# ---------------------------------------------------------------------------
# ProviderCircuitBreaker — state machine
# ---------------------------------------------------------------------------


def test_initial_state_is_closed():
    b = ProviderCircuitBreaker(fail_max=3, reset_timeout=60)
    assert b.state == "closed"
    assert b.failure_count == 0


def test_before_call_does_not_raise_when_closed():
    b = ProviderCircuitBreaker(fail_max=3, reset_timeout=60)
    b.before_call()  # should not raise


def test_opens_after_fail_max_failures():
    b = ProviderCircuitBreaker(fail_max=3, reset_timeout=60)
    for _ in range(3):
        b.on_failure()
    assert b.state == "open"


def test_raises_circuit_open_when_open():
    b = ProviderCircuitBreaker(fail_max=1, reset_timeout=60)
    b.on_failure()
    with pytest.raises(CircuitOpen) as exc_info:
        b.before_call()
    assert exc_info.value.resets_at is not None


def test_success_resets_to_closed():
    b = ProviderCircuitBreaker(fail_max=5, reset_timeout=60)
    b.on_failure()
    b.on_failure()
    b.on_success()
    assert b.state == "closed"
    assert b.failure_count == 0


def test_reset_clears_all_state():
    b = ProviderCircuitBreaker(fail_max=2, reset_timeout=60)
    b.on_failure()
    b.on_failure()
    assert b.state == "open"
    b.reset()
    assert b.state == "closed"
    assert b.failure_count == 0


async def test_transitions_to_half_open_after_timeout():
    b = ProviderCircuitBreaker(fail_max=1, reset_timeout=0.05)
    b.on_failure()
    assert b.state == "open"

    await asyncio.sleep(0.1)
    b.before_call()  # should not raise — transitions to HALF_OPEN
    assert b.state == "half_open"


async def test_half_open_success_closes_circuit():
    b = ProviderCircuitBreaker(fail_max=1, reset_timeout=0.05)
    b.on_failure()
    await asyncio.sleep(0.1)
    b.before_call()  # HALF_OPEN
    b.on_success()
    assert b.state == "closed"


async def test_half_open_failure_reopens_circuit():
    b = ProviderCircuitBreaker(fail_max=1, reset_timeout=0.05)
    b.on_failure()
    await asyncio.sleep(0.1)
    b.before_call()  # HALF_OPEN
    b.on_failure()
    assert b.state == "open"


# ---------------------------------------------------------------------------
# DirectProvider — circuit breaker integration
# ---------------------------------------------------------------------------


def _norm() -> NormalizedAddress:
    return NormalizedAddress(
        street_number="101",
        street_name="Lombard St",
        city="San Francisco",
        state="CA",
        zip5="94111",
        confidence=Confidence.HIGH,
    )


async def test_circuit_opens_after_repeated_search_failures():
    """After fail_max consecutive ProviderErrors, the circuit opens."""
    _zillow_breaker.reset()
    provider = DirectProvider()
    provider._fetch_html = AsyncMock(  # type: ignore[method-assign]
        side_effect=ProviderError("zillow blocked: status=403")
    )

    for _ in range(5):
        with pytest.raises(ProviderError):
            await provider.search(_norm())

    assert _zillow_breaker.state == "open"

    # Next call should fast-fail without touching _fetch_html
    call_count_before = provider._fetch_html.call_count
    with pytest.raises(ProviderError, match="circuit open"):
        await provider.search(_norm())
    assert provider._fetch_html.call_count == call_count_before  # not called again

    _zillow_breaker.reset()


async def test_circuit_does_not_open_on_success():
    _zillow_breaker.reset()
    from tests.test_direct import _detail_html

    provider = DirectProvider()
    provider._fetch_html = AsyncMock(  # type: ignore[method-assign]
        return_value=(_detail_html(), "https://www.zillow.com/homedetails/2101967478_zpid/")
    )

    await provider.search(_norm())
    assert _zillow_breaker.state == "closed"
    assert _zillow_breaker.failure_count == 0

    _zillow_breaker.reset()


async def test_circuit_opens_after_repeated_get_property_failures():
    _zillow_breaker.reset()
    provider = DirectProvider()
    provider._fetch_html = AsyncMock(  # type: ignore[method-assign]
        side_effect=ProviderError("zillow blocked: status=403")
    )

    for _ in range(5):
        with pytest.raises(ProviderError):
            await provider.get_property("12345")

    assert _zillow_breaker.state == "open"
    _zillow_breaker.reset()
