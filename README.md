# Zestimate Agent

Production-grade tool that, given any US property address, returns the **current Zillow Zestimate** — the exact value Zillow displays on its website. Target accuracy: ≥99% exact match.

## Architecture

![System Architecture](zestimate_agent_architecture.svg)

```
                     ┌──────────────────────────────────────────────────────────┐
                     │  Docker Compose                                          │
                     │                                                          │
  Browser ──────────▶│  ui (nginx :8080)                                       │
                     │    ├─ serves React SPA (static bundle)                  │
                     │    └─ /api/* ──────────────────────────────────────────▶│
                     │                                                          │
                     │  api (FastAPI :8000)                                     │
                     │    ├─ POST /lookup          full address lookup          │
                     │    ├─ POST /lookup/stream   SSE streaming progress       │
                     │    ├─ POST /lookup/zpid     direct zpid shortcut         │
                     │    └─ DELETE /cache         clear SQLite cache           │
                     │          │                                               │
                     │          ▼                                               │
                     │   LangGraph Agent                                        │
                     │    normalize → resolve → fetch → validate                │
                     │         │           │        │                           │
                     │         ▼           ▼        ▼                           │
                     │    Nominatim    DirectProvider  ────────────────────────▶│ zillow.com
                     │    (geocode)   (curl_cffi HTML)                          │
                     │                  + RapidAPI fallback ──────────────────▶│ RapidAPI
                     │          │                                               │
                     │          ▼                                               │
                     │    SQLite cache (TTL: 1 h)                               │
                     └──────────────────────────────────────────────────────────┘
```

### Pipeline stages

| # | Stage | Input → Output | Notes |
|---|-------|---------------|-------|
| 1 | **Normalize** | raw string → `NormalizedAddress` | Nominatim geocoding; splits number/street/unit/city/state/zip |
| 2 | **Resolve** | `NormalizedAddress` → zpid | Provider search + rapidfuzz street-name match; unit-aware |
| 3 | **Fetch** | zpid → `PropertyDetail` | `DirectProvider` reads `__NEXT_DATA__` from zillow.com; RapidAPI is fallback on retry ≥ 2 |
| 4 | **Validate** | `PropertyDetail` → `ZestimateResult` | Cross-checks number/zip/state/street; range-checks zestimate |

The LLM (Mistral) is invoked only when Stage 2 returns multiple equally-plausible candidates — the happy path never calls it.

## Folder structure

```
zestimate_agent/
├── backend/                 Python / FastAPI service
│   ├── src/zestimate_agent/ package source
│   ├── tests/               pytest suite (150 unit tests)
│   ├── evals/               accuracy eval harness (20 seed addresses)
│   ├── Dockerfile
│   ├── pyproject.toml
│   └── .env.example
│
├── frontend/                React / Vite UI
│   ├── src/
│   │   ├── components/      SearchForm, StreamProgress, ResultCard, …
│   │   ├── api.ts           fetch + SSE client
│   │   └── App.tsx          state machine (idle → streaming → done)
│   ├── nginx.conf           SPA + /api proxy config
│   ├── Dockerfile
│   └── package.json
│
├── docker-compose.yml       builds + runs both services
└── .gitignore
```

## Quick start (Docker)

```bash
# 1. Copy and fill in required keys
cp backend/.env.example backend/.env
# edit backend/.env → MISTRAL_API_KEY, RAPIDAPI_KEY

# 2. Build and run
docker compose up --build
```

| Service | URL |
|---------|-----|
| UI | http://localhost:8080 |
| API | http://localhost:8000 |
| API docs | http://localhost:8000/docs |

## Local development

See [backend/README.md](backend/README.md) and [frontend/README.md](frontend/README.md) for per-service setup.

```bash
# Terminal 1 — backend
cd backend
uv sync --extra dev
uv run uvicorn zestimate_agent.api:app --reload

# Terminal 2 — frontend
cd frontend
npm install
npm run dev          # http://localhost:5173 (Vite proxies /api → :8000)
```

## Production-ready features

Everything below is already implemented and running — not planned work.

### Reliability

| Feature | Where | What it does |
|---------|-------|-------------|
| **Retry with provider rotation** | `agent.py` | Up to 3 attempts; rotates browser impersonation on attempt 1, switches to RapidAPI on attempt 2 so a single blocked request never surfaces as a user error |
| **Circuit breaker** | `circuit_breaker.py` | Opens after 5 consecutive provider failures; fast-fails for 60 s before retrying — prevents request pile-up when Zillow is down |
| **Exponential backoff** | `providers/direct.py`, `providers/rapidapi.py` | Tenacity retries each HTTP call up to 3× with 1–8 s back-off before surfacing a `ProviderError` |
| **Request timeout** | `api.py` | Every `/lookup` is wrapped in `asyncio.timeout()`; returns HTTP 504 after a configurable deadline (default 30 s) instead of hanging indefinitely |
| **TTL cache with failure caching** | `cache.py` | Successful results cached for 1 h; failed lookups cached for 6 h to avoid hammering providers on known-bad addresses |

### Observability

| Feature | Where | What it does |
|---------|-------|-------------|
| **Structured JSON logging** | `observability.py` | `structlog` emits machine-readable JSON in production, human-readable TTY output in dev — every request carries `address`, `zpid`, `elapsed_ms`, `cache_hit` |
| **Prometheus metrics** | `api.py` | `/metrics` endpoint via `prometheus-fastapi-instrumentator`; `zestimate_lookups_total` counter labelled by `cache_hit` and `confidence` |
| **Correlation IDs** | `middleware.py` | Every request gets a `X-Request-ID` header injected and bound to the log context — makes tracing a specific request trivial in any log aggregator |
| **LangSmith tracing** | `observability.py` | Optional — set `LANGSMITH_TRACING=true` to trace every LangGraph run with full node-level input/output in LangSmith |
| **Health endpoints** | `api.py` | `/health` (liveness — always 200 while the process is up) and `/health/ready` (readiness — verifies config loaded and SQLite DB is reachable) |

### Security

| Feature | Where | What it does |
|---------|-------|-------------|
| **Per-IP rate limiting** | `auth.py` | `slowapi` enforces configurable limits (default 10 req/min for lookups, 5 req/min for cache ops) — returns HTTP 429 with a descriptive message |
| **Non-root container user** | `backend/Dockerfile` | API process runs as a dedicated `app` system user, not root — limits blast radius of any container escape |
| **Secrets never in code** | `.env.example`, `.gitignore` | All keys (`MISTRAL_API_KEY`, `RAPIDAPI_KEY`, `PROXY_URL`) loaded from `.env` via pydantic-settings; the file is gitignored and never committed |
| **CORS allowlist** | `api.py` | Origins locked to `CORS_ORIGINS` env var; defaults to same-origin only — no wildcard `*` in production |
| **Proxy URL redacted in logs** | `config.py` | `PROXY_URL` declared as `pydantic.SecretStr`; credentials in the URL never appear in `repr(settings)` or log output |

### Scalability & DevOps

| Feature | Where | What it does |
|---------|-------|-------------|
| **Multi-stage Docker builds** | `backend/Dockerfile`, `frontend/Dockerfile` | Builder stage installs deps; runtime stage contains only the final artifact — backend image has no build tools, frontend image is nginx + static files only |
| **Docker Compose orchestration** | `docker-compose.yml` | Single `docker compose up --build` starts both services; `ui` waits for `api` healthcheck before starting; named volume persists the SQLite cache across restarts |
| **Configurable worker count** | `docker-compose.yml` | `API_WORKERS` env var passed to uvicorn — scale up without rebuilding the image |
| **SSE-safe nginx proxy** | `frontend/nginx.conf` | `proxy_buffering off` + `proxy_read_timeout 300s` + `proxy_http_version 1.1` ensures streaming events reach the browser immediately without nginx buffering them |
| **SPA routing** | `frontend/nginx.conf` | `try_files $uri /index.html` fallback handles client-side routes so hard-refreshing a deep URL doesn't 404 |

### Correctness & Testing

| Feature | Where | What it does |
|---------|-------|-------------|
| **150 unit tests** | `backend/tests/` | Covers normalize, resolve, fetch, validate, cache, circuit breaker, API routes, CLI, and eval harness — all pass without network access |
| **Address cross-validation** | `validate.py` | Stage 4 checks street number, zip code, state, and street name (fuzzy ≥ 85) against the fetched property before returning — rejects wrong-property matches |
| **Unit-aware disambiguation** | `resolve.py` | Fuzzy match includes the unit field (`APT 5`, `#2B`) in the comparison so multi-unit buildings resolve to the correct unit |
| **Accuracy eval harness** | `evals/run_eval.py` | 20 seed addresses with known Zestimates; `zestimate eval --fail-under 0.99` exits non-zero if accuracy drops below threshold — can be wired into CI |
| **Pydantic models everywhere** | `models.py` | Every stage boundary is a validated Pydantic model; malformed provider responses fail loudly at the model layer, not silently downstream |

## Key design decisions

**DirectProvider is primary, RapidAPI is fallback.** DirectProvider reads the same `__NEXT_DATA__` blob the browser parses, so `property.zestimate` is byte-for-byte the value Zillow displays. RapidAPI wrappers cache data and can lag. DirectProvider is used on attempts 0–1; RapidAPI kicks in on attempt 2+ when DirectProvider is blocked.

**LLM only for disambiguation.** The four pipeline stages are deterministic. Mistral is invoked only when Stage 2 finds multiple HIGH-confidence address matches — roughly 1–2% of lookups.

**1-hour cache TTL.** Zestimates change infrequently but the spec requires "current" values. Cached results are re-fetched after 1 hour. Failures are cached for 6 hours to avoid hammering providers on bad addresses.
