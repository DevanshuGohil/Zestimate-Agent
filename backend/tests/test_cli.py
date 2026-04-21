"""CLI tests using Typer's CliRunner — offline (all external calls mocked)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from typer.testing import CliRunner

from zestimate_agent.cli import app
from zestimate_agent.models import (
    AmbiguousAddressError,
    Confidence,
    NormalizedAddress,
    ZestimateResult,
)

runner = CliRunner()


def _norm() -> NormalizedAddress:
    return NormalizedAddress(
        street_number="101",
        street_name="Lombard Street",
        city="San Francisco",
        state="CA",
        zip5="94111",
        confidence=Confidence.HIGH,
    )


def _result() -> ZestimateResult:
    return ZestimateResult(
        address="101 Lombard St, San Francisco, CA 94111",
        zestimate=799_900,
        zpid="2101967478",
        fetched_at=datetime.now(tz=timezone.utc),
        provider_used="direct",
        confidence=Confidence.HIGH,
    )


def _mock_settings() -> MagicMock:
    s = MagicMock()
    s.proxy_url = None
    s.cache_ttl_hours = 24
    s.cache_failure_ttl_hours = 6
    s.cache_db_path = MagicMock()
    return s


def _mock_cache(hit_result: MagicMock | None = None) -> MagicMock:
    """Return a MagicMock Cache with async methods wired up."""
    mc = MagicMock()
    default_hit = MagicMock(hit=False, result=None, was_failure=False)
    mc.lookup = AsyncMock(return_value=hit_result if hit_result is not None else default_hit)
    mc.store = AsyncMock()
    mc.store_failure = AsyncMock()
    mc.clear = AsyncMock()
    return mc


def _patch_pipeline(result: ZestimateResult, norm: NormalizedAddress):
    """Context managers that patch normalize, resolve, fetch, validate."""
    from zestimate_agent.models import PropertyDetail, ResolvedProperty

    detail = PropertyDetail(
        zpid_echo=result.zpid,
        zestimate=result.zestimate,
        full_address=result.address,
        raw={
            "zpid": int(result.zpid),
            "streetAddress": "101 Lombard St",
            "city": "San Francisco",
            "state": "CA",
            "zipcode": "94111",
            "zestimate": result.zestimate,
        },
    )
    resolved = ResolvedProperty(
        zpid=result.zpid,
        matched_address=result.address,
        confidence=Confidence.HIGH,
    )
    return (
        patch("zestimate_agent.cli.normalize_address", return_value=norm),
        patch("zestimate_agent.cli.resolve_zpid", return_value=resolved),
        patch("zestimate_agent.cli.fetch_property", return_value=detail),
        patch("zestimate_agent.cli.validate_result", return_value=result),
        patch("zestimate_agent.cli.get_settings", return_value=_mock_settings()),
        patch("zestimate_agent.cli.configure_observability"),
    )


# ---------------------------------------------------------------------------
# Human-readable output
# ---------------------------------------------------------------------------


def test_lookup_human_output(tmp_path):
    norm = _norm()
    res = _result()
    patches = _patch_pipeline(res, norm)
    mock_cache = _mock_cache()
    with (
        patches[0],
        patches[1],
        patches[2],
        patches[3],
        patches[4],
        patches[5],
        patch("zestimate_agent.cli.Cache", return_value=mock_cache),
    ):
        result = runner.invoke(app, ["lookup", "101 Lombard St, San Francisco, CA 94111"])

    assert result.exit_code == 0, result.output
    assert "799,900" in result.output
    assert "2101967478" in result.output
    assert "HIGH" in result.output
    assert "miss" in result.output


def test_lookup_json_output(tmp_path):
    norm = _norm()
    res = _result()
    patches = _patch_pipeline(res, norm)
    mock_cache = _mock_cache()
    with (
        patches[0],
        patches[1],
        patches[2],
        patches[3],
        patches[4],
        patches[5],
        patch("zestimate_agent.cli.Cache", return_value=mock_cache),
    ):
        result = runner.invoke(app, ["lookup", "--json", "101 Lombard St, San Francisco, CA 94111"])

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["zestimate"] == 799_900
    assert data["zpid"] == "2101967478"
    assert data["cache_hit"] is False
    assert "elapsed_ms" in data


def test_lookup_cache_hit_shows_hit(tmp_path):
    norm = _norm()
    res = _result()
    patches = _patch_pipeline(res, norm)
    mock_cache = _mock_cache(hit_result=MagicMock(hit=True, result=res, was_failure=False))
    with (
        patches[0],
        patches[4],
        patches[5],
        patch("zestimate_agent.cli.Cache", return_value=mock_cache),
    ):
        result = runner.invoke(app, ["lookup", "101 Lombard St, San Francisco, CA 94111"])

    assert result.exit_code == 0, result.output
    assert "HIT" in result.output


def test_lookup_ambiguous_exits_1(tmp_path):
    with (
        patch("zestimate_agent.cli.normalize_address", side_effect=AmbiguousAddressError("too vague")),
        patch("zestimate_agent.cli.get_settings", return_value=_mock_settings()),
        patch("zestimate_agent.cli.configure_observability"),
        patch("zestimate_agent.cli.Cache", return_value=_mock_cache()),
    ):
        result = runner.invoke(app, ["lookup", "somewhere"])

    assert result.exit_code == 1
    combined = (result.output + (result.stderr or "")).lower()
    assert "resolve" in combined or "ambiguous" in combined or "error" in combined


# ---------------------------------------------------------------------------
# --no-cache bypasses lookup
# ---------------------------------------------------------------------------


def test_no_cache_bypasses_cache_lookup(tmp_path):
    norm = _norm()
    res = _result()
    patches = _patch_pipeline(res, norm)
    mock_cache = _mock_cache()
    with (
        patches[0],
        patches[1],
        patches[2],
        patches[3],
        patches[4],
        patches[5],
        patch("zestimate_agent.cli.Cache", return_value=mock_cache),
    ):
        result = runner.invoke(
            app, ["lookup", "--no-cache", "101 Lombard St, San Francisco, CA 94111"]
        )

    assert result.exit_code == 0, result.output
    mock_cache.lookup.assert_not_called()
