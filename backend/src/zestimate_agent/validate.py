"""Stage 4: cross-check NormalizedAddress against PropertyDetail → ZestimateResult.

Checks (all must pass):
  1. street_number exact equality
  2. zip5 exact equality
  3. state exact equality
  4. street_name fuzzy ≥ 85 (WRatio — tolerates "St" vs "Street", unit suffixes)
  5. zestimate is a non-None int in [$10k, $500M]

If any check fails, raises ValidationError with a details dict so the retry
node can decide whether to try another provider or surface an error.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import structlog
from rapidfuzz import fuzz
from rapidfuzz import utils as fuzz_utils

from .models import (
    Confidence,
    NormalizedAddress,
    NoZestimateError,
    PropertyDetail,
    ResolvedProperty,
    ValidationError,
    ZestimateResult,
)
from .normalize import expand_suffixes

log = structlog.get_logger(__name__)

_STREET_FUZZY_THRESHOLD = 85
_ZESTIMATE_MIN = 10_000
_ZESTIMATE_MAX = 500_000_000
_CONFIDENCE_ORDER = {Confidence.HIGH: 2, Confidence.MEDIUM: 1, Confidence.LOW: 0}


def validate_result(
    normalized: NormalizedAddress,
    detail: PropertyDetail,
    resolved: ResolvedProperty,
    provider_name: str,
) -> ZestimateResult:
    """Validate PropertyDetail against the NormalizedAddress.

    Returns a ZestimateResult on success.
    Raises ValidationError if any check fails.
    """
    errors: dict[str, str] = {}
    p = detail.raw

    detail_street = p.get("streetAddress", "")
    detail_number, detail_name = _split_number_name(detail_street)
    detail_zip5 = (p.get("zipcode") or "")[:5]
    detail_state = (p.get("state") or "").upper()

    # 1. Street number — exact
    if normalized.street_number and detail_number:
        if normalized.street_number.upper() != detail_number.upper():
            errors["street_number"] = (
                f"expected {normalized.street_number!r}, got {detail_number!r}"
            )

    # 2. Zip5 — exact
    if normalized.zip5 and detail_zip5:
        if normalized.zip5 != detail_zip5:
            errors["zip5"] = f"expected {normalized.zip5!r}, got {detail_zip5!r}"

    # 3. State — exact (both must be 2-letter codes by this point)
    if normalized.state and detail_state:
        if normalized.state != detail_state:
            errors["state"] = f"expected {normalized.state!r}, got {detail_state!r}"

    # 4. Street name — WRatio ≥ 85 (handles abbreviations + unit suffixes)
    if normalized.street_name and detail_name:
        score = fuzz.WRatio(
            expand_suffixes(normalized.street_name),
            expand_suffixes(detail_name),
            processor=fuzz_utils.default_process,
        )
        if score < _STREET_FUZZY_THRESHOLD:
            errors["street_name"] = (
                f"fuzzy score {score:.0f} < {_STREET_FUZZY_THRESHOLD} "
                f"(normalized={normalized.street_name!r}, detail={detail_name!r})"
            )

    # Raise address mismatch errors before checking zestimate — if the wrong
    # property was resolved, NoZestimateError would be misleading.
    if errors:
        msg = f"validation failed for zpid={detail.zpid_echo}: " + "; ".join(
            f"{k}={v}" for k, v in errors.items()
        )
        log.warning("validate.failed", zpid=detail.zpid_echo, errors=errors)
        raise ValidationError(msg, details=errors)

    # 5. Zestimate — address checks passed, so we have the right property.
    #    None means Zillow simply doesn't publish a Zestimate for it.
    if detail.zestimate is None:
        msg = (
            f"Zillow does not publish a Zestimate for this property "
            f"(zpid={detail.zpid_echo}, address={detail.full_address!r})"
        )
        log.warning("validate.no_zestimate", zpid=detail.zpid_echo, address=detail.full_address)
        raise NoZestimateError(msg, zpid=detail.zpid_echo)
    if not isinstance(detail.zestimate, int):
        raise ValidationError(
            f"zestimate type error: expected int, got {type(detail.zestimate).__name__}",
            details={"zestimate": f"type={type(detail.zestimate).__name__}"},
        )
    if not (_ZESTIMATE_MIN <= detail.zestimate <= _ZESTIMATE_MAX):
        raise ValidationError(
            f"zestimate {detail.zestimate:,} out of range [{_ZESTIMATE_MIN:,}, {_ZESTIMATE_MAX:,}]",
            details={"zestimate": f"{detail.zestimate:,} out of range"},
        )

    # Combine Stage 1 + Stage 2 confidence — take the more conservative of the two
    confidence = _min_confidence(normalized.confidence, resolved.confidence)

    result = ZestimateResult(
        address=resolved.matched_address or normalized.single_line(),
        zestimate=detail.zestimate,  # type: ignore[arg-type]  # None ruled out above
        zpid=detail.zpid_echo,
        fetched_at=datetime.now(tz=timezone.utc),
        provider_used=provider_name,
        confidence=confidence,
    )
    log.info(
        "validate.ok",
        zpid=result.zpid,
        zestimate=result.zestimate,
        address=result.address,
        confidence=result.confidence,
    )
    return result


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _split_number_name(addr: str) -> tuple[str | None, str | None]:
    parts = addr.strip().split(None, 1)
    if not parts:
        return None, None
    number = parts[0] if parts[0][:1].isdigit() else None
    name = parts[1] if len(parts) > 1 else (None if number else parts[0])
    return number, name


def _min_confidence(a: Confidence, b: Confidence) -> Confidence:
    return a if _CONFIDENCE_ORDER[a] <= _CONFIDENCE_ORDER[b] else b


