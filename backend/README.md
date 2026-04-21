# Zestimate Agent — Backend

FastAPI service wrapping a LangGraph agent that fetches the current Zillow Zestimate for any US property address.

## API endpoints

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/lookup` | Full address → Zestimate (blocking) |
| `POST` | `/lookup/stream` | Full address → SSE progress stream |
| `POST` | `/lookup/zpid` | Known zpid → Zestimate (skips resolve) |
| `DELETE` | `/cache` | Clear all cached results |
| `GET` | `/health` | Liveness probe |
| `GET` | `/health/ready` | Readiness probe (checks settings + DB) |
| `GET` | `/metrics` | Prometheus metrics |
| `GET` | `/docs` | Swagger UI |

### `POST /lookup`

```json
// request
{ "address": "101 Lombard St, San Francisco, CA 94111", "no_cache": false }

// 200 response
{
  "address": "101 LOMBARD ST, SAN FRANCISCO, CA 94111",
  "zestimate": 1250000,
  "zpid": "2101967478",
  "confidence": "HIGH",
  "provider_used": "direct",
  "fetched_at": "2026-04-21T18:00:00Z",
  "cache_hit": false,
  "elapsed_ms": 3100
}
```

### `POST /lookup/stream`

Same request body as `/lookup`. Responds with `Content-Type: text/event-stream`. Each line is a JSON SSE event:

```
data: {"type":"step","node":"normalize","status":"running","label":"Normalizing address"}
data: {"type":"step","node":"normalize","status":"done","label":"Normalizing address","detail":{"address":"101 LOMBARD ST, SAN FRANCISCO, CA 94111","confidence":"HIGH"}}
data: {"type":"step","node":"resolve","status":"running","label":"Searching Zillow"}
...
data: {"type":"result","data":{...ZestimateResponse...}}
```

Event `type` values: `step` | `result` | `error`  
Step `status` values: `running` | `done` | `error`

### `POST /lookup/zpid`

```json
// request
{ "zpid": "2101967478", "no_cache": false }
// response — same shape as /lookup
```

## Pipeline

```
raw address
    │
    ▼
┌──────────┐   NormalizedAddress    ┌─────────┐   zpid   ┌───────┐   PropertyDetail   ┌──────────┐
│ normalize│ ─────────────────────▶ │ resolve │ ───────▶ │ fetch │ ─────────────────▶ │ validate │
└──────────┘                        └─────────┘           └───────┘                   └──────────┘
     │                                   │                    │                              │
     │         ┌────────────┐            │                    │                              │
     └─error──▶│    retry   │◀───────────┘◀───────────────────┘                              │
               └────────────┘                                                                │
                     │ max retries exceeded                                          ZestimateResult
                     ▼
               ┌───────────┐
               │  clarify  │  (terminal error)
               └───────────┘
```

The LLM (Mistral) is invoked only at the **disambiguate** node, when Stage 2 finds multiple HIGH-confidence address matches.

## Setup

Requires Python 3.11+ and [uv](https://github.com/astral-sh/uv).

```bash
cd backend
uv sync --extra dev
cp .env.example .env
# edit .env — fill in at minimum MISTRAL_API_KEY and RAPIDAPI_KEY
```

## Configuration

All settings are read from `backend/.env` (or environment variables).

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `MISTRAL_API_KEY` | Yes | — | LLM for disambiguation (Mistral free tier works) |
| `RAPIDAPI_KEY` | Yes | — | RapidAPI key for Zillow56 fallback provider |
| `RAPIDAPI_HOST` | No | `zillow56.p.rapidapi.com` | RapidAPI host |
| `GOOGLE_MAPS_API_KEY` | No | — | Improves address normalization accuracy |
| `PROXY_URL` | No | — | HTTP proxy for DirectProvider (`http://user:pass@host:port`) |
| `CACHE_TTL_HOURS` | No | `1` | How long successful results are cached |
| `CACHE_FAILURE_TTL_HOURS` | No | `6` | How long failed lookups are cached |
| `CACHE_DIR` | No | platform data dir | SQLite database directory |
| `REQUEST_TIMEOUT_SECONDS` | No | `30` | Max seconds per `/lookup` request |
| `RATE_LIMIT_LOOKUP` | No | `10/minute` | Rate limit for lookup endpoints (per IP) |
| `RATE_LIMIT_CACHE` | No | `5/minute` | Rate limit for cache endpoint (per IP) |
| `LOG_LEVEL` | No | `INFO` | `DEBUG` / `INFO` / `WARNING` / `ERROR` |
| `LANGSMITH_API_KEY` | No | — | Enable LangSmith graph tracing |
| `LANGSMITH_TRACING` | No | `false` | Set `true` to activate tracing |
| `CORS_ORIGINS` | No | `` | Comma-separated allowed origins (empty = same-origin only) |

## Development

```bash
# Run dev server (auto-reload)
uv run uvicorn zestimate_agent.api:app --reload --port 8000

# Run tests
uv run pytest tests/ --ignore=tests/test_agent.py -q

# Run live tests (hit real Zillow)
uv run pytest -m live tests/ -v

# Lint + type-check
uv run ruff check src/ tests/
uv run mypy src/

# CLI
uv run zestimate lookup "101 Lombard St, San Francisco, CA 94111"
uv run zestimate lookup --no-cache --json "101 Lombard St, San Francisco, CA 94111"
```

## Docker

```bash
# From the repo root
docker compose up --build

# Backend only
docker build -t zestimate-api ./backend
docker run -p 8000:8000 --env-file ./backend/.env zestimate-api
```

## Source layout

```
backend/
├── src/zestimate_agent/
│   ├── agent.py          LangGraph graph + streaming entry point
│   ├── api.py            FastAPI app + all routes
│   ├── pipeline.py       run_pipeline() (no agent layer)
│   ├── normalize.py      Stage 1: address normalization
│   ├── resolve.py        Stage 2: address → zpid via provider search
│   ├── fetch.py          Stage 3: zpid → PropertyDetail
│   ├── validate.py       Stage 4: cross-checks + ZestimateResult
│   ├── cache.py          SQLite TTL cache
│   ├── config.py         pydantic-settings (reads .env)
│   ├── models.py         all Pydantic models + GraphState
│   ├── auth.py           IP-based rate limiting (slowapi)
│   ├── circuit_breaker.py per-provider circuit breaker
│   ├── observability.py  structlog + LangSmith setup
│   └── providers/
│       ├── base.py       Provider ABC
│       ├── direct.py     DirectProvider (curl_cffi → zillow.com __NEXT_DATA__)
│       └── rapidapi.py   RapidAPIProvider (Zillow56 via httpx)
├── tests/                150 unit tests
├── evals/                accuracy eval harness
├── Dockerfile
├── pyproject.toml
└── .env.example
```
