"""Scan Actual for duplicates and unlinked transfers, queue them, and apply approved review items.

Review policy lives here: which Actual actions a decided item means (actions_for), which other
items it makes moot (touched_ids), and whether Actual really did what was asked (an import that
added fewer rows than approved fails the item). The gateway only carries the actions out."""
from __future__ import annotations

import accounts as acct
import actual_gateway
import review


def account_last4() -> dict[str, str]:
    """account_id → last 4 digits, from remembered statement fingerprints."""
    out = {}
    for fp, v in acct.load_map().items():
        last4 = fp.rsplit("|", 1)[-1]
        if last4 and last4 != "?":
            out[v["account_id"]] = last4
    return out


def actions_for(item: dict) -> list[dict]:
    """Translate a decided review item into gateway actions (see HttpGateway.apply_actions)."""
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


def touched_ids(actions: list[dict]) -> set[str]:
    """Actual transaction ids these actions change — other items about them are now moot."""
    ids = set()
    for a in actions:
        ids.update(a.get("ids") or [])
        ids.update(x for x in (a.get("keep_id"), a.get("other_id")) if x)
    return ids


def _check_imports(actions: list[dict], result: dict) -> dict:
    """An approved import that Actual only partly took (e.g. skipped as a duplicate) is a failure."""
    for a, r in zip(actions, result.get("results") or []):
        if a.get("type") != "import":
            continue
        want, got = len(a["transactions"]), (r.get("added") or 0) + (r.get("updated") or 0)
        if got < want:
            msg = f"only {got} of {want} rows were imported"
            r.setdefault("errors", []).append(msg)
            result["errors"] = [*(result.get("errors") or []), msg]
            result["ok"] = False
    return result


def apply_item(item: dict, gateway=None) -> dict:
    """Run a decided review item against Actual, mark it done, and supersede items it made moot."""
    gateway = gateway or actual_gateway.default()
    try:
        actions = actions_for(item)
        result = _check_imports(actions, gateway.apply_actions(actions)) if actions \
            else {"ok": True, "results": [], "errors": []}
        review.mark_done(item["id"], result, ok=result["ok"])
        if result["ok"]:
            result["superseded"] = review.supersede(touched_ids(actions), except_id=item["id"])
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
