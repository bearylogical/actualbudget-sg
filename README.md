# Budget Parser → Actual Budget

Parse UOB credit card statements and import directly into your [Actual Budget](https://actualbudget.org) instance.

## Quick Start

```bash
docker compose up --build
```

Open **http://localhost:3000**

---

## Architecture

```
browser → frontend (Nginx :3000)
              ↓ /api/*
         backend (FastAPI :8000)   ← parses XLS, categorises, exports CSV
              ↓ http://actual-bridge:3001
         actual-bridge (Node :3001) ← @actual-app/api ↔ Actual Server
```

Three containers, one `docker compose up`.

---

## Usage

### 1. Parse your statement
- Drag & drop your UOB `.xls` or `.xlsx` credit card export
- Transactions are auto-categorised into 18 categories using keyword rules
- Review, search, filter, and manually override any category by clicking it

### 2. Import to Actual Budget
Click **⬆ Import to Actual** in the nav bar, then:

1. **Connect** — enter your Actual server URL (e.g. `http://192.168.1.x:5006`) and password
2. **Select Budget** — pick which budget file to import into
3. **Select Account** — choose the credit card account in Actual
4. **Map Categories** *(optional)* — match your parsed categories to Actual's categories
5. **Import** — transactions land in Actual and sync immediately

### 3. Export CSV
Use **⬇ Export CSV** on the Transactions tab for a clean spreadsheet import to any other tool.

---

## Configuration

All config is via environment variables in `docker-compose.yml`.

| Variable | Default | Description |
|---|---|---|
| `ACTUAL_BRIDGE_URL` | `http://actual-bridge:3001` | Internal bridge URL (don't change unless networking) |
| `PORT` (bridge) | `3001` | Bridge listen port |

The bridge caches budget data in a Docker volume (`actual-data`), so subsequent loads of the same budget are fast.

---

## Actual Server Setup

You need a running [Actual Budget server](https://actualbudget.org/docs/install/). The easiest way:

```yaml
# Add to your existing docker-compose or run separately
services:
  actual-server:
    image: actualbudget/actual-server:latest
    ports:
      - "5006:5006"
    volumes:
      - actual-server-data:/data
volumes:
  actual-server-data:
```

Then in the Budget Parser UI, use `http://actual-server:5006` if on the same Docker network, or `http://<your-host-ip>:5006` otherwise.

---

## Adding Categorisation Rules

Edit `backend/categorizer.py` — add a tuple to `RULES`:

```python
(r"my merchant name|other name", "My Category", 0.90),
```

- Pattern: Python regex, case-insensitive
- Category: any string (or one of the 18 existing ones)
- Confidence: `0.0–1.0` (shown as badge colour in UI)

Rebuild the backend container after changes: `docker compose up --build backend`

---

## Supported Statement Formats

| Bank | Format | Status |
|---|---|---|
| UOB Credit Card | `.xls` / `.xlsx` | ✅ Supported |

PRs welcome for DBS, OCBC, Maybank, etc.
