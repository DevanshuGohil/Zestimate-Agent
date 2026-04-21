# Zestimate Agent — Frontend

React + TypeScript + Tailwind CSS UI for the Zestimate Agent. Built with Vite.

## Features

- Address search with real-time streaming progress (Server-Sent Events)
- Per-step output detail (normalized address, matched zpid, raw zestimate value)
- Result card with confidence badge, provider, and cache status
- Search history panel (last 10 lookups, in-memory)
- Cache admin panel (clear all cached results)
- No-cache toggle for forcing a fresh fetch

## Development

Requires Node 20+.

```bash
cd frontend
npm install
npm run dev        # http://localhost:5173
```

The Vite dev server proxies `/api/*` → `http://localhost:8000` so the backend must be running:

```bash
# in another terminal
cd backend && uv run uvicorn zestimate_agent.api:app --reload
```

## Scripts

| Command | Description |
|---------|-------------|
| `npm run dev` | Start Vite dev server with HMR |
| `npm run build` | TypeScript compile + Vite production bundle → `dist/` |
| `npm run preview` | Preview the production build locally |

## Docker

```bash
# From the repo root (runs alongside the backend)
docker compose up --build

# Frontend only
docker build -t zestimate-ui ./frontend
docker run -p 8080:80 zestimate-ui
```

The production image is `nginx:1.27-alpine` serving the static bundle. Nginx proxies `/api/` to the `api` service (Docker Compose service name) with SSE-safe settings (`proxy_buffering off`, `proxy_read_timeout 300s`).

## Source layout

```
frontend/src/
├── App.tsx              Root component — streaming state machine
├── api.ts               fetch + SSE client (lookupStream async generator)
├── types.ts             TypeScript types (ZestimateResponse, StreamEvent, …)
├── index.css            Tailwind + shimmer keyframe animation
├── main.tsx             React entry point
└── components/
    ├── SearchForm.tsx   Address input, no-cache toggle, submit
    ├── StreamProgress.tsx  Live step list with per-node output detail
    ├── ResultCard.tsx   Final result display (zestimate, confidence, zpid)
    ├── ErrorCard.tsx    Error display with optional clarification candidates
    ├── Header.tsx       App header
    ├── HistoryPanel.tsx Last 10 successful lookups (in-memory)
    └── AdminPanel.tsx   Cache clear button
```

## Streaming state machine

`App.tsx` manages a state machine with four phases:

```
idle
  │  submit address
  ▼
streaming  ──── step events arrive ──── steps[] grows in real time
  │
  ├─ type:"result"  → success  (shows ResultCard + final steps)
  └─ type:"error"   → error    (shows ErrorCard + partial steps)
```

Each step event transitions the matching entry in `steps[]` from `running` → `done` or `error`. An `AbortController` cancels the in-flight stream when a new search starts before the previous one finishes.

## API proxy

In development, Vite rewrites `/api/foo` → `http://localhost:8000/foo`:

```ts
// vite.config.ts
proxy: {
  "/api": {
    target: "http://localhost:8000",
    rewrite: (path) => path.replace(/^\/api/, ""),
  },
}
```

In production (Docker), nginx does the same rewrite:

```nginx
location /api/ {
  proxy_pass http://api:8000/;   # trailing slash strips /api prefix
  proxy_buffering off;           # required for SSE
}
```
