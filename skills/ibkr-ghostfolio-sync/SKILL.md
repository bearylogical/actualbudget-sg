---
name: "ibkr-ghostfolio-sync"
description: "Sync Interactive Brokers trades and positions into Ghostfolio on demand via the IBKR connector and the budget-app MCP server, with preview and approval before import. Also use to check that Ghostfolio matches IBKR or to debug why the sync fails."
---

# IBKR → Ghostfolio sync (on demand)

Use when the user asks to update/sync IBKR into Ghostfolio, to check that Ghostfolio matches IBKR, or when the sync is failing.

## Requirements
- Interactive Brokers connector (tools `get_account_trades`, `get_account_positions`).
- budget-app MCP server (tools `ibkr_to_ghostfolio_preview`, `ibkr_to_ghostfolio_import`, `ghostfolio_clear_cash`, `ghostfolio_list_activities`, `ghostfolio_update_activity`, `ghostfolio_delete_activity`). If the tools aren't available, see **Troubleshooting** below; don't try to write to Ghostfolio another way.

## Steps
1. Read IBKR (read-only):
   - `get_account_positions`
   - `get_account_trades` with `YEAR_TO_DATE`; on a first sync also pull `LAST_QUARTER`, `TWO_QUARTERS_AGO`, `THREE_QUARTERS_AGO`, `FOUR_QUARTERS_AGO` and merge the `trades` arrays (dedupe by `trade_id`).
   - Pass the trades through unchanged; FX (`sec_type: CASH`) rows are skipped by the server, so there's no need to filter them.
2. Call `ibkr_to_ghostfolio_preview` with the raw JSON (`{"trades": [...]}` and `{"positions": [...]}` as strings). It writes nothing.
3. Show the user a short summary:
   - new activities (symbol, BUY/SELL, quantity, date), how many were already imported, FX rows skipped
   - `unmapped` symbols: ask for the Yahoo ticker (e.g. `XYZ.DE`) and tell them it goes in `/data/symbol_map.json`
   - `ghostfolio_check` errors, if any
   - position differences after import, and `suggested_opening_lots` (older holdings at IBKR average cost)
   - `account.cash_balance_to_clear`: the import sets the IBKR account's cash in Ghostfolio to 0
4. Ask for explicit approval, separately for (a) the new activities and (b) the opening lots. Opening lots are a one-time setup; don't suggest them again once positions match.
5. On approval, call `ibkr_to_ghostfolio_import` with the approved activities (JSON string, unchanged from the preview) and the positions JSON. The server links every activity to the IBKR account, drops anything already in Ghostfolio, and zeroes the account's cash. Report `imported`, `already_imported`, `cash_cleared` and any remaining `position_diff_after`, explaining each difference with its hint (older trades, transfer-in, corporate action, or a duplicate in Ghostfolio).

## Stocks only, no cash
Ghostfolio tracks securities only; cash (bank and IBKR) lives in Actual, so cash in Ghostfolio would be counted twice in net worth. The import zeroes the IBKR account's balance; for other accounts (e.g. bank accounts created in Ghostfolio) call `ghostfolio_clear_cash` first without `apply` to list balances, then with `apply=true` after the user agrees. The Money summary counts Ghostfolio securities only, even if some cash remains.

## How "already imported" works
- Activities synced from IBKR carry `ibkr:<trade id>` in the comment and are always recognised.
- Activities the user typed into Ghostfolio by hand have no tag. They match on symbol + quantity + BUY/SELL **within ±1 day**, because Ghostfolio stores them at local (SGT) midnight, which is the previous day in UTC. If a hand-entered trade used a rounded quantity, it won't match. Point out any new activity that looks like a near-duplicate of one already in Ghostfolio before importing.
- The account's cash balance in Ghostfolio (USD/EUR "holding") is ignored when comparing positions.
- Hand-entered activities must sit in the IBKR account to be matched (the check runs per account).

## Fixing existing activities
- `ghostfolio_list_activities` (filter by `account_id` / `symbol`) shows ids, fees and comments. Use it to check that hand-entered trades carry the IBKR commission: compare each activity's `fee` with the matching IBKR trade's `commission`.
- `ghostfolio_update_activity(activity_id, changes_json)` edits fee, quantity, unitPrice, date, type, currency, comment or accountId. Call it first without `apply` to show before/after, then with `apply=true` after the user's yes. The symbol can't be edited; delete and re-import instead.
- `ghostfolio_delete_activity(activity_id)` is for real duplicates only (e.g. a hand-entered trade that the sync also imported). Dry run first, then `apply=true` after the user explicitly approves that specific delete.

## Troubleshooting
Work down this list; stop at the first thing that's wrong.
1. **Preview/import tools missing**: the budget-app MCP server isn't registered or crashed on start.
   - Claude Code: `claude mcp add -s user budget-app -e BUDGET_APP_URL=http://127.0.0.1:8000 -- uv run --with "mcp>=1.26,<2" --with httpx python /Users/syamil/Projects/budget-app/mcp/server.py`, then restart the session.
   - Claude Desktop: same command in `claude_desktop_config.json` (see `docs/MCP.md` in the budget-app repo).
   - `mcp` **must be pinned below 2**. mcp 2.x removed `FastMCP` and the server exits with `No module named 'mcp.server.fastmcp'`.
2. **`health` says Ghostfolio is ok but the preview fails**: `/health` only checks login and the summary endpoints, not the ones the sync uses. Run the preview to find out.
3. **Preview error `Ghostfolio unreachable: 404 … /api/v1/order`**: the backend predates the Ghostfolio `/order` → `/activities` rename. Rebuild it: `docker compose up -d --build backend`.
4. **Every IBKR holding shows `ghostfolio: 0` although Ghostfolio has them**: the backend predates the change that reads holding symbols from `assetProfile`. Rebuild the backend as in 3.
5. **Lots of "new" activities that are already in Ghostfolio**: check the dates and quantities of the Ghostfolio entries against IBKR (see "How already imported works"). Never import until that's explained.
6. **Import/clear cash fails with `property isExcluded should not exist`**: the backend predates the fix for newer Ghostfolio's account API. Rebuild the backend as in 3, then run `ghostfolio_clear_cash` (dry run, then `apply=true`).
7. **`ghostfolio_list_activities` / update / delete tools missing**: the MCP server predates them. Restart the MCP server (Claude Desktop: restart the app) after pulling, and rebuild the backend as in 3.

## Rules
- Never import, clear cash, edit or delete without the user's yes to that specific change. Always dry-run edits and deletes first. Never delete in bulk.
- Re-running is safe: activities carry `ibkr:<trade id>` and are skipped when already present.
- Cash dividends aren't in the trade feed; mention that if the user asks about dividends. Dividend reinvestments (DRIP) come through as small BUYs.
- This is record-keeping, not investment advice.
