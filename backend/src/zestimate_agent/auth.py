"""Rate-limiting dependency (no authentication)."""

from __future__ import annotations

from fastapi import Request
from slowapi import Limiter
from slowapi.util import get_remote_address


def _limit_key(request: Request) -> str:
    return get_remote_address(request)


limiter = Limiter(key_func=_limit_key)
