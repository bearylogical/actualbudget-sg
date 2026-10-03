"""
Budget-app MCP server — lets Claude (Desktop / Cowork) work with the review queue,
reconciliation, the Money summary, transaction history and the IBKR → Ghostfolio sync.

It is a thin client over the budget-app backend's HTTP API, so every safety rule
of the app still applies: deletions and balance fixes only happen through
`review_decide`, which Claude should call only after you've said yes.

Run (stdio, for Claude Desktop):   BUDGET_APP_URL=http://127.0.0.1:8000 python server.py
Run (HTTP, e.g. on the homelab):   python server.py --http --port 8765   (tailnet only, no auth)
Run (public, claude.ai connector): MCP_PUBLIC_URL=https://budget-mcp.example.com \
                                   MCP_OWNER_PASSWORD_HASH=... python server.py --http
    Public mode is always read-only and requires OAuth sign-in (see oauth.py).
"""
from __future__ import annotations

import argparse
import functools
import inspect
import json
import os
import time
from urllib.parse import urlencode

import httpx
from mcp.server.fastmcp import FastMCP

import mcp_audit

API = os.getenv("BUDGET_APP_URL", "http://127.0.0.1:8000").rstrip("/")
# MCP_READ_ONLY=true: only reading tools are exposed (use this for scheduled reports)
READ_ONLY = os.getenv("MCP_READ_ONLY", "false").lower() == "true"

# Public mode (claude.ai custom connector): OAuth required, write tools never registered.
PUBLIC_URL = os.getenv("MCP_PUBLIC_URL", "").rstrip("/")
if os.getenv("MCP_REQUIRE_PUBLIC", "false").lower() == "true" and not PUBLIC_URL.startswith("https://"):
    # the internet-facing container must never fall back to the private, write-enabled, no-auth mode
    raise SystemExit("MCP_REQUIRE_PUBLIC is set: MCP_PUBLIC_URL must be the connector's https:// URL.")
_kwargs: dict = {}
if PUBLIC_URL:
    from pathlib import Path

    from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions

    import oauth

    _pw = os.getenv("MCP_OWNER_PASSWORD_HASH", "")
    if not _pw.startswith("scrypt:"):
        raise SystemExit("MCP_PUBLIC_URL is set but MCP_OWNER_PASSWORD_HASH isn't — "
                         "make one with `python oauth.py hash`. Refusing to start a public server without sign-in.")
    READ_ONLY = True
    _provider = oauth.OwnerOAuthProvider(PUBLIC_URL, _pw, Path(os.getenv("DATA_DIR", "/data")),
                                         allow_loopback=os.getenv("MCP_ALLOW_LOOPBACK", "false").lower() == "true",
                                         single_grant=os.getenv("MCP_SINGLE_GRANT", "true").lower() != "false")
    _kwargs = dict(
        auth_server_provider=_provider,
        auth=AuthSettings(
            issuer_url=PUBLIC_URL, resource_server_url=f"{PUBLIC_URL}/mcp", validate_token_resource=True,
            required_scopes=[oauth.SCOPE],
            client_registration_options=ClientRegistrationOptions(enabled=True, valid_scopes=[oauth.SCOPE],
                                                                  default_scopes=[oauth.SCOPE]),
            revocation_options=RevocationOptions(enabled=True)),
    )

mcp = FastMCP("budget-app", **_kwargs)
if PUBLIC_URL:
    oauth.install_routes(mcp, _provider)


def _client_id() -> str | None:
    try:
        from mcp.server.auth.middleware.auth_context import get_access_token
        t = get_access_token()
        return t.client_id[:12] if t else None
    except Exception:
        return None


def _audited(f, write: bool):
    """Wrap a tool so every call lands in the audit trail (tool, summarised args, client, result)."""
    sig = inspect.signature(f)

    @functools.wraps(f)
    def wrapper(*a, **kw):
        t0, ok, err = time.monotonic(), True, None
        try:
            r = f(*a, **kw)
            if isinstance(r, dict) and r.get("error"):
                ok, err = False, str(r["error"])
            return r
        except Exception as e:
            ok, err = False, repr(e)
            raise
        finally:
            try:
                args = dict(sig.bind_partial(*a, **kw).arguments)
            except TypeError:
                args = {}
            mcp_audit.tool(f.__name__, args, write, ok, err, int((time.monotonic() - t0) * 1000), _client_id())
    return wrapper


def read_tool():
    """Register a read-only tool (audited). Returns the plain function, so internal calls aren't logged."""
    def deco(f):
        mcp.tool()(_audited(f, write=False))
        return f
    return deco


def write_tool():
    """Register a tool that changes data (audited) — skipped entirely in read-only mode."""
    def deco(f):
        if not READ_ONLY:
            mcp.tool()(_audited(f, write=True))
        return f
    return deco


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


@read_tool()
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


@read_tool()
def list_accounts() -> dict:
    """Actual accounts with ids and balances in SGD (use the id for reconcile_account)."""
    d = _req("GET", "/actual/accounts")
    if "error" in d:
        return d
    return {"accounts": [{"id": a["id"], "name": a["name"], "balance": _money(a.get("balance")),
                          "offbudget": a.get("offbudget"), "closed": a.get("closed")} for a in d.get("accounts", [])]}


@read_tool()
def review_pending(kind: str = "") -> dict:
    """Items waiting for approval: possible duplicates, unlinked transfers, reconciliation fixes,
    category fixes. kind: '' | import_duplicate | existing_duplicate | transfer_pair | reconcile_fix | recategorize"""
    return _req("GET", f"/review?status=pending&kind={kind}")


@write_tool()
def review_scan(days: int = 120) -> dict:
    """Scan Actual for duplicates and unlinked transfers in the last N days and queue them."""
    return _req("POST", "/review/scan", {"days": days})


@write_tool()
def category_scan(days: int = 120, mode: str = "uncategorised", use_llm: bool = True) -> dict:
    """Propose categories for transactions already in Actual (last N days) and queue them as
    'recategorize' review items. Changes nothing in Actual. mode='uncategorised' covers
    uncategorised rows + rows your own Actual rules disagree with; mode='all' also flags rows
    the merchant rules categorise differently. Then use review_pending(kind='recategorize')."""
    return _req("POST", "/review/recategorize/scan", {"days": days, "mode": mode, "use_llm": use_llm})


@read_tool()
def list_categories() -> dict:
    """Actual category groups and categories (ids for review_decide's category_id)."""
    return _req("GET", "/actual/categories")


@write_tool()
def review_ask_ai() -> dict:
    """Have the configured LLM give a verdict + reason on each pending item (advisory only)."""
    return _req("POST", "/review/advise", {})


@write_tool()
def review_decide(item_id: str, decision: str, category_id: str = "") -> dict:
    """APPLY a decision to Actual. Only call after the user explicitly approved this item.
    Decisions: import_duplicate: skip|import · existing_duplicate: delete_a|delete_b|keep_both ·
    transfer_pair: link|keep_separate · reconcile_fix: apply|reject · recategorize: apply|keep.
    For recategorize, category_id applies a different category than proposed (see list_categories).
    The decision is remembered."""
    body = {"decision": decision}
    if category_id:
        body["category_id"] = category_id
    return _req("POST", f"/review/{item_id}/decide", body)


@read_tool()
def review_memory() -> dict:
    """Patterns learned from past decisions (and how many times each was confirmed)."""
    return _req("GET", "/review/memory")


@write_tool()
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


@read_tool()
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


@write_tool()
def ibkr_to_ghostfolio_import(activities_json: str, positions_json: str = "", account_id: str = "") -> dict:
    """Import previewed activities (the `new` list, optionally plus `suggested_opening_lots`) into
    Ghostfolio. Only call after the user approved the preview. Pass the activities unchanged: each is
    linked to the IBKR account, ones already in Ghostfolio are skipped, and the account's cash balance
    is then set to 0 so Ghostfolio tracks stocks/ETFs only (cash is tracked in Actual).
    Returns imported / already_imported counts, cash_cleared and position_diff_after."""
    acts = json.loads(activities_json)
    positions = json.loads(positions_json) if positions_json else []
    return _req("POST", "/investments/ibkr/import", {
        "activities": acts.get("new", acts) if isinstance(acts, dict) else acts,
        "positions": positions.get("positions", positions) if isinstance(positions, dict) else positions,
        "account_id": account_id or None})


@write_tool()
def ghostfolio_clear_cash(apply: bool = False, account_ids: str = "") -> dict:
    """Zero the cash balance of Ghostfolio accounts so it tracks stocks/ETFs only (bank and broker cash
    is tracked in Actual). apply=false (default) only lists the accounts holding cash; call with
    apply=true after the user agreed. account_ids: comma-separated, default all accounts.
    Activities and holdings are never touched."""
    ids = [x.strip() for x in account_ids.split(",") if x.strip()]
    return _req("POST", "/investments/ghostfolio/clear-cash", {"account_ids": ids or None, "dry_run": not apply})


@read_tool()
def ghostfolio_list_activities(account_id: str = "", symbol: str = "") -> dict:
    """List Ghostfolio activities (id, date, type, symbol, quantity, unitPrice, fee, currency, comment,
    accountId), newest first. Filter by account_id and/or symbol (substring, e.g. "VWRA").
    Use it to find the id for ghostfolio_update_activity / ghostfolio_delete_activity."""
    q = urlencode({k: v for k, v in {"account_id": account_id, "symbol": symbol}.items() if v})
    return _req("GET", f"/investments/ghostfolio/activities{'?' + q if q else ''}")


@write_tool()
def ghostfolio_update_activity(activity_id: str, changes_json: str, apply: bool = False) -> dict:
    """Edit one Ghostfolio activity. changes_json: JSON object with any of fee, quantity, unitPrice,
    date (YYYY-MM-DD or ISO), type (BUY/SELL/...), currency, comment, accountId — e.g. {"fee": 1.70}.
    apply=false (default) returns {before, after} without writing; call with apply=true only after
    the user approved that exact change. To change the symbol, delete and re-import instead."""
    return _req("POST", f"/investments/ghostfolio/activities/{activity_id}/update",
                {"changes": json.loads(changes_json), "dry_run": not apply})


@write_tool()
def ghostfolio_delete_activity(activity_id: str, apply: bool = False) -> dict:
    """Delete one Ghostfolio activity. apply=false (default) shows the activity that would be deleted;
    call with apply=true only after the user explicitly approved deleting it. Not reversible —
    an IBKR-synced trade can be brought back by re-running the IBKR import."""
    return _req("POST", f"/investments/ghostfolio/activities/{activity_id}/delete", {"dry_run": not apply})


@read_tool()
def weekly_snapshot() -> dict:
    """Everything a weekly money report needs in one call: last 7 days of spending vs your usual
    week (by category, top payees, largest items), month-to-date pace and guidance, net worth /
    cash / card / investments, pending review items, and service health. Read-only."""
    week = _req("GET", "/finance/week?days=7")
    summary = money_summary()
    review = _req("GET", "/review/counts")
    h = health()
    if "error" not in week:
        week = {**week, "spent": _money(week["spent"]), "usual_week": _money(week["usual_week"]),
                "categories": [{**c, "spent": _money(c["spent"]), "usual_week": _money(c["usual_week"])} for c in week["categories"]],
                "top_payees": [{**p, "spent": _money(p["spent"])} for p in week["top_payees"]],
                "largest": [{**t, "amount": _money(t["amount"])} for t in week["largest"]],
                "uncategorised": {**week["uncategorised"], "amount": _money(week["uncategorised"]["amount"])}}
    return {"week": week, "month": summary, "review": review, "health": h,
            "note": "amounts in SGD; guidance is about cash flow, not investment advice"}


@read_tool()
def get_transactions(start: str = "", end: str = "", search: str = "", account: str = "",
                     category: str = "", kind: str = "all", limit: int = 100) -> dict:
    """Historical transactions from Actual, newest first. Read-only.
    start/end: YYYY-MM-DD (default: the last 30 days). search: matches payee or notes.
    account / category: id or part of the name. kind: all | spend | income | transfer.
    limit: rows returned (max 500); count, money_in and money_out cover every match.
    Amounts in SGD: negative = money out."""
    qs = urlencode({"start": start, "end": end, "q": search, "account": account,
                    "category": category, "kind": kind, "limit": limit})
    d = _req("GET", f"/finance/transactions?{qs}")
    if "error" in d:
        return d
    return {**d, "money_in": _money(d["money_in"]), "money_out": _money(d["money_out"]),
            "transactions": [{**t, "amount": _money(t["amount"])} for t in d["transactions"]]}


@read_tool()
def monthly_trends(months: int = 12) -> dict:
    """Month-by-month history for the last N months (max 24): income, spent, net, savings rate,
    spending by category, and Actual balances at each month end (on-budget, off-budget, total;
    investments not included). For charts of cash flow and net worth over time. Amounts in SGD."""
    d = _req("GET", f"/finance/trends?months={int(months)}")
    if "error" in d:
        return d
    return {"note": d["note"], "months": [{
        **x, **{k: _money(x[k]) for k in ("income", "spent", "net", "budgeted")},
        "categories": [{**c, "spent": _money(c["spent"])} for c in x["categories"]],
        "balance_end": {k: (v if k == "as_of" else _money(v)) for k, v in x["balance_end"].items()},
    } for x in d["months"]]}


@read_tool()
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
        from urllib.parse import urlparse

        from mcp.server.transport_security import TransportSecuritySettings

        mcp.settings.host, mcp.settings.port = a.host, a.port
        # FastMCP only sets Host/Origin checks for a localhost bind at construction time;
        # set them for the real bind. Public: only the public hostname (+ in-container probes).
        # Tailnet/private: MCP_ALLOWED_HOSTS (comma list, "host:*" for any port), else no check.
        extra = [h.strip() for h in os.getenv("MCP_ALLOWED_HOSTS", "").split(",") if h.strip()]
        if PUBLIC_URL:
            mcp.settings.transport_security = TransportSecuritySettings(
                enable_dns_rebinding_protection=True,
                allowed_hosts=[urlparse(PUBLIC_URL).netloc, "127.0.0.1:*", "localhost:*", *extra],
                allowed_origins=["https://claude.ai", "https://claude.com"])
            # Only Anthropic may reach the MCP/token endpoints (the in-app version of the WAF rule).
            cidrs = oauth.parse_cidrs(os.getenv("MCP_ALLOWED_CIDRS", ""))
            if cidrs:
                _app = mcp.streamable_http_app
                mcp.streamable_http_app = lambda: oauth.SourceIPGate(_app(), cidrs)
                print(f"Source-IP gate on: {', '.join(map(str, cidrs))} (+ {', '.join(oauth.OPEN_PATHS)} from anywhere)", flush=True)
        elif a.host not in ("127.0.0.1", "localhost", "::1"):
            mcp.settings.transport_security = TransportSecuritySettings(
                enable_dns_rebinding_protection=bool(extra), allowed_hosts=extra)
        mcp.run(transport="streamable-http")
    else:
        mcp.run()
