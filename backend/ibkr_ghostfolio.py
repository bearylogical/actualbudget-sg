"""
IBKR → Ghostfolio, on demand.

Claude reads your IBKR account through the IBKR connector (trades + positions) and
hands the raw JSON to /investments/ibkr/preview. This module:

  * turns stock/ETF trades into Ghostfolio activities (FX conversions are skipped —
    Ghostfolio tracks them as cash, not holdings)
  * maps IBKR symbols to Yahoo tickers using the listing exchange from your positions
    ("VWRA @LSEETF" → VWRA.L, "VWCE @IBIS2" → VWCE.DE, "ASML @AEB" → ASML.AS);
    overrides in DATA_DIR/symbol_map.json
  * tags every activity with the IBKR trade id so it's never imported twice
  * compares IBKR positions with Ghostfolio holdings afterwards (the investment
    equivalent of reconciling a bank balance)

Nothing is written until /investments/ibkr/import is called with the previewed
activities (Ghostfolio's own duplicate check runs again on import).
"""
from __future__ import annotations

import json
import os
import re
from datetime import date
from pathlib import Path

DATA_DIR = Path(os.getenv("DATA_DIR", "/data"))
SYMBOL_MAP_FILE = DATA_DIR / "symbol_map.json"

# IBKR listing exchange → Yahoo Finance suffix
EXCHANGE_SUFFIX = {
    "LSE": ".L", "LSEETF": ".L", "LSEIOB1": ".L",
    "AEB": ".AS", "IBIS": ".DE", "IBIS2": ".DE", "XETRA": ".DE", "FWB": ".F", "FWB2": ".F",
    "SBF": ".PA", "BVME": ".MI", "BM": ".MC", "EBS": ".SW", "VSE": ".VI",
    "SGX": ".SI", "SEHK": ".HK", "ASX": ".AX", "TSE": ".TO", "TSEJ": ".T",
    "NASDAQ": "", "NYSE": "", "ARCA": "", "BATS": "", "AMEX": "", "SMART": "",
}
US_CURRENCY_DEFAULT = {"USD": ""}


def load_symbol_map() -> dict[str, str]:
    try:
        return json.loads(SYMBOL_MAP_FILE.read_text())
    except Exception:
        return {}


def exchanges_from_positions(positions: list[dict]) -> dict[str, str]:
    """{"VWRA": "LSEETF", "GOOGL": ""} from 'VWRA @LSEETF' style descriptions."""
    out = {}
    for p in positions or []:
        desc = str(p.get("contract_description") or p.get("symbol") or "")
        m = re.match(r"^\s*([A-Z0-9.\-]+)(?:\s+@(\S+))?", desc)
        if m:
            out[m.group(1)] = m.group(2) or ""
    return out


def yahoo_symbol(symbol: str, currency: str, exchange: str | None, overrides: dict[str, str]) -> str | None:
    if symbol in overrides:
        return overrides[symbol]
    if exchange is not None:
        suffix = EXCHANGE_SUFFIX.get(exchange.upper())
        if suffix is not None:
            return symbol + suffix
    if exchange == "" and currency in US_CURRENCY_DEFAULT:
        return symbol
    return None      # unknown listing → ask the user once, then it's in symbol_map.json


def map_trades(trades: list[dict], positions: list[dict], account_id: str | None,
               overrides: dict[str, str] | None = None) -> dict:
    overrides = load_symbol_map() if overrides is None else overrides
    exch = exchanges_from_positions(positions)
    activities, skipped, unmapped = [], [], []
    for t in trades or []:
        sec = (t.get("sec_type") or "").upper()
        if sec == "CASH":
            skipped.append({"trade_id": t.get("trade_id"), "why": "FX conversion (cash, not a holding)"})
            continue
        if sec not in ("STK", "ETF", "FUND", ""):
            skipped.append({"trade_id": t.get("trade_id"), "why": f"{sec} not supported by Ghostfolio"})
            continue
        sym = str(t.get("symbol") or "").split(" ")[0]
        ysym = yahoo_symbol(sym, t.get("currency", ""), exch.get(sym), overrides)
        if not ysym:
            unmapped.append({"symbol": sym, "currency": t.get("currency"), "trade_id": t.get("trade_id"),
                             "hint": "add it to symbol_map.json, e.g. {\"%s\": \"%s.L\"}" % (sym, sym)})
            continue
        side = (t.get("side") or "").upper()
        typ = "BUY" if side in ("BUY", "BOT", "B") else "SELL" if side in ("SELL", "SLD", "S") else None
        if not typ:
            skipped.append({"trade_id": t.get("trade_id"), "why": f"unknown side {side!r}"})
            continue
        drip = "DRIP" in str(t.get("exchange") or "").upper()
        recurring = "RECINV" in str(t.get("exchange") or "").upper()
        act = {
            "type": typ, "symbol": ysym, "dataSource": "YAHOO", "currency": t.get("currency"),
            "quantity": abs(float(t.get("size") or 0)), "unitPrice": float(t.get("price") or 0),
            "fee": abs(float(t.get("commission") or 0)),
            "date": _iso(t.get("trade_time")),
            "comment": f"ibkr:{t.get('trade_id')}" + (" (dividend reinvestment)" if drip else "")
                       + (" (recurring investment)" if recurring else ""),
        }
        if account_id:
            act["accountId"] = account_id
        activities.append(act)
    return {"activities": activities, "skipped": skipped, "unmapped": unmapped}


def _iso(ts) -> str:
    s = str(ts or "")
    if re.match(r"^\d{4}-\d{2}-\d{2}T", s):
        return s.replace("Z", ".000Z") if s.endswith("Z") and "." not in s else s
    return f"{s[:10]}T00:00:00.000Z"


def already_imported(activities: list[dict], existing_orders: list[dict]) -> tuple[list, list]:
    """Split into (new, duplicate) by IBKR trade id in the comment, then by symbol+qty+type within a day
    (activities entered by hand in Ghostfolio are stored at local midnight, i.e. the previous UTC day)."""
    seen_ids = {m.group(1) for o in existing_orders
                for m in [re.search(r"ibkr:(\S+)", str(o.get("comment") or ""))] if m}
    days: dict[tuple, list[date]] = {}
    for o in existing_orders:
        sym = (o.get("SymbolProfile") or o.get("assetProfile") or {}).get("symbol") or o.get("symbol")
        key = (sym, round(float(o.get("quantity") or 0), 6), o.get("type"))
        days.setdefault(key, []).append(date.fromisoformat(str(o.get("date", ""))[:10]))
    new, dup = [], []
    for a in activities:
        tid = re.search(r"ibkr:(\S+)", a["comment"]).group(1)
        d = date.fromisoformat(a["date"][:10])
        near = any(abs((d - e).days) <= 1 for e in days.get((a["symbol"], round(a["quantity"], 6), a["type"]), []))
        (dup if tid in seen_ids or near else new).append(a)
    return new, dup


def compare_positions(positions: list[dict], holdings: list[dict], overrides: dict[str, str] | None = None) -> list[dict]:
    """IBKR quantity vs Ghostfolio quantity per holding → differences only."""
    overrides = load_symbol_map() if overrides is None else overrides
    exch = exchanges_from_positions(positions)
    gf = {}
    for h in holdings or []:
        sym = h.get("symbol")
        prof = h.get("assetProfile") or h.get("SymbolProfile") or {}
        if prof.get("assetSubClass") == "CASH" or prof.get("assetClass") == "LIQUIDITY":
            continue                     # the account's cash balance, not a security
        if sym:
            gf[sym] = gf.get(sym, 0.0) + float(h.get("quantity") or 0)
    diffs = []
    for p in positions or []:
        if (p.get("asset_class") or "STK").upper() not in ("STK", "ETF", "FUND"):
            continue
        sym = re.match(r"^\s*([A-Z0-9.\-]+)", str(p.get("contract_description") or "")).group(1)
        ysym = yahoo_symbol(sym, p.get("currency", ""), exch.get(sym), overrides) or sym
        ib_qty = float(p.get("position") or 0)
        g_qty = gf.pop(ysym, 0.0)
        if abs(ib_qty - g_qty) > 1e-6:
            diffs.append({"symbol": ysym, "ibkr": ib_qty, "ghostfolio": g_qty, "difference": round(ib_qty - g_qty, 6),
                          "hint": ("older trades than the synced window, a transfer-in, or a corporate action"
                                   if ib_qty > g_qty else
                                   "Ghostfolio has more than IBKR — an activity imported twice, or a sale/transfer-out missing")})
    for sym, q in gf.items():
        if abs(q) > 1e-6:
            diffs.append({"symbol": sym, "ibkr": 0.0, "ghostfolio": q, "difference": -q,
                          "hint": "in Ghostfolio but not in IBKR — sold elsewhere, or held at another broker"})
    return diffs


def opening_lots(positions: list[dict], diffs: list[dict], account_id: str | None, date: str,
                 overrides: dict[str, str] | None = None) -> list[dict]:
    """
    For positions older than the trade history the connector can see (~4 quarters):
    one BUY per missing quantity at IBKR's average cost, dated `date`, so Ghostfolio's
    holdings match IBKR. Returned as a suggestion only.
    """
    overrides = load_symbol_map() if overrides is None else overrides
    exch = exchanges_from_positions(positions)
    by_sym = {}
    for p in positions or []:
        sym = re.match(r"^\s*([A-Z0-9.\-]+)", str(p.get("contract_description") or "")).group(1)
        by_sym[yahoo_symbol(sym, p.get("currency", ""), exch.get(sym), overrides) or sym] = p
    lots = []
    for d in diffs:
        p = by_sym.get(d["symbol"])
        if not p or d["difference"] <= 0:
            continue
        lot = {"type": "BUY", "symbol": d["symbol"], "dataSource": "YAHOO", "currency": p.get("currency"),
               "quantity": d["difference"], "unitPrice": float(p.get("average_price") or 0), "fee": 0,
               "date": _iso(date), "comment": f"ibkr:opening-{d['symbol']}-{date} (opening position at IBKR average cost)"}
        if account_id:
            lot["accountId"] = account_id
        lots.append(lot)
    return lots
