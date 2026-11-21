"""Synchronous HTTP transport to actual-bridge. Only actual_gateway.py should import this:
everything else reaches Actual through the gateway."""
from __future__ import annotations

import os
import threading

import httpx

import connection
import money

BRIDGE_URL = os.getenv("ACTUAL_BRIDGE_URL", "http://actual-bridge:3001")


NO_BUDGET = "No budget loaded"


class BridgeError(RuntimeError):
    """Actual couldn't be reached or refused the request.
    status: the bridge's HTTP status (None when it never answered); timeout: it didn't answer in time."""

    def __init__(self, message: str, status: int | None = None, timeout: bool = False):
        super().__init__(message)
        self.status, self.timeout = status, timeout

    @property
    def no_budget(self) -> bool:
        """No budget is loaded and the saved connection couldn't reload one."""
        return NO_BUDGET in str(self)
_reload_lock = threading.Lock()


def is_no_budget(status: int, error: str | None) -> bool:
    return status == 400 and error == NO_BUDGET


def reload_saved_budget() -> bool:
    """Reload the budget from the saved connection (connection.py). The bridge keeps the loaded
    budget in memory only, so after a bridge restart every read fails with "No budget loaded"
    until someone reconnects in the UI; this does it for them. False if nothing is saved."""
    body = connection.load_body()
    if not body:
        return False
    with _reload_lock:                      # concurrent callers share one reload
        try:
            if httpx.get(f"{BRIDGE_URL}/health", timeout=5).json().get("budgetLoaded"):
                return True
        except Exception:
            pass
        try:
            r = httpx.post(f"{BRIDGE_URL}/budgets/load", json=body, timeout=60)
        except httpx.HTTPError:
            return False
        if not r.is_success:
            print(f"[bridge] auto-reload of saved budget failed: HTTP {r.status_code} {r.text[:200]}")
        return r.is_success


def _request(method: str, path: str, body: dict | None, timeout: float) -> httpx.Response:
    try:
        return httpx.request(method, f"{BRIDGE_URL}{path}", json=body, timeout=timeout)
    except httpx.TimeoutException as e:
        raise BridgeError(f"actual-bridge did not answer within {timeout:g}s — it may be stuck reaching "
                          "your Actual server (DNS / TLS / firewall from inside Docker?)", timeout=True) from e
    except httpx.HTTPError as e:
        raise BridgeError(f"actual-bridge unreachable: {e}") from e


def _error(r: httpx.Response) -> str | None:
    try:
        return r.json().get("error")
    except (ValueError, AttributeError):
        return None


def call(method: str, path: str, body: dict | None = None, timeout: float = 120) -> dict:
    r = _request(method, path, body, timeout)
    if is_no_budget(r.status_code, _error(r)) and reload_saved_budget():
        r = _request(method, path, body, timeout)
    try:
        data = r.json()
    except ValueError:
        data = {"error": r.text[:300]}
    if not r.is_success:
        raise BridgeError(data.get("error") or f"HTTP {r.status_code}", status=r.status_code)
    return data


def txns(start: str, end: str, account_ids: list[str] | None = None) -> tuple[list[dict], list[dict]]:
    d = call("POST", "/txns/range", {"startDate": start, "endDate": end, "accountIds": account_ids})
    return d["transactions"], d["accounts"]


def import_rows(account_id: str, rows: list[dict], *, dry_run: bool = False,
                verified: dict[str, str] | None = None, reimport_deleted: bool | None = None) -> dict:
    """POST Statement rows to the bridge's /import as Actual rows (money.to_actual) and hand the
    answer back in Statement rows (money.restate_import)."""
    body = {"accountId": account_id, "rows": [money.to_actual(t) for t in rows], "dryRun": dry_run,
            "verified": verified or {}}
    if reimport_deleted is not None:
        body["reimportDeleted"] = reimport_deleted
    return money.restate_import(rows, call("POST", "/import", body, timeout=120))


def apply_actions(actions: list[dict]) -> dict:
    """
    Execute approved actions in order. Supported:
      {"type": "delete", "ids": [...]}
      {"type": "link",   "keep_id", "other_id", "keep_account", "other_account", "date"}
      {"type": "import", "account": id, "transactions": [parsed rows]}   (forced past the duplicate check)
      {"type": "adjust", "account": id, "amount_cents": int, "date": "YYYY-MM-DD", "note": str}
      {"type": "categorize", "updates": [{"id", "category", "notes"?}]}
    """
    out = []
    for a in actions:
        t = a.get("type")
        if t == "delete":
            out.append(call("POST", "/txns/delete", {"ids": a["ids"]}))
        elif t == "link":
            out.append(call("POST", "/transfers/link", {"pairs": [{k: a[k] for k in
                            ("keep_id", "other_id", "keep_account", "other_account", "date")}]}))
        elif t == "import":
            rows = a["transactions"]
            out.append(import_rows(a["account"], rows, reimport_deleted=True,
                                   verified={r["imported_id"]: "import" for r in rows}))
        elif t == "adjust":
            out.append(call("POST", "/txns/add", {"accountId": a["account"], "transactions": [{
                "date": a["date"], "amount": int(a["amount_cents"]),
                "payee_name": a.get("payee", "Reconciliation adjustment"),
                "notes": a.get("note", "#reconcile"), "cleared": True}]}))
        elif t == "categorize":
            out.append(call("POST", "/txns/update", {"updates": a["updates"]}))
        else:
            raise ValueError(f"unknown action type {t!r}")
    errors = [e for r in out for e in (r.get("errors") or [])]
    return {"ok": not errors, "results": out, "errors": errors}

