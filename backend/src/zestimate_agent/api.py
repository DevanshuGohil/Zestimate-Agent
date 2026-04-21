"""FastAPI server for the Zestimate agent.

Start:
    uvicorn zestimate_agent.api:app --reload

Or via the package entry point:
    zestimate-api
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

import httpx
import structlog
from fastapi import FastAPI, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from prometheus_fastapi_instrumentator import Instrumentator
from prometheus_client import Counter
from pydantic import BaseModel, Field, field_validator
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

from .agent import run_agent, stream_agent
from .auth import limiter
from .cache import Cache
from .config import get_settings
from .fetch import fetch_property
from .middleware import CorrelationIdMiddleware
from .models import (
    Confidence,
    ClarificationRequest,
    NoZestimateError,
    PropertyDetail,
    ZestimateResult,
)
from .normalize import normalize_address, set_shared_http_client
from .observability import configure as configure_observability
from .providers.direct import DirectProvider

_LOOKUP_COUNTER = Counter(
    "zestimate_lookups_total",
    "Total Zestimate lookups by outcome",
    ["cache_hit", "confidence"],
)

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):  # type: ignore[type-arg]
    # Fail fast — if required settings are missing, refuse to start rather
    # than serving 500s until someone notices.
    settings = get_settings()
    configure_observability(settings)

    http_client = httpx.AsyncClient()
    set_shared_http_client(http_client)

    # Store settings on app.state so the readiness probe can verify them
    app.state.settings_ok = True
    log.info("api.startup", version=app.version)

    yield

    await http_client.aclose()
    set_shared_http_client(None)
    log.info("api.shutdown")


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

def _rate_limit_exceeded_handler(request: Request, exc: RateLimitExceeded) -> None:
    raise HTTPException(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        detail=f"Rate limit exceeded: {exc.detail}. Try again later.",
    )


app = FastAPI(
    title="Zestimate Agent API",
    description="Fetch the current Zillow Zestimate for any US property address.",
    version="0.1.0",
    lifespan=lifespan,
)

# CORS — restrict to configured origins; falls back to same-origin only
_cors_origins = [o.strip() for o in __import__("os").getenv("CORS_ORIGINS", "").split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins or [],
    allow_credentials=True,
    allow_methods=["GET", "POST", "DELETE"],
    allow_headers=["*"],
)

# Rate limiting
app.state.limiter = limiter
app.add_middleware(SlowAPIMiddleware)
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)  # type: ignore[arg-type]

# Correlation IDs — added after CORS so request_id is bound before route handlers log
app.add_middleware(CorrelationIdMiddleware)

# Prometheus metrics — exposes /metrics endpoint
Instrumentator().instrument(app).expose(app)


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------


class LookupRequest(BaseModel):
    address: str = Field(..., description="Full US property address", min_length=10, max_length=500)
    no_cache: bool = Field(False, description="Bypass the local SQLite cache")

    @field_validator("address", mode="before")
    @classmethod
    def strip_address(cls, v: str) -> str:
        return v.strip()


class ZestimateResponse(BaseModel):
    address: str
    zestimate: int
    zpid: str
    confidence: str
    provider_used: str
    fetched_at: str
    cache_hit: bool
    elapsed_ms: int


class ClarificationDetail(BaseModel):
    reason: str
    original_input: str
    candidates: list[dict[str, Any]]


class HealthResponse(BaseModel):
    status: str
    version: str


class ReadinessResponse(BaseModel):
    status: str
    version: str
    checks: dict[str, str]


class CacheStatsResponse(BaseModel):
    cleared: bool


class ZpidRequest(BaseModel):
    zpid: str = Field(..., description="Zillow property ID", min_length=1, max_length=30)
    no_cache: bool = Field(False, description="Bypass the local SQLite cache")

    @field_validator("zpid", mode="before")
    @classmethod
    def strip_zpid(cls, v: str) -> str:
        return str(v).strip()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _raise_for_clarification(address: str, cr: ClarificationRequest) -> None:
    """Convert a ClarificationRequest terminal state to the correct HTTP error.

    Mapping:
      cr.zpid set         → 404 (NoZestimateError: property found, no estimate)
      cr.candidates       → 422 (ambiguous address, multiple matches)
      "validation failed" → 422 (resolved property failed cross-checks)
      otherwise           → 503 (provider / max-retry failure)
    """
    if cr.zpid is not None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "reason": cr.reason,
                "zpid": cr.zpid,
                "original_input": address,
                "hint": (
                    "Zillow does not publish Zestimates for all properties "
                    "(common for rentals, new construction, and some condos)."
                ),
            },
        )
    if cr.candidates:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=ClarificationDetail(
                reason=cr.reason,
                original_input=address,
                candidates=cr.candidates,
            ).model_dump(),
        )
    reason_lower = cr.reason.lower()
    if "validation failed" in reason_lower:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"reason": cr.reason, "original_input": address, "candidates": []},
        )
    raise HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail=f"Provider error: {cr.reason}",
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.get("/health", response_model=HealthResponse, tags=["meta"])
async def health() -> HealthResponse:
    """Liveness probe — always 200 while the process is up."""
    return HealthResponse(status="ok", version=app.version)


@app.get(
    "/health/ready",
    response_model=ReadinessResponse,
    responses={503: {"description": "Service not ready"}},
    tags=["meta"],
)
async def readiness(request: Request) -> ReadinessResponse:
    """Readiness probe — 200 only when settings loaded and cache DB is reachable."""
    checks: dict[str, str] = {}
    failed = False

    # Settings check
    if getattr(request.app.state, "settings_ok", False):
        checks["settings"] = "ok"
    else:
        checks["settings"] = "error"
        failed = True

    # Cache DB check — attempt a lightweight read
    try:
        settings = get_settings()
        cache = Cache(settings.cache_db_path, settings.cache_ttl_hours, settings.cache_failure_ttl_hours)
        await cache.lookup("__readiness_probe__")
        checks["cache_db"] = "ok"
    except Exception as e:
        checks["cache_db"] = f"error: {e}"
        failed = True

    if failed:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"status": "not ready", "checks": checks},
        )
    return ReadinessResponse(status="ready", version=app.version, checks=checks)


@app.post(
    "/lookup",
    response_model=ZestimateResponse,
    responses={
        404: {"description": "Property found but Zillow has no Zestimate for it"},
        422: {"description": "Address could not be resolved (clarification needed)"},
        429: {"description": "Rate limit exceeded"},
        503: {"description": "Upstream provider error"},
    },
    tags=["zestimate"],
)
@limiter.limit(lambda: get_settings().rate_limit_lookup)
async def lookup(
    request: Request,
    req: LookupRequest,
) -> ZestimateResponse:
    """Return the current Zillow Zestimate for the given US property address."""
    t0 = time.monotonic()

    try:
        settings = get_settings()
    except Exception:
        log.exception("api.lookup.settings_error")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Server configuration error. Contact the administrator.",
        )

    cache = Cache(settings.cache_db_path, settings.cache_ttl_hours, settings.cache_failure_ttl_hours)
    cache_hit = False
    result: ZestimateResult | None = None

    # --- Cache probe ---
    cache_key: str | None = None
    if not req.no_cache:
        try:
            normalized = await normalize_address(req.address, settings)
            cache_key = Cache.make_key(normalized.single_line())
            hit = await cache.lookup(cache_key)
            if hit.hit and hit.was_failure:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=(
                        "Address not found in Zillow (cached failure). "
                        "Pass no_cache=true to retry."
                    ),
                )
            if hit.hit and hit.result is not None:
                cache_hit = True
                result = hit.result
        except HTTPException:
            raise
        except Exception:
            # Normalization error — skip cache, let agent handle it
            cache_key = None

    # --- Agent (LangGraph pipeline with retry + LLM disambiguation) ---
    if result is None:
        try:
            outcome = await asyncio.wait_for(
                run_agent(req.address),
                timeout=settings.request_timeout_seconds,
            )
        except asyncio.TimeoutError:
            log.warning(
                "api.lookup.timeout",
                address=req.address,
                timeout_s=settings.request_timeout_seconds,
            )
            raise HTTPException(
                status_code=status.HTTP_504_GATEWAY_TIMEOUT,
                detail=f"Request timed out after {settings.request_timeout_seconds}s.",
            )
        if isinstance(outcome, ZestimateResult):
            result = outcome
        else:
            _raise_for_clarification(req.address, outcome)

        # --- Cache store ---
        if not req.no_cache:
            if cache_key is None:
                try:
                    normalized = await normalize_address(req.address, settings)
                    cache_key = Cache.make_key(normalized.single_line())
                except Exception:
                    cache_key = Cache.make_key(req.address.upper().strip())
            await cache.store(cache_key, result)  # type: ignore[arg-type]

    elapsed_ms = int((time.monotonic() - t0) * 1000)
    _LOOKUP_COUNTER.labels(
        cache_hit=str(cache_hit).lower(),
        confidence=result.confidence.value,
    ).inc()
    log.info(
        "api.lookup.ok",
        address=result.address,
        zestimate=result.zestimate,
        cache_hit=cache_hit,
        elapsed_ms=elapsed_ms,
    )

    return ZestimateResponse(
        address=result.address,
        zestimate=result.zestimate,
        zpid=result.zpid,
        confidence=result.confidence.value,
        provider_used=result.provider_used,
        fetched_at=result.fetched_at.isoformat(),
        cache_hit=cache_hit,
        elapsed_ms=elapsed_ms,
    )


@app.delete(
    "/cache",
    response_model=CacheStatsResponse,
    responses={
        429: {"description": "Rate limit exceeded"},
    },
    tags=["meta"],
)
@limiter.limit(lambda: get_settings().rate_limit_cache)
async def clear_cache(
    request: Request,
) -> CacheStatsResponse:
    """Clear all cached Zestimate results."""
    try:
        settings = get_settings()
        await Cache(
            settings.cache_db_path, settings.cache_ttl_hours, settings.cache_failure_ttl_hours
        ).clear()
        log.info("api.cache.cleared")
        return CacheStatsResponse(cleared=True)
    except Exception:
        log.exception("api.cache.clear_error")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to clear cache.",
        )


def _result_from_detail(detail: PropertyDetail, provider_name: str) -> ZestimateResult:
    """Build a ZestimateResult directly from PropertyDetail (zpid path — no address checks)."""
    if detail.zestimate is None:
        raise NoZestimateError(
            f"Zillow does not publish a Zestimate for this property "
            f"(zpid={detail.zpid_echo}, address={detail.full_address!r})",
            zpid=detail.zpid_echo,
        )
    return ZestimateResult(
        address=detail.full_address,
        zestimate=detail.zestimate,
        zpid=detail.zpid_echo,
        fetched_at=datetime.now(tz=timezone.utc),
        provider_used=provider_name,
        confidence=Confidence.HIGH,
    )


@app.post(
    "/lookup/zpid",
    response_model=ZestimateResponse,
    responses={
        404: {"description": "Property found but Zillow has no Zestimate for it"},
        429: {"description": "Rate limit exceeded"},
        503: {"description": "Upstream provider error"},
    },
    tags=["zestimate"],
)
@limiter.limit(lambda: get_settings().rate_limit_lookup)
async def lookup_by_zpid(
    request: Request,
    req: ZpidRequest,
) -> ZestimateResponse:
    """Fetch the Zestimate for a known Zillow zpid, skipping address resolution."""
    t0 = time.monotonic()

    try:
        settings = get_settings()
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Server configuration error.",
        )

    cache = Cache(settings.cache_db_path, settings.cache_ttl_hours, settings.cache_failure_ttl_hours)
    cache_key = Cache.make_key(f"zpid:{req.zpid}")
    cache_hit = False
    result: ZestimateResult | None = None

    if not req.no_cache:
        hit = await cache.lookup(cache_key)
        if hit.hit and hit.was_failure:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="No Zestimate cached for this zpid (cached failure). Pass no_cache=true to retry.",
            )
        if hit.hit and hit.result is not None:
            cache_hit = True
            result = hit.result

    if result is None:
        try:
            provider = DirectProvider(
                proxy_url=settings.proxy_url.get_secret_value() if settings.proxy_url else None
            )
            detail = await asyncio.wait_for(
                fetch_property(req.zpid, provider),
                timeout=settings.request_timeout_seconds,
            )
            result = _result_from_detail(detail, provider.name)
        except asyncio.TimeoutError:
            raise HTTPException(
                status_code=status.HTTP_504_GATEWAY_TIMEOUT,
                detail=f"Request timed out after {settings.request_timeout_seconds}s.",
            )
        except NoZestimateError as e:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={
                    "reason": str(e),
                    "zpid": e.zpid,
                    "hint": "Zillow does not publish Zestimates for all properties.",
                },
            )
        except Exception as e:
            log.warning("api.lookup_zpid.error", zpid=req.zpid, error=str(e))
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=f"Provider error: {e}",
            )

        if not req.no_cache:
            await cache.store(cache_key, result)

    elapsed_ms = int((time.monotonic() - t0) * 1000)
    _LOOKUP_COUNTER.labels(
        cache_hit=str(cache_hit).lower(),
        confidence=result.confidence.value,
    ).inc()
    log.info("api.lookup_zpid.ok", zpid=result.zpid, zestimate=result.zestimate, cache_hit=cache_hit)

    return ZestimateResponse(
        address=result.address,
        zestimate=result.zestimate,
        zpid=result.zpid,
        confidence=result.confidence.value,
        provider_used=result.provider_used,
        fetched_at=result.fetched_at.isoformat(),
        cache_hit=cache_hit,
        elapsed_ms=elapsed_ms,
    )


# ---------------------------------------------------------------------------
# Streaming helpers
# ---------------------------------------------------------------------------


def _sse(data: dict[str, Any]) -> str:
    return f"data: {json.dumps(data, default=str)}\n\n"


def _result_to_dict(result: ZestimateResult, cache_hit: bool, elapsed_ms: int) -> dict[str, Any]:
    return {
        "address": result.address,
        "zestimate": result.zestimate,
        "zpid": result.zpid,
        "confidence": result.confidence.value,
        "provider_used": result.provider_used,
        "fetched_at": result.fetched_at.isoformat(),
        "cache_hit": cache_hit,
        "elapsed_ms": elapsed_ms,
    }


def _clarification_to_sse_error(cr: ClarificationRequest | None) -> dict[str, Any]:
    if cr is None:
        return {"type": "error", "status": 503, "message": "Unknown error."}
    if cr.zpid is not None:
        return {
            "type": "error", "status": 404, "message": cr.reason, "zpid": cr.zpid,
            "hint": "Zillow does not publish Zestimates for all properties.",
        }
    if cr.candidates:
        return {"type": "error", "status": 422, "message": cr.reason, "candidates": cr.candidates}
    if "validation failed" in cr.reason.lower():
        return {"type": "error", "status": 422, "message": cr.reason}
    return {"type": "error", "status": 503, "message": cr.reason}


# ---------------------------------------------------------------------------
# POST /lookup/stream — SSE progress stream
# ---------------------------------------------------------------------------


@app.post("/lookup/stream", tags=["zestimate"])
@limiter.limit(lambda: get_settings().rate_limit_lookup)
async def lookup_stream(request: Request, req: LookupRequest) -> StreamingResponse:
    """Stream real-time node-level progress events for a Zestimate lookup.

    Emits Server-Sent Events (text/event-stream).  Each event is a JSON object:
      {"type": "step",   "node": str, "status": "running"|"done"|"error", "label": str}
      {"type": "result", "data": ZestimateResponse}
      {"type": "error",  "status": int, "message": str, ...}
    """

    async def generate() -> AsyncGenerator[str, None]:
        t0 = time.monotonic()

        try:
            settings = get_settings()
        except Exception:
            yield _sse({"type": "error", "status": 500, "message": "Server configuration error."})
            return

        cache = Cache(
            settings.cache_db_path, settings.cache_ttl_hours, settings.cache_failure_ttl_hours
        )
        cache_key: str | None = None

        # --- Cache check ---
        if not req.no_cache:
            yield _sse({"type": "step", "node": "cache", "status": "running", "label": "Checking cache"})
            try:
                normalized = await normalize_address(req.address, settings)
                cache_key = Cache.make_key(normalized.single_line())
                hit = await cache.lookup(cache_key)

                if hit.hit and hit.was_failure:
                    yield _sse({"type": "step", "node": "cache", "status": "done", "label": "Checking cache"})
                    yield _sse({"type": "error", "status": 404,
                                "message": "Address not found in Zillow (cached failure). Pass no_cache=true to retry."})
                    return

                if hit.hit and hit.result is not None:
                    yield _sse({"type": "step", "node": "cache", "status": "done", "label": "Cache hit"})
                    elapsed_ms = int((time.monotonic() - t0) * 1000)
                    yield _sse({"type": "result", "data": _result_to_dict(hit.result, True, elapsed_ms)})
                    return

                yield _sse({"type": "step", "node": "cache", "status": "done", "label": "Cache miss — running agent"})
            except Exception:
                yield _sse({"type": "step", "node": "cache", "status": "done", "label": "Cache skipped"})

        # --- Stream agent nodes ---
        raw_result: ZestimateResult | None = None

        try:
            async with asyncio.timeout(settings.request_timeout_seconds):
                async for evt in stream_agent(req.address):
                    if evt["type"] == "result":
                        raw_result = evt.pop("_result", None)
                        if raw_result is not None:
                            elapsed_ms = int((time.monotonic() - t0) * 1000)
                            yield _sse({
                                "type": "result",
                                "data": _result_to_dict(raw_result, False, elapsed_ms),
                            })
                            _LOOKUP_COUNTER.labels(
                                cache_hit="false",
                                confidence=raw_result.confidence.value,
                            ).inc()
                        else:
                            yield _sse({"type": "error", "status": 503, "message": "Agent returned no result."})
                        break
                    elif evt["type"] == "clarify":
                        cr = evt.pop("_clarification", None)
                        yield _sse(_clarification_to_sse_error(cr))
                        break
                    else:
                        yield _sse(evt)

        except (asyncio.TimeoutError, TimeoutError):
            yield _sse({"type": "error", "status": 504,
                        "message": f"Request timed out after {settings.request_timeout_seconds}s."})
            return
        except Exception as e:
            log.exception("api.lookup_stream.error", address=req.address, error=str(e))
            yield _sse({"type": "error", "status": 503, "message": "Provider error."})
            return

        # --- Cache store ---
        if raw_result and not req.no_cache:
            try:
                if cache_key is None:
                    try:
                        normalized = await normalize_address(req.address, settings)
                        cache_key = Cache.make_key(normalized.single_line())
                    except Exception:
                        cache_key = Cache.make_key(req.address.upper().strip())
                await cache.store(cache_key, raw_result)
            except Exception:
                pass

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ---------------------------------------------------------------------------
# Entry point for `zestimate-api` script
# ---------------------------------------------------------------------------


def run() -> None:
    import os

    import uvicorn

    uvicorn.run(
        "zestimate_agent.api:app",
        host=os.getenv("API_HOST", "0.0.0.0"),
        port=int(os.getenv("API_PORT", "8000")),
        workers=int(os.getenv("API_WORKERS", "1")),
    )


if __name__ == "__main__":
    run()
