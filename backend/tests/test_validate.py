"""Stage 4 validate tests — pure logic, fully offline."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest

from zestimate_agent.models import (
    Confidence,
    NormalizedAddress,
    NoZestimateError,
    PropertyDetail,
    ResolvedProperty,
    ValidationError,
    ZestimateResult,
)
from zestimate_agent.validate import _min_confidence, _split_number_name, validate_result


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


def _detail(
    zpid: str = "2101967478",
    zestimate: int | None = 799_900,
    street: str = "101 Lombard St",
    state: str = "CA",
    zipcode: str = "94111",
) -> PropertyDetail:
    return PropertyDetail(
        zpid_echo=zpid,
        zestimate=zestimate,
        full_address=f"{street}, San Francisco, {state} {zipcode}",
        raw={
            "zpid": int(zpid),
            "streetAddress": street,
            "city": "San Francisco",
            "state": state,
            "zipcode": zipcode,
            "zestimate": zestimate,
        },
    )


def _resolved(zpid: str = "2101967478", confidence: Confidence = Confidence.HIGH) -> ResolvedProperty:
    return ResolvedProperty(
        zpid=zpid,
        matched_address="101 Lombard St, San Francisco, CA 94111",
        confidence=confidence,
    )


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_split_number_name_standard() -> None:
    assert _split_number_name("101 Lombard St") == ("101", "Lombard St")


def test_split_number_name_with_unit() -> None:
    n, name = _split_number_name("233 E Erie St APT 1902")
    assert n == "233"
    assert name == "E Erie St APT 1902"


def test_split_number_name_no_number() -> None:
    n, name = _split_number_name("Main Street")
    assert n is None


def test_min_confidence_returns_lower() -> None:
    assert _min_confidence(Confidence.HIGH, Confidence.MEDIUM) == Confidence.MEDIUM
    assert _min_confidence(Confidence.MEDIUM, Confidence.HIGH) == Confidence.MEDIUM
    assert _min_confidence(Confidence.HIGH, Confidence.HIGH) == Confidence.HIGH


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_validate_happy_path() -> None:
    result = validate_result(_norm(), _detail(), _resolved(), "direct")
    assert isinstance(result, ZestimateResult)
    assert result.zestimate == 799_900
    assert result.zpid == "2101967478"
    assert result.confidence == Confidence.HIGH
    assert result.provider_used == "direct"


def test_validate_confidence_takes_min_of_stages() -> None:
    result = validate_result(
        _norm(confidence=Confidence.MEDIUM),
        _detail(),
        _resolved(confidence=Confidence.HIGH),
        "direct",
    )
    assert result.confidence == Confidence.MEDIUM


def test_validate_street_name_abbreviation_passes() -> None:
    # "Lombard Street" vs "Lombard St" — WRatio ≥ 85
    detail = _detail(street="101 Lombard Street")
    result = validate_result(_norm(street="Lombard St"), detail, _resolved(), "direct")
    assert result.zestimate == 799_900


def test_validate_unit_suffix_passes() -> None:
    # "E Erie St APT 1902" vs "E Erie St" — WRatio ≥ 85
    detail = _detail(street="233 E Erie St APT 1902", zipcode="60611", state="IL")
    norm = _norm(number="233", street="E Erie St", city="Chicago", state="IL", zip5="60611")
    resolved = _resolved(zpid="3866777")
    result = validate_result(norm, detail, resolved, "direct")
    assert result.zestimate == 799_900


# ---------------------------------------------------------------------------
# Field-level failures
# ---------------------------------------------------------------------------


def test_validate_street_number_mismatch_raises() -> None:
    detail = _detail(street="200 Lombard St")  # 200, not 101
    with pytest.raises(ValidationError) as exc_info:
        validate_result(_norm(), detail, _resolved(), "direct")
    assert "street_number" in exc_info.value.details


def test_validate_zip_mismatch_raises() -> None:
    detail = _detail(zipcode="94105")  # wrong zip
    with pytest.raises(ValidationError) as exc_info:
        validate_result(_norm(), detail, _resolved(), "direct")
    assert "zip5" in exc_info.value.details


def test_validate_state_mismatch_raises() -> None:
    detail = _detail(state="NY")
    with pytest.raises(ValidationError) as exc_info:
        validate_result(_norm(), detail, _resolved(), "direct")
    assert "state" in exc_info.value.details


def test_validate_street_name_low_score_raises() -> None:
    detail = _detail(street="101 Filbert St")  # different street
    with pytest.raises(ValidationError) as exc_info:
        validate_result(_norm(), detail, _resolved(), "direct")
    assert "street_name" in exc_info.value.details


def test_validate_zestimate_none_raises_no_zestimate_error() -> None:
    detail = _detail(zestimate=None)
    with pytest.raises(NoZestimateError) as exc_info:
        validate_result(_norm(), detail, _resolved(), "direct")
    assert exc_info.value.zpid == "2101967478"
    assert "Zestimate" in str(exc_info.value)


def test_validate_zestimate_too_low_raises() -> None:
    detail = _detail(zestimate=5_000)
    with pytest.raises(ValidationError) as exc_info:
        validate_result(_norm(), detail, _resolved(), "direct")
    assert "zestimate" in exc_info.value.details


def test_validate_zestimate_too_high_raises() -> None:
    detail = _detail(zestimate=600_000_000)
    with pytest.raises(ValidationError) as exc_info:
        validate_result(_norm(), detail, _resolved(), "direct")
    assert "zestimate" in exc_info.value.details


def test_validate_multiple_errors_reported_together() -> None:
    # Both number and zip are wrong — all errors should be collected
    detail = _detail(street="200 Lombard St", zipcode="94105")
    with pytest.raises(ValidationError) as exc_info:
        validate_result(_norm(), detail, _resolved(), "direct")
    assert "street_number" in exc_info.value.details
    assert "zip5" in exc_info.value.details
