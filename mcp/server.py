"""
Budget-app MCP server — lets Claude (Desktop / Cowork) work with the review queue,
reconciliation, the Money summary and the IBKR → Ghostfolio sync.

It is a thin client over the budget-app backend's HTTP API, so every safety rule
of the app still applies: deletions and balance fixes only happen through
`review_decide`, which Claude should call only after you've said yes.

Run (stdio, for Claude Desktop):   BUDGET_APP_URL=http://127.0.0.1:8000 python server.py
Run (HTTP, e.g. on the homelab):   python server.py --http --port 8765   (put it behind Tailscale/auth)
"""
from __future__ import annotations

import argparse
import json
import os

import httpx
from mcp.server.fastmcp import FastMCP

API = os.getenv("BUDGET_APP_URL", "http://127.0.0.1:8000").rstrip("/")
mcp = FastMCP("budget-app")


def _req(method: str, path: str, body: dict | None = None, timeout: float = 300):
    r = httpx.request(method, f"{API}{path}", json=body, timeout=timeout, trust_env=False)
    try:
        data = r.json()
    except ValueError:
        data = {"error": r.text[:500]}
    if r.status_code >= 400:
        return {"error": data.get("detail") or data.get("error") or f"HTTP {r.status_code}"}
    return data


def _money(c):
    return None if c is None else round(c / 100, 2)


@mcp.tool()
def money_summary() -> dict:
    """Net worth, cash, card owed, investments (Ghostfolio), savings rate, this month's pace,
    safe-to-spend and the guidance list. Amounts in SGD."""
    d = _req("GET", "/finance/summary?months=7")
    if "error" in d:
        return d
    return {
        "as_of": d["as_of"], "net_worth": _money(d["net_worth"]), "cash": _money(d["cash"]),
        "card_owed": _money(d["card_owed"]), "investments": _money(d["investments"]),
        "savings_rate": d["savings_rate"], "emergency_months": d["emergency_months"],
        "this_month": {k: (_money(v) if k in ("spent", "usual_by_today", "projected", "safe_per_day", "budgeted") else v)
                       for k, v in d["month"].items()},
        "categories": [{"name": c["name"], "spent": _money(c["spent"]), "usual": _money(c["avg"]), "status": c["status"]}
                       for c in d["categories"]],
        "guidance": d["guidance"], "review_pending": d["review_pending"],
        "accounts": [{"id": a["id"], "name": a["name"], "balance": _money(a["balance"]), "offbudget": a["offbudget"]}
                     for a in d["accounts"]],
    }


@mcp.tool()
def list_accounts() -> dict:
    """Actual accounts with ids and balances in SGD (use the id for reconcile_account)."""
    d = _req("GET", "/actual/accounts")
    if "error" in d:
        return d
    return {"accounts": [{"id": a["id"], "name": a["name"], "balance": _money(a.get("balance")),
                          "offbudget": a.get("offbudget"), "closed": a.get("closed")} for a in d.get("accounts", [])]}


@mcp.tool()
def review_pending(kind: str = "") -> dict:
    """Items waiting for approval: possible duplicates, unlinked transfers, reconciliation fixes.
    kind: '' | import_duplicate | existing_duplicate | transfer_pair | reconcile_fix"""
    return _req("GET", f"/review?status=pending&kind={kind}")


@mcp.tool()
def review_scan(days: int = 120) -> dict:
    """Scan Actual for duplicates and unlinked transfers in the last N days and queue them."""
    return _req("POST", "/review/scan", {"days": days})


@mcp.tool()
def review_ask_ai() -> dict:
    """Have the configured LLM give a verdict + reason on each pending item (advisory only)."""
    return _req("POST", "/review/advise", {})


@mcp.tool()
def review_decide(item_id: str, decision: str) -> dict:
    """APPLY a decision to Actual. Only call after the user explicitly approved this item.
    Decisions: import_duplicate: skip|import · existing_duplicate: delete_a|delete_b|keep_both ·
    transfer_pair: link|keep_separate · reconcile_fix: apply|reject. The decision is remembered."""
    return _req("POST", f"/review/{item_id}/decide", {"decision": decision})


@mcp.tool()
def review_memory() -> dict:
    """Patterns learned from past decisions (and how many times each was confirmed)."""
    return _req("GET", "/review/memory")


@mcp.tool()
def reconcile_account(account_id: str, bank_balance: float, as_of: str = "", use_llm: bool = False) -> dict:
    """Compare an Actual account with the balance the bank shows (SGD; for a credit card pass what you
    owe as a positive number). Explains the gap (duplicates, unlinked transfers) and queues fixes for
    review. For a full row-by-row reconciliation upload the statement in the web app."""
    accts = _req("GET", "/actual/accounts").get("accounts", [])
    a = next((x for x in accts if x["id"] == account_id), None)
    if not a:
        return {"error": "unknown account id — call list_accounts"}
    is_card = any(w in a["name"].lower() for w in ("card", "credit", "visa", "master", "amex"))
    body = {"account_id": account_id, "balance": -abs(bank_balance) if is_card else bank_balance,
            "statement": {"bank": "", "kind": "credit_card" if is_card else "deposit",
                          "period_end": as_of or None}, "transactions": [], "use_llm": use_llm}
    d = _req("POST", "/reconcile", body)
    if "error" in d:
        return d
    r = d["report"]
    return {"account": a["name"], "as_of": r["as_of"], "bank": _money(r["expected_balance"]),
            "actual": _money(r["actual_balance"]), "gap": _money(r["gap"]), "reconciled": r["reconciled"],
            "duplicates": r["duplicates"], "unlinked_transfers": r["transfers"],
            "proposals": [{"title": p["title"], "why": p["why"], "effect": _money(p["effect"])} for p in r["recommendation"]],
            "queued_for_review": [q["id"] for q in d.get("queued", [])], "explanation": d.get("explanation")}


@mcp.tool()
def ibkr_to_ghostfolio_preview(trades_json: str, positions_json: str, account_id: str = "") -> dict:
    """Preview syncing IBKR into Ghostfolio. Pass the raw JSON from the IBKR connector:
    get_account_trades (use YEAR_TO_DATE or the quarter periods) and get_account_positions.
    Returns new activities, already-imported count, skipped FX rows, unmapped symbols,
    and IBKR-vs-Ghostfolio position differences (+ suggested opening lots). Writes nothing."""
    trades = json.loads(trades_json)
    positions = json.loads(positions_json)
    return _req("POST", "/investments/ibkr/preview", {
        "trades": trades.get("trades", trades) if isinstance(trades, dict) else trades,
        "positions": positions.get("positions", positions) if isinstance(positions, dict) else positions,
        "account_id": account_id or None})


@mcp.tool()
def ibkr_to_ghostfolio_import(activities_json: str, positions_json: str = "") -> dict:
    """Import previewed activities (the `new` list, optionally plus `suggested_opening_lots`) into
    Ghostfolio. Only call after the user approved the preview."""
    acts = json.loads(activities_json)
    positions = json.loads(positions_json) if positions_json else []
    return _req("POST", "/investments/ibkr/import", {
        "activities": acts,
        "positions": positions.get("positions", positions) if isinstance(positions, dict) else positions})


@mcp.tool()
def health() -> dict:
    """Status of backend, Actual bridge/server, scheduler, LLM and Ghostfolio."""
    d = _req("GET", "/health")
    return {"status": d.get("status"), **{k: v.get("status") for k, v in (d.get("components") or {}).items()}}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--http", action="store_true", help="serve streamable HTTP instead of stdio")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    a = ap.parse_args()
    if a.http:
        mcp.settings.host, mcp.settings.port = a.host, a.port
        mcp.run(transport="streamable-http")
    else:
        mcp.run()
