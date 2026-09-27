"""Synchronous client for actual-bridge + executor for approved review items."""
from __future__ import annotations

import os

import httpx

BRIDGE_URL = os.getenv("ACTUAL_BRIDGE_URL", "http://actual-bridge:3001")


class BridgeError(RuntimeError):
    pass


def call(method: str, path: str, body: dict | None = None, timeout: float = 120) -> dict:
    try:
        r = httpx.request(method, f"{BRIDGE_URL}{path}", json=body, timeout=timeout)
    except httpx.HTTPError as e:
        raise BridgeError(f"actual-bridge unreachable: {e}") from e
    try:
        data = r.json()
    except ValueError:
        data = {"error": r.text[:300]}
    if not r.is_success:
        raise BridgeError(data.get("error") or f"HTTP {r.status_code}")
    return data


def txns(start: str, end: str, account_ids: list[str] | None = None) -> tuple[list[dict], list[dict]]:
    d = call("POST", "/txns/range", {"startDate": start, "endDate": end, "accountIds": account_ids})
    return d["transactions"], d["accounts"]


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
            r = call("POST", "/import", {"accountId": a["account"], "transactions": rows, "reimportDeleted": True,
                                         "verified": {r["imported_id"]: "import" for r in rows}})
            got = (r.get("added") or 0) + (r.get("updated") or 0)
            if got < len(rows):
                r.setdefault("errors", []).append(f"only {got} of {len(rows)} rows were imported")
            out.append(r)
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


def touched_ids(actions: list[dict]) -> set[str]:
    ids = set()
    for a in actions:
        ids.update(a.get("ids") or [])
        ids.update(x for x in (a.get("keep_id"), a.get("other_id")) if x)
    return ids


def actions_for(item: dict) -> list[dict]:
    """Translate an approved review item into bridge actions."""
    kind, d, p = item["kind"], item["decision"], item["payload"]
    if kind == "import_duplicate":
        return [{"type": "import", "account": p["account"], "transactions": [p["incoming"]]}] if d == "import" else []
    if kind == "existing_duplicate":
        if d in ("delete_a", "delete_b"):
            return [{"type": "delete", "ids": [p["a" if d == "delete_a" else "b"]["id"]]}]
        return []
    if kind == "transfer_pair":
        if d != "link":
            return []
        a, b = p["a"], p["b"]
        return [{"type": "link", "keep_id": a["id"], "other_id": b["id"], "keep_account": a["account"],
                 "other_account": b["account"], "date": a["date"]}]
    if kind == "reconcile_fix":
        return p["actions"] if d == "apply" else []
    if kind == "recategorize":
        from recategorize import update_for
        return [{"type": "categorize", "updates": [update_for(p)]}] if d == "apply" else []
    raise ValueError(kind)
