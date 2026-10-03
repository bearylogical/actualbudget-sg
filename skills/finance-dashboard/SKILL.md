---
name: "finance-dashboard"
description: "Pull all of the user's financial information (budget-app / Actual Budget money summary and accounts, Interactive Brokers portfolio, Ghostfolio, review queue, service health) into one private dashboard artifact. Use when the user asks for their money dashboard, a full financial overview, 'where do I stand', net worth plus portfolio in one place, or to refresh that dashboard. Read-only."
---

# Finance dashboard

Builds (or refreshes) one private HTML artifact, **Money Dashboard**, that shows everything in one place. It only reads; it never changes data.

## 1. Gather (read-only, in parallel)
Call everything that's available in one batch. If a source is missing or errors, keep going and mark that section "unavailable" with the reason. Never fill gaps with guessed numbers.

| Source | Calls | Gives |
|---|---|---|
| budget-app MCP | `weekly_snapshot` (includes the month summary, review queue and health). If it errors, fall back to `money_summary` + `health`. | net worth, cash, card owed, investments, savings rate, emergency months, month pace, safe-to-spend, categories vs usual, last 7 days, accounts, guidance, pending reviews, service status |
| Interactive Brokers connector | `get_account_summary`, `get_account_balances`, `get_account_positions`, `get_pa_performance_all_periods`, `get_pa_allocation` | portfolio value, cash by currency, positions with P&L, returns by period, allocation |
| Ghostfolio MCP (if connected) | holdings and performance (read tools only) | holdings across all accounts, performance |
| Actual MCP (if connected) | `net-worth`, `cash-flow`, `spending-by-category` for the last 6–12 months (read tools only) | trends for charts |

Never call write tools (`review_decide`, `ibkr_to_ghostfolio_import`, `create-*`, `update-*`, `delete-*`, `set-*`, order or alert tools).

If the budget-app tools aren't available at all, say so, build the dashboard from what is available, and point to the `ibkr-ghostfolio-sync` skill's Troubleshooting (MCP not registered, or `mcp` not pinned below 2).

## 2. Derive
- **Currency:** budget-app amounts are SGD. IBKR values come in their own currency (USD/EUR); label them with their currency. Only convert if a rate is in the data (e.g. IBKR base-currency fields). Don't invent FX rates.
- **IBKR vs Ghostfolio drift:** compare quantities per symbol (IBKR `VWRA @LSEETF` → `VWRA.L`, `VWCE @IBIS2` → `VWCE.DE`, `ASML @AEB` → `ASML.AS`, `RHM @IBIS` → `RHM.DE`, US tickers unchanged). Ignore Ghostfolio cash holdings. If anything differs, show it and suggest running the `ibkr-ghostfolio-sync` skill.
- **Freshness:** record the time each source was read; show a stale warning if budget-app's `as_of` is more than 2 days old or the scheduler/Actual server isn't `ok`.

## 3. Build the artifact
Load the `artifact-design` and `dataviz` skills before writing, and follow them. Embed the data as one JSON object in the page; no live calls from the page.

Sections, top to bottom:
1. **Header:** "Money Dashboard", as-of time, a status row of source chips (ok / stale / unavailable).
2. **KPI tiles:** net worth, cash, card owed, investments, savings rate, emergency months.
3. **This month:** spent vs usual-by-today vs projected, safe-to-spend per day, budget. Then categories vs usual (bars, over-pace highlighted).
4. **Last 7 days:** spent vs usual week, top payees, largest transactions, uncategorised count.
5. **Accounts:** table of balances, on-budget and off-budget, closed accounts hidden.
6. **Portfolio:** IBKR value and cash by currency; positions table (symbol, qty, price, value, unrealised P&L, % of portfolio); allocation chart; returns by period.
7. **Drift:** IBKR vs Ghostfolio differences, or "in sync".
8. **To do:** guidance items, pending review count by kind, unhealthy services.
9. **Trends** (only if the Actual MCP gave history): net worth and monthly cash flow over time.

Keep it scannable: one screen of KPIs, details below, works at phone width.

## 4. Publish
- Keep it **private**. This is personal financial data, so don't suggest sharing it.
- Refreshing: if a **Money Dashboard** artifact already exists (check `Artifact` `list`), read it and republish to the same URL so the link stays the same. Otherwise publish a new one with icon `chart`.
- Reply with the link and 3–5 lines: the headline numbers, anything stale or unavailable, and the top to-do items.

## Rules
- Read-only. To act on a to-do item (review, reconcile, sync), the user asks separately and the relevant flow handles approval.
- Numbers come only from tool results; mark anything missing as unavailable.
- Cash-flow guidance is fine. Portfolio figures are reporting, **not investment advice**; don't recommend buying or selling.
