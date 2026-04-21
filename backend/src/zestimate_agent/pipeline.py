"""Orchestrates the 4-stage pipeline (no agent / LangGraph layer).

Stage order:
  1. normalize  — free-text address → NormalizedAddress
  2. resolve    — NormalizedAddress → ResolvedProperty (zpid)
  3. fetch      — zpid → PropertyDetail
  4. validate   — cross-check + sanity → ZestimateResult

Errors from any stage propagate up. The CLI (Step 6) and the LangGraph agent
(Step 7) both call run_pipeline; the agent adds retry, disambiguation, and
cache on top.
"""

from __future__ import annotations

import time

import structlog

from .config import Settings, get_settings
from .fetch import fetch_property
from .models import ZestimateResult
from .normalize import normalize_address
from .providers.base import Provider
from .providers.direct import DirectProvider
from .resolve import resolve_zpid
from .validate import validate_result

log = structlog.get_logger(__name__)


async def run_pipeline(
    raw_address: str,
    provider: Provider | None = None,
    settings: Settings | None = None,
) -> ZestimateResult:
    """Run the full 4-stage pipeline end-to-end.

    Args:
        raw_address: free-text US property address
        provider:    data provider; defaults to DirectProvider
        settings:    app config; defaults to get_settings()

    Returns:
        ZestimateResult with the current Zestimate for the address.

    Raises:
        AmbiguousAddressError: address could not be normalized or resolved
        ValidationError: resolved property failed cross-checks
        ProviderError: provider failed after retries
    """
    if settings is None:
        settings = get_settings()
    if provider is None:
        provider = DirectProvider(proxy_url=settings.proxy_url.get_secret_value() if settings.proxy_url else None)

    start = time.monotonic()
    log.info("pipeline.start", address=raw_address, provider=provider.name)

    normalized = await normalize_address(raw_address, settings)
    resolved = await resolve_zpid(normalized, provider)
    detail = await fetch_property(resolved.zpid, provider)
    result = validate_result(normalized, detail, resolved, provider.name)

    elapsed_ms = int((time.monotonic() - start) * 1000)
    log.info(
        "pipeline.ok",
        address=raw_address,
        zpid=result.zpid,
        zestimate=result.zestimate,
        confidence=str(result.confidence),
        provider=result.provider_used,
        elapsed_ms=elapsed_ms,
    )
    return result
