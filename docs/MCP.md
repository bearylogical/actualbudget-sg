# Talking to your money from Claude (MCP)

Three MCP servers, each doing one job:

| Server | What Claude can do | Writes? |
|---|---|---|
| **budget-app** (`mcp/server.py`, this repo) | Money summary & guidance, review queue (list / scan / ask AI / decide), reconcile an account against a bank balance, IBKR → Ghostfolio preview & import, health | only through `review_decide` / `ibkr_to_ghostfolio_import` — call them after you've said yes |
| **Actual** ([`actual-mcp`](https://github.com/s-stefanov/actual-mcp)) | Ad-hoc questions: spending by payee, trends, "what did I spend on Grab in August" | keep it **read-only** (no `--enable-write`) so every change goes through the review queue |
| **Ghostfolio** ([`ghostfolio-mcp`](https://github.com/mhajder/ghostfolio-mcp)) | Holdings, performance, dividends | read what you need; imports go through budget-app so they're de-duplicated |

IBKR itself comes from the **Interactive Brokers connector** in Claude — no server to run.

## Claude Desktop (Mac)

`~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "budget-app": {
      "command": "uv",
      "args": ["run", "--with", "mcp", "--with", "httpx", "python",
               "/Users/syamil/Projects/budget-app/mcp/server.py"],
      "env": { "BUDGET_APP_URL": "http://127.0.0.1:8000" }
    },
    "actual": {
      "command": "npx",
      "args": ["-y", "actual-mcp"],
      "env": {
        "ACTUAL_SERVER_URL": "https://budget.local.mangk.uk",
        "ACTUAL_PASSWORD": "…",
        "ACTUAL_BUDGET_SYNC_ID": "…",
        "ACTUAL_DATA_DIR": "/Users/syamil/.actual-mcp"
      }
    },
    "ghostfolio": {
      "command": "uvx",
      "args": ["ghostfolio-mcp"],
      "env": { "GHOSTFOLIO_URL": "https://ghostfolio.local.bearylogical.net", "GHOSTFOLIO_TOKEN": "…" }
    }
  }
}
```

* `BUDGET_APP_URL` must reach the backend. If the stack runs on the homelab rather than the
  Mac, either run `docker compose --profile mcp up -d mcp` there and point an HTTP MCP client
  at `http://<homelab>:8765/mcp` over Tailscale, or tunnel port 8000.
* `ACTUAL_BUDGET_SYNC_ID` is the budget's *Sync ID* (Actual → Settings → Advanced).
* Local MCP servers configured in Claude Desktop are also reachable from Cowork sessions
  linked to this Mac.

## Things to ask

* "How am I doing this month?" → `money_summary`
* "Check UOB One Account against the bank — it shows 21,800.15" → `reconcile_account`
* "Scan for duplicates and tell me what you'd do" → `review_scan`, `review_ask_ai`, `review_pending`
* "Link the card payments" → `review_decide(item, "link")` after you confirm
* "Sync my IBKR into Ghostfolio" → IBKR connector `get_account_trades` (YEAR_TO_DATE) +
  `get_account_positions` → `ibkr_to_ghostfolio_preview` → you approve →
  `ibkr_to_ghostfolio_import`

## IBKR → Ghostfolio details

* FX conversions are skipped; options/futures aren't supported by Ghostfolio.
* Tickers are mapped from the listing exchange in your positions (`VWRA @LSEETF` → `VWRA.L`,
  `VWCE @IBIS2` → `VWCE.DE`, `ASML @AEB` → `ASML.AS`, US listings unchanged). Anything it
  can't map is listed under `unmapped`; add it to `/data/symbol_map.json` once.
* Each activity carries `ibkr:<trade id>` in its comment — re-syncing never duplicates.
* The connector sees about four quarters of trades. Holdings bought earlier show up as
  position differences; the preview offers **opening lots** at IBKR's average cost so
  Ghostfolio's quantities match IBKR.
* Dividends in cash aren't in the connector's trade feed; dividend *reinvestments* (DRIP)
  are imported as buys.
