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


def _flat(h: dict) -> dict:
    """Newer Ghostfolio nests symbol/name under assetProfile (older: SymbolProfile or top level)."""
    prof = h.get("assetProfile") or h.get("SymbolProfile") or {}
    return {**h, "symbol": h.get("symbol") or prof.get("symbol"), "name": h.get("name") or prof.get("name")}


def is_cash(h: dict) -> bool:
    """An account's cash balance shows up as a holding (e.g. "USD") — not a security."""
    prof = h.get("assetProfile") or h.get("SymbolProfile") or {}
    return "CASH" in (h.get("assetSubClass"), prof.get("assetSubClass")) or \
        "LIQUIDITY" in (h.get("assetClass"), prof.get("assetClass"))


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
    rows = [_flat(h) for h in rows if not is_cash(h)]   # stocks/ETFs only — cash lives in Actual
    top = sorted(({"name": h.get("name") or h.get("symbol"), "symbol": h.get("symbol"),
                   "value": _num(h.get("valueInBaseCurrency"), h.get("value")),
                   "allocation": _num(h.get("allocationInPercentage")),
                   "performance": _num(h.get("netPerformancePercentWithCurrencyEffect"),
                                       h.get("netPerformancePercent"))}
                  for h in rows), key=lambda h: -(h["value"] or 0))[:8]
    value = _num(accounts.get("totalValueInBaseCurrency"), p.get("currentValueInBaseCurrency"),
                 p.get("currentNetWorth"))
    cash = _num(accounts.get("totalBalanceInBaseCurrency"))
    if value is not None and cash:
        value -= cash                    # securities only: account cash is already counted in Actual
    return {
        "configured": True,
        "currency": (user.get("settings") or {}).get("baseCurrency") or "SGD",
        "value": value,
        "cash": cash,                    # cash balances held in Ghostfolio accounts (excluded from value)
        "invested": _num(p.get("totalInvestment"), p.get("totalInvestmentValueWithCurrencyEffect")),
        "performance_ytd": _num(p.get("netPerformancePercentageWithCurrencyEffect"), p.get("netPerformancePercentage")),
        "performance_ytd_value": _num(p.get("netPerformanceWithCurrencyEffect"), p.get("netPerformance")),
        "accounts": [{"name": a.get("name"), "value": v, "platform": (a.get("platform") or {}).get("name")}
                     for a in accounts.get("accounts", []) if (v := _securities_value(a))],
        "holdings": top,
    }


def _securities_value(a: dict) -> float | None:
    v = _num(a.get("valueInBaseCurrency"), a.get("value"))
    return None if v is None else round(v - (_num(a.get("balanceInBaseCurrency"), a.get("balance")) or 0), 2)


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
    params = {"accounts": account_id} if account_id else None
    with _client(transport) as c:
        try:
            d = _get(c, "v1/activities", params)
        except httpx.HTTPStatusError as e:   # Ghostfolio before the /order → /activities rename
            if e.response.status_code != 404:
                raise
            d = _get(c, "v1/order", params)
    return d.get("activities", d if isinstance(d, list) else [])


def holdings(account_id: str | None = None, transport=None) -> list[dict]:
    with _client(transport) as c:
        d = _get(c, "v1/portfolio/holdings", {"accounts": account_id} if account_id else None)
    rows = d.get("holdings", [])
    return [_flat(h) for h in (rows.values() if isinstance(rows, dict) else rows)]


def import_activities(activities: list[dict], dry_run: bool = True, transport=None) -> dict:
    with _client(transport) as c:
        token = _auth(c)
        r = c.post(f"{_base()}/api/v1/import", params={"dryRun": str(dry_run).lower()},
                   json={"activities": activities}, headers={"Authorization": f"Bearer {token}"})
        if r.status_code >= 400:
            raise GhostfolioError(f"Ghostfolio import failed ({r.status_code}): {r.text[:300]}")
        return r.json()


def set_cash_balance(account: dict, balance: float = 0, transport=None) -> None:
    """PUT the account back unchanged except for its cash balance (Ghostfolio needs the full account).
    isExcluded is left out: newer Ghostfolio rejects it ("property isExcluded should not exist") and
    leaves fields that aren't sent unchanged."""
    body = {"id": account["id"], "name": account.get("name"), "currency": account.get("currency"),
            "balance": balance, "comment": account.get("comment"),
            "platformId": account.get("platformId") or (account.get("platform") or {}).get("id")}
    with _client(transport) as c:
        token = _auth(c)
        r = c.put(f"{_base()}/api/v1/account/{account['id']}", json=body, headers={"Authorization": f"Bearer {token}"})
        if r.status_code >= 400:
            raise GhostfolioError(f"Ghostfolio account update failed ({r.status_code}): {r.text[:300]}")


def clear_cash(account_ids: list[str] | None = None, dry_run: bool = True, transport=None) -> list[dict]:
    """Zero the cash balance of the given accounts (default: all), so Ghostfolio tracks securities only.
    Activities and holdings are untouched. Returns [{id, name, currency, balance}] for accounts that had cash."""
    changed = []
    for a in accounts(transport):
        if account_ids and a["id"] not in account_ids:
            continue
        if abs(float(a.get("balance") or 0)) < 1e-9:
            continue
        if not dry_run:
            set_cash_balance(a, 0, transport)
        changed.append({"id": a["id"], "name": a.get("name"), "currency": a.get("currency"),
                        "balance": float(a.get("balance") or 0)})
    return changed


# ── edit / delete single activities ───────────────────────────────────────────

def _send(client: httpx.Client, method: str, paths: list[str], body: dict | None = None) -> httpx.Response:
    """Authenticated write; tries each path in turn on 404 (Ghostfolio renamed /order → /activities)."""
    r = None
    for path in paths:
        def go():
            return client.request(method, f"{_base()}/api/{path}", json=body,
                                  headers={"Authorization": f"Bearer {_auth(client)}"})
        r = go()
        if r.status_code == 401:
            _jwt["token"] = None
            r = go()
        if r.status_code != 404:
            return r
    return r


def activity_view(o: dict) -> dict:
    """Flat, readable view of a Ghostfolio activity."""
    prof = o.get("SymbolProfile") or o.get("assetProfile") or {}
    return {"id": o.get("id"), "date": str(o.get("date", ""))[:10], "type": o.get("type"),
            "symbol": prof.get("symbol") or o.get("symbol"), "dataSource": prof.get("dataSource") or o.get("dataSource"),
            "quantity": o.get("quantity"), "unitPrice": o.get("unitPrice"), "fee": o.get("fee"),
            "currency": o.get("currency") or prof.get("currency"), "comment": o.get("comment"),
            "accountId": o.get("accountId") or (o.get("account") or o.get("Account") or {}).get("id")}


def find_activity(activity_id: str, transport=None) -> dict:
    o = next((x for x in orders(None, transport) if x.get("id") == activity_id), None)
    if not o:
        raise GhostfolioError(f"Ghostfolio has no activity {activity_id}")
    return o


EDITABLE = {"date", "type", "quantity", "unitPrice", "fee", "currency", "comment", "accountId"}


def update_activity(activity_id: str, changes: dict, dry_run: bool = True, transport=None) -> dict:
    """Change fields of one activity (EDITABLE only). Ghostfolio's PUT needs the full activity, so the
    current one is read first and the changes are merged in. Returns {before, after}."""
    bad = set(changes) - EDITABLE
    if bad:
        raise GhostfolioError(f"can't edit {sorted(bad)} — editable fields: {sorted(EDITABLE)} "
                              "(to change the symbol, delete the activity and import it again)")
    if not changes:
        raise GhostfolioError("no changes given")
    o = find_activity(activity_id, transport)
    before = activity_view(o)
    after = {**before, **changes}
    if "date" in changes:
        d = str(changes["date"])
        iso = d if "T" in d else f"{d}T00:00:00.000Z"
        after["date"] = d[:10]
    else:
        iso = str(o.get("date"))   # keep the stored timestamp exactly
    if dry_run:
        return {"dry_run": True, "before": before, "after": after}
    body = {"id": activity_id, "accountId": after["accountId"], "comment": after["comment"],
            "currency": after["currency"], "dataSource": after["dataSource"],
            "date": iso,
            "fee": float(after["fee"] or 0), "quantity": float(after["quantity"]), "symbol": after["symbol"],
            "type": after["type"], "unitPrice": float(after["unitPrice"])}
    tags = [t["id"] for t in (o.get("tags") or []) if isinstance(t, dict) and t.get("id")]
    if tags:
        body["tags"] = tags
    with _client(transport) as c:
        r = _send(c, "PUT", [f"v1/activities/{activity_id}", f"v1/order/{activity_id}"], body)
    if r.status_code >= 400:
        raise GhostfolioError(f"Ghostfolio activity update failed ({r.status_code}): {r.text[:300]}")
    return {"dry_run": False, "before": before, "after": activity_view(r.json()) if r.content else after}


def delete_activity(activity_id: str, dry_run: bool = True, transport=None) -> dict:
    """Delete one activity. Returns the activity that was (or, on a dry run, would be) deleted."""
    before = activity_view(find_activity(activity_id, transport))
    if dry_run:
        return {"dry_run": True, "deleted": before}
    with _client(transport) as c:
        r = _send(c, "DELETE", [f"v1/activities/{activity_id}", f"v1/order/{activity_id}"])
    if r.status_code >= 400:
        raise GhostfolioError(f"Ghostfolio activity delete failed ({r.status_code}): {r.text[:300]}")
    return {"dry_run": False, "deleted": before}
