"""
Read-only Ghostfolio client for the Money dashboard.

Env:
  GHOSTFOLIO_URL    e.g. http://ghostfolio:3333 or https://ghostfolio.local.bearylogical.net
  GHOSTFOLIO_TOKEN  the security token from Ghostfolio (Settings → Security token)

Auth: POST /api/v1/auth/anonymous {accessToken} → bearer JWT (cached ~1 day).
"""
from __future__ import annotations

import os
import time

import httpx

_jwt: dict = {"token": None, "exp": 0.0}


class GhostfolioError(RuntimeError):
    pass


def configured() -> bool:
    return bool(os.getenv("GHOSTFOLIO_URL") and os.getenv("GHOSTFOLIO_TOKEN"))


def _base() -> str:
    return os.getenv("GHOSTFOLIO_URL", "").rstrip("/")


def _auth(client: httpx.Client) -> str:
    if _jwt["token"] and _jwt["exp"] > time.time():
        return _jwt["token"]
    r = client.post(f"{_base()}/api/v1/auth/anonymous", json={"accessToken": os.getenv("GHOSTFOLIO_TOKEN", "")})
    if r.status_code in (401, 403):
        raise GhostfolioError("Ghostfolio rejected the security token (GHOSTFOLIO_TOKEN)")
    r.raise_for_status()
    _jwt.update(token=r.json()["authToken"], exp=time.time() + 86400)
    return _jwt["token"]


def _get(client: httpx.Client, path: str, params: dict | None = None) -> dict:
    token = _auth(client)
    r = client.get(f"{_base()}/api/{path}", params=params, headers={"Authorization": f"Bearer {token}"})
    if r.status_code == 401:            # token expired early → one retry
        _jwt["token"] = None
        r = client.get(f"{_base()}/api/{path}", params=params, headers={"Authorization": f"Bearer {_auth(client)}"})
    r.raise_for_status()
    return r.json()


def _num(*vals):
    for v in vals:
        if isinstance(v, (int, float)):
            return float(v)
    return None


def summary(transport: httpx.BaseTransport | None = None) -> dict:
    """{configured, value, cash, invested, performance_ytd, holdings[:8], accounts, currency}"""
    if not configured():
        return {"configured": False}
    try:
        with httpx.Client(timeout=30, transport=transport) as c:
            accounts = _get(c, "v1/account")
            try:
                perf = _get(c, "v2/portfolio/performance", {"range": "ytd"})
            except httpx.HTTPError:
                perf = {}
            try:
                holdings = _get(c, "v1/portfolio/holdings")
            except httpx.HTTPError:
                holdings = {}
            try:
                user = _get(c, "v1/user")
            except httpx.HTTPError:
                user = {}
    except (httpx.HTTPError, GhostfolioError, KeyError) as e:
        return {"configured": True, "error": str(e)[:300]}

    p = perf.get("performance", perf) or {}
    rows = holdings.get("holdings") or []
    if isinstance(rows, dict):
        rows = list(rows.values())
    top = sorted(({"name": h.get("name") or h.get("symbol"), "symbol": h.get("symbol"),
                   "value": _num(h.get("valueInBaseCurrency"), h.get("value")),
                   "allocation": _num(h.get("allocationInPercentage")),
                   "performance": _num(h.get("netPerformancePercentWithCurrencyEffect"),
                                       h.get("netPerformancePercent"))}
                  for h in rows), key=lambda h: -(h["value"] or 0))[:8]
    value = _num(accounts.get("totalValueInBaseCurrency"), p.get("currentValueInBaseCurrency"),
                 p.get("currentNetWorth"))
    return {
        "configured": True,
        "currency": (user.get("settings") or {}).get("baseCurrency") or "SGD",
        "value": value,
        "cash": _num(accounts.get("totalBalanceInBaseCurrency")),
        "invested": _num(p.get("totalInvestment"), p.get("totalInvestmentValueWithCurrencyEffect")),
        "performance_ytd": _num(p.get("netPerformancePercentageWithCurrencyEffect"), p.get("netPerformancePercentage")),
        "performance_ytd_value": _num(p.get("netPerformanceWithCurrencyEffect"), p.get("netPerformance")),
        "accounts": [{"name": a.get("name"), "value": _num(a.get("valueInBaseCurrency"), a.get("value")),
                      "platform": (a.get("platform") or {}).get("name")} for a in accounts.get("accounts", [])],
        "holdings": top,
    }


# ── write-side helpers (IBKR sync) ────────────────────────────────────────────

def _client(transport=None) -> httpx.Client:
    if not configured():
        raise GhostfolioError("Ghostfolio is not configured (GHOSTFOLIO_URL / GHOSTFOLIO_TOKEN)")
    return httpx.Client(timeout=60, transport=transport)


def accounts(transport=None) -> list[dict]:
    with _client(transport) as c:
        return _get(c, "v1/account").get("accounts", [])


def find_broker_account(name_hint: str = "ibkr", transport=None) -> dict | None:
    hints = {name_hint.lower(), "ibkr", "interactive brokers", "interactive"}
    for a in accounts(transport):
        text = f"{a.get('name', '')} {(a.get('platform') or {}).get('name', '')}".lower()
        if any(h in text for h in hints):
            return a
    return None


def orders(account_id: str | None = None, transport=None) -> list[dict]:
    with _client(transport) as c:
        d = _get(c, "v1/order", {"accounts": account_id} if account_id else None)
    return d.get("activities", d if isinstance(d, list) else [])


def holdings(account_id: str | None = None, transport=None) -> list[dict]:
    with _client(transport) as c:
        d = _get(c, "v1/portfolio/holdings", {"accounts": account_id} if account_id else None)
    rows = d.get("holdings", [])
    return list(rows.values()) if isinstance(rows, dict) else rows


def import_activities(activities: list[dict], dry_run: bool = True, transport=None) -> dict:
    with _client(transport) as c:
        token = _auth(c)
        r = c.post(f"{_base()}/api/v1/import", params={"dryRun": str(dry_run).lower()},
                   json={"activities": activities}, headers={"Authorization": f"Bearer {token}"})
        if r.status_code >= 400:
            raise GhostfolioError(f"Ghostfolio import failed ({r.status_code}): {r.text[:300]}")
        return r.json()
