"""FastAPI server tests — fully offline (all external calls mocked)."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient

from zestimate_agent.api import app
from zestimate_agent.models import (
    AmbiguousAddressError,
    Candidate,
    ClarificationRequest,
    Confidence,
    NoZestimateError,
    ProviderError,
    ValidationError,
    ZestimateResult,
)

client = TestClient(app, raise_server_exceptions=False)

ADDRESS = "101 Lombard St, San Francisco, CA 94111"



def _result() -> ZestimateResult:
    return ZestimateResult(
        address="101 Lombard St, San Francisco, CA 94111",
        zestimate=799_900,
        zpid="2101967478",
        fetched_at=datetime.now(tz=timezone.utc),
        provider_used="direct",
        confidence=Confidence.HIGH,
    )


def _settings() -> MagicMock:
    s = MagicMock()
    s.proxy_url = None
    s.cache_ttl_hours = 24
    s.cache_failure_ttl_hours = 6
    s.cache_db_path = MagicMock()
    s.request_timeout_seconds = 30
    s.rate_limit_lookup = "1000/minute"
    s.rate_limit_cache = "1000/minute"
    return s


def _cache_miss() -> MagicMock:
    return MagicMock(hit=False, result=None, was_failure=False)


def _mock_cache() -> MagicMock:
    """Return a MagicMock Cache with all async methods as AsyncMocks."""
    mc = MagicMock()
    mc.lookup = AsyncMock(return_value=_cache_miss())
    mc.store = AsyncMock()
    mc.store_failure = AsyncMock()
    mc.clear = AsyncMock()
    return mc


# ---------------------------------------------------------------------------
# /health
# ---------------------------------------------------------------------------


def test_health_returns_200():
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"
    assert "version" in r.json()


# ---------------------------------------------------------------------------
# /lookup — happy path
# ---------------------------------------------------------------------------


def test_lookup_success():
    res = _result()
    mock_cache = _mock_cache()
    with (
        patch("zestimate_agent.api.get_settings", return_value=_settings()),
        patch("zestimate_agent.api.configure_observability"),
        patch("zestimate_agent.api.normalize_address", return_value=MagicMock(single_line=lambda: "101 LOMBARD ST, SAN FRANCISCO, CA 94111")),
        patch("zestimate_agent.api.Cache", return_value=mock_cache),
        patch("zestimate_agent.api.run_agent", return_value=res),
    ):
        r = client.post("/lookup", json={"address": ADDRESS})

    assert r.status_code == 200
    data = r.json()
    assert data["zestimate"] == 799_900
    assert data["zpid"] == "2101967478"
    assert data["confidence"] == "HIGH"
    assert data["cache_hit"] is False
    assert "elapsed_ms" in data
    assert "fetched_at" in data


def test_lookup_cache_hit_returns_cached_result():
    res = _result()
    mock_cache = _mock_cache()
    mock_cache.lookup = AsyncMock(return_value=MagicMock(hit=True, result=res, was_failure=False))
    with (
        patch("zestimate_agent.api.get_settings", return_value=_settings()),
        patch("zestimate_agent.api.configure_observability"),
        patch("zestimate_agent.api.normalize_address", return_value=MagicMock(single_line=lambda: "101 LOMBARD ST, SAN FRANCISCO, CA 94111")),
        patch("zestimate_agent.api.Cache", return_value=mock_cache),
        patch("zestimate_agent.api.run_agent") as mock_agent,
    ):
        r = client.post("/lookup", json={"address": ADDRESS})

    assert r.status_code == 200
    assert r.json()["cache_hit"] is True
    mock_agent.assert_not_called()


def test_lookup_no_cache_skips_cache_lookup():
    res = _result()
    mock_cache = _mock_cache()
    with (
        patch("zestimate_agent.api.get_settings", return_value=_settings()),
        patch("zestimate_agent.api.configure_observability"),
        patch("zestimate_agent.api.Cache", return_value=mock_cache),
        patch("zestimate_agent.api.run_agent", return_value=res),
    ):
        r = client.post("/lookup", json={"address": ADDRESS, "no_cache": True})

    assert r.status_code == 200
    mock_cache.lookup.assert_not_called()
    mock_cache.store.assert_not_called()


def test_lookup_stores_result_in_cache():
    res = _result()
    mock_cache = _mock_cache()
    with (
        patch("zestimate_agent.api.get_settings", return_value=_settings()),
        patch("zestimate_agent.api.configure_observability"),
        patch("zestimate_agent.api.normalize_address", return_value=MagicMock(single_line=lambda: "101 LOMBARD ST, SAN FRANCISCO, CA 94111")),
        patch("zestimate_agent.api.Cache", return_value=mock_cache),
        patch("zestimate_agent.api.run_agent", return_value=res),
    ):
        r = client.post("/lookup", json={"address": ADDRESS})

    assert r.status_code == 200
    mock_cache.store.assert_called_once()


# ---------------------------------------------------------------------------
# /lookup — error paths
# ---------------------------------------------------------------------------


def test_lookup_ambiguous_address_returns_422():
    cr = ClarificationRequest(
        reason="multiple matches",
        original_input=ADDRESS,
        candidates=[{"zpid": "1", "address": "101 Lombard St"}],
    )
    mock_cache = _mock_cache()
    with (
        patch("zestimate_agent.api.get_settings", return_value=_settings()),
        patch("zestimate_agent.api.configure_observability"),
        patch("zestimate_agent.api.normalize_address", return_value=MagicMock(single_line=lambda: "X")),
        patch("zestimate_agent.api.Cache", return_value=mock_cache),
        patch("zestimate_agent.api.run_agent", return_value=cr),
    ):
        r = client.post("/lookup", json={"address": ADDRESS})

    assert r.status_code == 422
    detail = r.json()["detail"]
    assert "reason" in detail
    assert "candidates" in detail


def test_lookup_validation_error_returns_422():
    cr = ClarificationRequest(
        reason="Validation failed: street_number mismatch",
        original_input=ADDRESS,
        candidates=[],
    )
    mock_cache = _mock_cache()
    with (
        patch("zestimate_agent.api.get_settings", return_value=_settings()),
        patch("zestimate_agent.api.configure_observability"),
        patch("zestimate_agent.api.normalize_address", return_value=MagicMock(single_line=lambda: "X")),
        patch("zestimate_agent.api.Cache", return_value=mock_cache),
        patch("zestimate_agent.api.run_agent", return_value=cr),
    ):
        r = client.post("/lookup", json={"address": ADDRESS})

    assert r.status_code == 422


def test_lookup_provider_error_returns_503():
    cr = ClarificationRequest(
        reason="Max retries (2) exceeded: fetch: HTTP 403; fetch: HTTP 403",
        original_input=ADDRESS,
        candidates=[],
    )
    mock_cache = _mock_cache()
    with (
        patch("zestimate_agent.api.get_settings", return_value=_settings()),
        patch("zestimate_agent.api.configure_observability"),
        patch("zestimate_agent.api.normalize_address", return_value=MagicMock(single_line=lambda: "X")),
        patch("zestimate_agent.api.Cache", return_value=mock_cache),
        patch("zestimate_agent.api.run_agent", return_value=cr),
    ):
        r = client.post("/lookup", json={"address": ADDRESS})

    assert r.status_code == 503
    assert "HTTP 403" in r.json()["detail"]


def test_lookup_no_zestimate_returns_404():
    cr = ClarificationRequest(
        reason="Zillow does not publish a Zestimate for this property (zpid=9999)",
        original_input=ADDRESS,
        candidates=[],
        zpid="9999",
    )
    mock_cache = _mock_cache()
    with (
        patch("zestimate_agent.api.get_settings", return_value=_settings()),
        patch("zestimate_agent.api.configure_observability"),
        patch("zestimate_agent.api.normalize_address", return_value=MagicMock(single_line=lambda: "X")),
        patch("zestimate_agent.api.Cache", return_value=mock_cache),
        patch("zestimate_agent.api.run_agent", return_value=cr),
    ):
        r = client.post("/lookup", json={"address": ADDRESS})

    assert r.status_code == 404
    detail = r.json()["detail"]
    assert "zpid" in detail
    assert "hint" in detail


def test_lookup_cached_failure_returns_404():
    mock_cache = _mock_cache()
    mock_cache.lookup = AsyncMock(return_value=MagicMock(hit=True, result=None, was_failure=True))
    with (
        patch("zestimate_agent.api.get_settings", return_value=_settings()),
        patch("zestimate_agent.api.configure_observability"),
        patch("zestimate_agent.api.normalize_address", return_value=MagicMock(single_line=lambda: "X")),
        patch("zestimate_agent.api.Cache", return_value=mock_cache),
    ):
        r = client.post("/lookup", json={"address": ADDRESS})

    assert r.status_code == 404
    assert "no_cache" in r.json()["detail"].lower()


def test_lookup_short_address_rejected():
    r = client.post("/lookup", json={"address": "123"})
    assert r.status_code == 422  # Pydantic min_length validation


# ---------------------------------------------------------------------------
# DELETE /cache
# ---------------------------------------------------------------------------


def test_clear_cache_returns_200():
    mock_cache = _mock_cache()
    with (
        patch("zestimate_agent.api.get_settings", return_value=_settings()),
        patch("zestimate_agent.api.Cache", return_value=mock_cache),
    ):
        r = client.delete("/cache")

    assert r.status_code == 200
    assert r.json()["cleared"] is True
    mock_cache.clear.assert_called_once()
