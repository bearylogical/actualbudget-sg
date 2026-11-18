"""Scan Actual for duplicates and unlinked transfers, queue them, and apply approved review items."""
from __future__ import annotations

import accounts as acct
import actual_gateway
import bridge_client
import review


def account_last4() -> dict[str, str]:
    """account_id → last 4 digits, from remembered statement fingerprints."""
    out = {}
    for fp, v in acct.load_map().items():
        last4 = fp.rsplit("|", 1)[-1]
        if last4 and last4 != "?":
            out[v["account_id"]] = last4
    return out


def apply_item(item: dict, gateway=None) -> dict:
    """Run a decided review item against Actual, mark it done, and supersede items it made moot."""
    gateway = gateway or actual_gateway.default()
    try:
        actions = bridge_client.actions_for(item)
        result = gateway.apply_actions(actions) if actions else {"ok": True, "results": [], "errors": []}
        review.mark_done(item["id"], result, ok=result["ok"])
        if result["ok"]:
            result["superseded"] = review.supersede(bridge_client.touched_ids(actions), except_id=item["id"])
    except Exception as e:
        result = {"ok": False, "errors": [str(e)]}
        review.mark_done(item["id"], result, ok=False)
    return {**review.get(item["id"]), "applied": result}


def scan_and_queue(start: str, end: str, account_ids: list[str] | None = None, gateway=None) -> dict:
    """Find duplicates (in-account and cross-account) and unlinked transfers in Actual and queue them.
    Items matching a learned pattern apply themselves."""
    gateway = gateway or actual_gateway.default()
    rows, _ = gateway.txns(start, end)
    scope = set(account_ids or [])
    dup_rows = [t for t in rows if t["account"] in scope] if scope else rows
    queued, auto = [], []
    dups = review.find_existing_duplicates(dup_rows)
    for d in review.find_cross_account_duplicates(rows):
        if not scope or {d["a"]["account"], d["b"]["account"]} & scope:
            dups.append(d)
    for d in dups:
        it = review.enqueue("existing_duplicate", {**d, "account": d["a"]["account"]}, [d["a"]["id"], d["b"]["id"]])
        (auto if it["status"] == "auto" else queued).append(it)
    for p in review.find_transfer_pairs(rows, account_last4()):
        if scope and not ({p["a"]["account"], p["b"]["account"]} & scope):
            continue
        it = review.enqueue("transfer_pair", p, [p["a"]["id"], p["b"]["id"]])
        (auto if it["status"] == "auto" else queued).append(it)
    applied = [apply_item(it, gateway) for it in auto if it.get("decision")]
    pending = [q for q in queued if q["status"] == "pending"]
    return {"queued": len(pending), "auto_applied": len(applied),
            "duplicates": sum(q["kind"] == "existing_duplicate" for q in pending),
            "transfers": sum(q["kind"] == "transfer_pair" for q in pending),
            "items": queued + applied}
