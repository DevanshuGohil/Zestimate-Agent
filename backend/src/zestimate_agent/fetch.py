"""Stage 3: zpid -> PropertyDetail via a provider."""

from __future__ import annotations

import structlog

from .models import PropertyDetail
from .providers.base import Provider

log = structlog.get_logger(__name__)


async def fetch_property(zpid: str, provider: Provider) -> PropertyDetail:
    """Fetch full property detail for a zpid.

    The provider is responsible for raising `ProviderError` on any failure;
    this function only logs + forwards.
    """
    log.info("fetch.start", zpid=zpid, provider=provider.name)
    detail = await provider.get_property(zpid)
    log.info(
        "fetch.ok",
        zpid=zpid,
        zestimate=detail.zestimate,
        provider=provider.name,
    )
    return detail
