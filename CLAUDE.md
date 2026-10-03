# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

A 4-service personal finance app that parses Singapore bank statements (UOB, DBS, OCBC) and imports transactions into [Actual Budget](https://actualbudget.org/). All services run via Docker Compose.

## Development Commands

### Running the full stack
```bash
make up                     # version check → build → start (--wait) → load budget → health
make bridge                 # same, actual-bridge only (after upgrading Actual)
make check / make health    # compare Actual/api versions; show service health
docker compose up --build   # build and start all services, no checks
docker compose up           # start without rebuilding
docker compose down         # stop all services
```

### Frontend (SvelteKit) — local dev only
```bash
cd frontend
npm install
npm run dev      # dev server at localhost:5173 with proxy to backend:8000
npm run build    # static build output
```

### Backend (FastAPI) — local dev only
```bash
cd backend
pip install -r requirements.txt
uvicorn main:app --reload --port 8000
```

### Actual Bridge (Node.js) — local dev only
```bash
cd actual-bridge
npm install
node index.js   # starts on port 3001
```

## Architecture

```
Browser (localhost:3000)
    ↓
Frontend (Nginx :3000) — Static SvelteKit build
    ↓ /api/* proxied
Backend (FastAPI :8000) — Statement parsing & categorization
    ↓ HTTP
Actual-Bridge (Node :3001) — Thin Express wrapper around @actual-app/api
    ↓
Actual Budget Server (external :5006)
```

The **Scheduler** is a 4th service that watches a directory for `.xls/.xlsx` files and auto-imports them, bypassing the frontend entirely.

## Key Files & Responsibilities

- **`backend/parsers.py`** — Bank-specific statement parsers (UOB primary, DBS/OCBC partial). Each parser returns a normalized list of transaction dicts. Add new banks here.
- **`backend/categorizer.py`** — Regex-based auto-categorization with confidence scoring (0.0–1.0). ~70 rules across 18 categories tuned for Singapore merchants.
- **`backend/main.py`** — FastAPI routes: `/parse` (upload statement), `/export/csv`, and proxy routes to `/actual/*` (pass-through to actual-bridge).
- **`actual-bridge/index.js`** — Express server managing Actual Budget API state (credentials, loaded budget). Handles debit/credit logic and transaction splits on import.
- **`frontend/src/routes/+page.svelte`** — Monolithic main page: upload, transaction table, category editing, spending summary, Actual integration controls.
- **`frontend/src/components/ActualSidebar.svelte`** — Connect to Actual server, select budget/account.
- **`frontend/src/components/CategoryMapper.svelte`** — Map parsed categories to Actual Budget categories.
- **`scheduler/scheduler.py`** — File watcher: polls `/watch` every 30s, auto-parses and imports, moves files to `/watch/done` or `/watch/error`.

## Proxy Configuration

- **Production**: Nginx proxies `/api/*` → `backend:8000` (see `frontend/nginx.conf`)
- **Dev**: Vite proxies `/api/*` → `backend:8000` (see `frontend/vite.config.js`)
- Backend proxies `/actual/*` requests through to `actual-bridge:3001`

## Actual Bridge Notes

- `@actual-app/api` must match the running Actual Budget server version. The bridge build reads `$ACTUAL_SERVER_URL/info` (from `.env`) and installs the matching version, so `make bridge` (or `docker compose up --build`) after an Actual upgrade is enough; `ACTUAL_API_VERSION` in `.env` overrides it. `make check` (`scripts/actual-version.sh`) compares the two.
- The bridge maintains in-memory state (server URL, password, loaded budget). Restarting the bridge requires re-authenticating from the UI.
- Unhandled promise rejections are caught globally to prevent crashes from Actual API quirks.

## Statement Parsing

- `.xls` files are converted to `.xlsx` via LibreOffice CLI before parsing (hence LibreOffice in the backend Docker image).
- Parser selection is auto-detected by header row content.
- Parsed transactions include: `date`, `description`, `amount`, `type` (debit/credit), `category`, `confidence`.
