"""Custom ASGI middleware for the Zestimate API."""

from __future__ import annotations

import uuid
from typing import Callable

import structlog
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

_REQUEST_ID_HEADER = "X-Request-ID"


class CorrelationIdMiddleware(BaseHTTPMiddleware):
    """Inject a correlation ID into every request.

    Reads X-Request-ID from the incoming request if present; otherwise
    generates a fresh UUID4.  The ID is:
      - Bound into structlog's contextvars so every log line emitted during
        the request automatically includes `request_id=<id>`.
      - Returned in the X-Request-ID response header so callers can correlate
        their client-side logs with server-side logs.
    """

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        request_id = request.headers.get(_REQUEST_ID_HEADER) or str(uuid.uuid4())
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id=request_id)
        try:
            response: Response = await call_next(request)
        finally:
            structlog.contextvars.clear_contextvars()
        response.headers[_REQUEST_ID_HEADER] = request_id
        return response
