# Budget Parser → Actual Budget

Parse Singapore bank statements (UOB card, POSB/DBS, OCBC) and import them into your [Actual Budget](https://actualbudget.org) instance — with clean payees, rule-aware categorisation, an optional LLM fallback, and an audit/sync tool for your Actual rules.

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
         backend (FastAPI :8000)   ← parses XLS/PDF, categorises, exports CSV
              ↓ http://actual-bridge:3001
         actual-bridge (Node :3001) ← @actual-app/api ↔ Actual Server
```

Three containers, one `docker compose up`.

---

## Usage

### 1. Parse your statement
- Drag & drop a UOB card export (`.xls`, even when it's named `.csv`) or a POSB/DBS `.csv`
- Every row gets a clean payee (`GRAB*A-9RNP… SINGAPORE SG` → `Grab`) and a category from, in order:
  1. **your Actual rules** (incl. the ones Actual learns when you re-categorise) — badge *Actual rule*
  2. **seed merchant rules** for Singapore (`backend/merchants.py`) — badge *Seed rule*
  3. **optional LLM** for anything left — badge *AI guess*, tagged `#llm` in Actual notes
- Own-account transfers (card bill payments, Wise/brokerage top-ups) are never categorised;
  PayNow/FAST to people are tagged `#review` instead of guessed
- Click any category to change it — with *Learn rules* ticked, your edits become payee rules in Actual

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
| `DATA_DIR` | `/data` | Category aliases + LLM cache (shared volume) |
| `ACCOUNT_ROUTES` | — | Scheduler: bank/file → Actual account name or id |
| `LLM_*` | off | See *LLM fallback* |
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

## Categorisation

Prefer teaching **Actual** (re-categorise a payee there, or tick *Learn rules* here) — those
rules win over everything else and also apply to the scheduler.

To add a seed merchant, edit `backend/merchants.py` (first match wins, always use `\b`):

```python
M(r"\bmy merchant\b|\bother name\b", "Clean Payee", "Dining & Hawker", 0.9),
```

Seed categories come from `backend/taxonomy.py`. If your Actual categories are named
differently, map them once with **Map unmapped** in the import bar (saved as aliases).

Run the tests after changes: `cd backend && pip install pytest && pytest -q tests`

### LLM fallback (optional, off by default)

Set in `docker-compose.yml` (`x-llm-env`):

| Variable | Example |
|---|---|
| `LLM_PROVIDER` | `ollama` · `openai` (any OpenAI-compatible server) · `anthropic` |
| `LLM_MODEL` | `qwen2.5:7b` · `gpt-4o-mini` · `claude-haiku-4-5` |
| `LLM_BASE_URL` | `http://ollama:11434/v1` (Ollama / LM Studio / vLLM / LiteLLM) |
| `LLM_API_KEY` | only for hosted providers |

Only payee names and statement descriptions are sent — no account numbers or balances.
The model must choose from your Actual categories; answers are cached per payee in
`/data/llm_cache.json`, so each merchant is asked once.

## Which account? (recommender)

Every upload is identified from its header — bank, **credit card vs savings/current**, last 4
digits, statement balance — e.g. `UOB Credit card •2583 (UOB ONE CARD)`,
`UOB Savings / current •4421 (One Account)`, `DBS/POSB Savings / current •1343 (personal)`.
Your open Actual accounts are then ranked by:

1. **remembered** — you imported this card/account number into that account before
2. **overlap** — some of these transactions already exist there
3. **balance** — account balance equals the statement balance
4. **name** — bank, product (“One Card”, “One Account”), POSB nickname, last 4, card-vs-savings words

A confident pick is selected for you (with the reasons shown); otherwise you get one-click
suggestions, and a warning if you pick an account that doesn't fit. After a successful
import the choice is remembered in `/data/account_map.json` — the scheduler uses the same
memory, so import each account once in the UI and scheduled imports route themselves.

Scheduler order: remembered → `ACCOUNT_ROUTES` → confident recommendation →
`ACTUAL_ACCOUNT_ID`; if none apply the file goes to `error/` rather than a wrong account.
Route keys match words from the statement, e.g.
`{"2583": "UOB One Card", "uob deposit": "UOB One Account", "posb": "POSB Savings"}`.

## Duplicate protection

| Situation | What happens |
|---|---|
| Same file imported twice / overlapping exports | matched on the bank reference (UOB `Ref No`, `PIB…`/`MBK…`, POSB refs) or a content hash → skipped |
| Two identical purchases the same day (2 × $1.80 kopi) | each gets its own id (`hash`, `hash-1`) → both kept, and re-imports pair up 1:1 |
| Card rows not yet posted (no posting date) | held back until they post (`SKIP_PENDING=true`), since FX amounts can change |
| Same transaction under a different id (PDF vs xls, old id scheme) | same date + amount already imported → **held for review**, never auto-added |
| Transaction you deleted in Actual | stays deleted on re-import (`REIMPORT_DELETED=false`) |
| Right file, wrong account | blocked: the UI shows ⛔ and the scheduler refuses when the transactions already live in another account |
| UI and scheduler importing at once | imports are serialised in the bridge |
| Hand-typed transactions in Actual | Actual links them to the imported row (same amount, ±7 days) instead of duplicating |

Your edits (category, notes, payee) are never overwritten by a re-import. Verified against
Actual's real import engine: re-import, overlapping export and "same rows under new ids"
all add 0 rows (`actual-bridge: npm test`, `backend: pytest`).

## Health checks

The dot in the top bar summarises all services — click it for details and **Test now** for the LLM.

| Endpoint | Use |
|---|---|
| `GET :8000/health/live` | backend process up (Docker healthcheck) |
| `GET :8000/health` | every service: backend, bridge, Actual server (+ version match), scheduler heartbeat, LLM |
| `GET :8000/health?llm=refresh` | re-test the LLM now (otherwise cached for `LLM_HEALTH_TTL`, default 600 s) |
| `GET :8000/health?strict=1` | HTTP 503 unless everything is `ok` — point Uptime Kuma here |
| `GET :3000/healthz` | nginx up |

Each container also has a Docker `healthcheck` (`docker compose ps` shows `healthy`), and
services start in dependency order. The scheduler writes `/data/scheduler-heartbeat.json`
every poll; it's reported `stale` if older than max(3 × poll interval, 5 min).

Statuses: `ok` · `degraded` (working with a problem, e.g. Actual server/API version mismatch,
last scheduler file failed) · `error` · `stale` · `disabled` · `unknown` (e.g. not connected yet).

## Rules audit & sync

With a budget loaded, click **🧹 Rules audit**. It reports broken, duplicate and conflicting
rules, raw payees to merge, disagreements with the seed table, and proposes rules for the
merchants in your data. Each fix is opt-in, previewed first, then applied.

CLI equivalent:

```bash
docker compose exec backend python rules_cli.py audit            # markdown report
docker compose exec backend python rules_cli.py sync             # dry run
docker compose exec backend python rules_cli.py sync --apply --aliases --rules --duplicates
```

Recommended first run after upgrading: **merge raw payees** (moves their transactions *and*
learned rules onto the clean payee), then **create rules**, then re-run the audit.

---

## Supported Statement Formats

| Bank | Format | Status |
|---|---|---|
| UOB credit card | `.xls` (often named `.csv`) / `.xlsx` | ✅ bank `Ref No` used for dedup |
| POSB / DBS account | digibank `.csv` | ✅ pending rows skipped, interest rows kept |
| OCBC | `.xls` / `.xlsx` / `.csv` | ⚠️ generic parser, untested |
| Wise | — | not yet (use Wise's statement API) |

## Scheduler

Drop files into the `watch-dir` volume. Route each bank to its own account:

```yaml
ACCOUNT_ROUTES: '{"uob": "UOB One Card", "posb": "POSB Savings"}'
```
