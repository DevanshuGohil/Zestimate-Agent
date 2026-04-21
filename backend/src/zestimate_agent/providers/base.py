"""Abstract provider interface. Every data source must implement both methods."""

from __future__ import annotations

from abc import ABC, abstractmethod

from ..models import Candidate, NormalizedAddress, PropertyDetail


class Provider(ABC):
    """Provider contract used by Stages 2 (resolve) and 3 (fetch).

    Implementations MUST be idempotent and raise `ProviderError` on any
    network / HTTP / parsing failure so the agent can fall back.
    """

    name: str  # short identifier used in logs and ZestimateResult.provider_used

    @abstractmethod
    async def search(self, normalized: NormalizedAddress) -> list[Candidate]:
        """Return zero or more candidate properties for the given address."""

    @abstractmethod
    async def get_property(self, zpid: str) -> PropertyDetail:
        """Return full property detail for a zpid. Raises on failure."""
