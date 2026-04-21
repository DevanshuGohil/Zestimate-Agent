"""Stage 2: NormalizedAddress -> ResolvedProperty via provider search."""

from __future__ import annotations

import structlog
from rapidfuzz import fuzz, utils as fuzz_utils

from .normalize import expand_suffixes

from .models import (
    AmbiguousAddressError,
    Candidate,
    Confidence,
    NormalizedAddress,
    ResolvedProperty,
)
from .providers.base import Provider

log = structlog.get_logger(__name__)

# Thresholds for street-name fuzzy match
_HIGH_THRESHOLD = 90
_MEDIUM_THRESHOLD = 70


async def resolve_zpid(normalized: NormalizedAddress, provider: Provider) -> ResolvedProperty:
    """Resolve a NormalizedAddress to a single zpid.

    Raises:
        AmbiguousAddressError: when zero candidates are found, or when multiple
            candidates are plausible and need LLM disambiguation.
        ProviderError: propagated from the provider on network / HTTP failure.
    """
    log.info("resolve.start", address=normalized.single_line())
    candidates = await provider.search(normalized)
    log.info("resolve.search_results", count=len(candidates))

    if not candidates:
        raise AmbiguousAddressError(
            f"no candidates found for '{normalized.single_line()}'",
            candidates=[],
        )

    return _disambiguate(normalized, candidates)


# ---------------------------------------------------------------------------
# Internal matching helpers (pure — no I/O)
# ---------------------------------------------------------------------------


def _disambiguate(normalized: NormalizedAddress, candidates: list[Candidate]) -> ResolvedProperty:
    """Pick the best candidate via exact street_number + zip5 + fuzzy street_name."""
    scored = _score_candidates(normalized, candidates)

    if not scored:
        raise AmbiguousAddressError(
            f"no candidates matched street_number + zip5 for "
            f"'{normalized.single_line()}'",
            candidates=candidates,
        )

    top_score, top_candidate = scored[0]
    second_score = scored[1][0] if len(scored) > 1 else 0

    if top_score >= _HIGH_THRESHOLD:
        if second_score >= _HIGH_THRESHOLD:
            plausible = [c for _, c in scored if _ >= _HIGH_THRESHOLD]
            raise AmbiguousAddressError(
                f"{len(plausible)} candidates match at HIGH confidence; "
                "LLM disambiguation required",
                candidates=plausible,
            )
        confidence = Confidence.HIGH
    elif top_score >= _MEDIUM_THRESHOLD:
        confidence = Confidence.MEDIUM
    else:
        raise AmbiguousAddressError(
            f"best candidate street-name score {top_score:.0f} below threshold "
            f"({_MEDIUM_THRESHOLD}) for '{normalized.single_line()}'",
            candidates=candidates,
        )

    result = ResolvedProperty(
        zpid=top_candidate.zpid,
        matched_address=_format_candidate(top_candidate),
        confidence=confidence,
    )
    log.info(
        "resolve.ok",
        zpid=result.zpid,
        confidence=result.confidence,
        score=round(top_score, 1),
    )
    return result


def _score_candidates(
    normalized: NormalizedAddress,
    candidates: list[Candidate],
) -> list[tuple[float, Candidate]]:
    """Return candidates that pass the number+zip filter, sorted by street-name score desc."""
    normed_street = (normalized.street_name or "").upper()
    normed_number = (normalized.street_number or "").upper()
    normed_zip = (normalized.zip5 or "")
    # Include unit in the fuzzy target so "APT 5" vs "APT 1" is distinguishable
    normed_target = normed_street
    if normalized.unit:
        normed_target = f"{normed_street} {normalized.unit.upper()}"

    filtered: list[tuple[float, Candidate]] = []
    for c in candidates:
        cand_number = (c.street_number or "").upper()
        cand_zip = (c.zip5 or "")

        if normed_number and cand_number and cand_number != normed_number:
            continue
        if normed_zip and cand_zip and cand_zip != normed_zip:
            continue

        # candidate street_name may already embed the unit (e.g. "MAIN ST APT 5")
        cand_street = (c.street_name or "").upper()
        if c.unit:
            cand_street = f"{cand_street} {c.unit.upper()}"

        score = (
            fuzz.WRatio(
                expand_suffixes(normed_target),
                expand_suffixes(cand_street),
                processor=fuzz_utils.default_process,
            )
            if cand_street
            else 0.0
        )
        filtered.append((score, c))

    return sorted(filtered, key=lambda t: t[0], reverse=True)


def _format_candidate(c: Candidate) -> str:
    parts = []
    if c.street_number and c.street_name:
        parts.append(f"{c.street_number} {c.street_name}")
    elif c.street_name:
        parts.append(c.street_name)
    if c.city:
        parts.append(c.city)
    state_zip = " ".join(filter(None, [c.state, c.zip5]))
    if state_zip:
        parts.append(state_zip)
    return ", ".join(parts)
