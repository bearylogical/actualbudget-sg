"""
Category fixer — proposes categories for transactions ALREADY in Actual and queues
each proposal in the review queue (kind "recategorize"). Nothing changes in Actual
until you approve it; approvals teach payee → category patterns like the rest of
the queue (after REVIEW_LEARN_AFTER identical decisions new ones resolve themselves).

What gets proposed (same pipeline as imports: Actual rules → seed rules → LLM):
  * uncategorised rows on on-budget accounts                         (always)
  * categorised rows where one of YOUR Actual rules says otherwise   (always)
  * categorised rows where the seed rules / LLM disagree             (mode="all" only)
Skipped: transfers, split parents/children, starting balances, off-budget accounts,
and transfer / PayNow-to-person rows (those belong to the transfer finder or to you).

The lifecycle (each takes an Actual gateway, see actual_gateway.py):
  scan(gw, ...)       propose for a window of Actual rows and queue; learned patterns apply themselves
  refresh(gw, ...)    re-check what's waiting: supersede rows fixed in Actual, re-propose, promote learned
  apply(gw, ids, ...) your decision on queued items, written to Actual in ONE call (one sync)
Every write re-reads the rows first: a row whose category changed in Actual since it was queued
is superseded, never overwritten. A failed row fails only its own item.
"""
from __future__ import annotations

import re
from datetime import date, timedelta

import actual_gateway
import history
import money
import review
from actual_rules import ActualContext
from pipeline import enrich

KIND = "recategorize"
SCAN_DAYS = 120

TAG_REVIEW = re.compile(r"\s*#review\b")


def _to_parsed(t: dict) -> dict:
    """Actual row → the shape enrich() expects from a statement parser."""
    text = t.get("imported_payee") or t.get("payee") or ""
    amount, is_credit = money.statement_amount(t["amount"])
    return {"description": text, "match_text": text, "amount": amount, "is_credit": is_credit, "date": t["date"]}


def candidates(rows: list[dict]) -> list[dict]:
    return [t for t in rows
            if t.get("amount") and not t.get("offbudget") and not t.get("transfer_id")
            and not t.get("payee_transfer_acct") and not t.get("is_parent") and not t.get("is_child")
            and not t.get("starting_balance_flag")]


def propose(rows: list[dict], ctx: ActualContext, *, mode: str = "uncategorised",
            use_llm: bool = True, llm=None, history=None) -> list[dict]:
    """→ [{txn, current, proposed, reason}] for rows whose category should change."""
    cands = candidates(rows)
    by_acct: dict[str, list[dict]] = {}
    for t in cands:
        by_acct.setdefault(t["account"], []).append(t)

    out = []
    for account_id, txns in by_acct.items():
        # the LLM only ever sees uncategorised rows — never second-guess your choices with it
        unc = [t for t in txns if not t.get("category")]
        cat = [t for t in txns if t.get("category")]
        results = []
        if unc:
            results += zip(unc, enrich([_to_parsed(t) for t in unc], ctx, account_id=account_id,
                                       use_llm=use_llm, llm=llm, history=history)["transactions"])
        if cat:
            results += zip(cat, enrich([_to_parsed(t) for t in cat], ctx, account_id=account_id,
                                       use_llm=False, llm=llm, history=history)["transactions"])
        for t, e in results:
            new_id = e.get("category_id")
            if not new_id or new_id == t.get("category") or e.get("kind") in ("transfer", "p2p"):
                continue
            if t.get("category") and not (e["source"] == "actual" or mode == "all"):
                continue
            new = ctx.cat_by_id.get(new_id) or {}
            cur = ctx.cat_by_id.get(t.get("category")) or {}
            src = e["source"]
            why = {"actual": "one of your Actual rules matches",
                   "history": f"you've categorised this payee this way {e.get('history_times', 'several')}×",
                   "seed": "known merchant",
                   "llm": "AI guess from the payee"}.get(src, src)
            if t.get("category"):
                why += f" — currently {cur.get('name', 'unknown')}"
            out.append({
                "account": account_id,
                "txn": {k: t.get(k) for k in ("id", "account", "account_name", "date", "amount", "payee",
                                              "imported_payee", "category", "notes")},
                "current": {"id": t.get("category"), "name": cur.get("name")},
                "proposed": {"id": new_id, "name": new.get("name", e.get("category")),
                             "group": new.get("group_name"), "source": src,
                             "confidence": round(float(e.get("confidence") or 0), 2)},
                "reason": why,
            })
    return out


def update_for(payload: dict) -> dict:
    """The bridge update for an approved proposal: set the category, drop the #review tag."""
    upd = {"id": payload["txn"]["id"], "category": payload["proposed"]["id"]}
    notes = payload["txn"].get("notes") or ""
    if TAG_REVIEW.search(notes):
        upd["notes"] = TAG_REVIEW.sub("", notes).strip() or None
    return upd


# ── lifecycle ────────────────────────────────────────────────────────────────

def window(start: str | None = None, end: str | None = None, days: int = SCAN_DAYS) -> tuple[str, str]:
    """(start, end): end defaults to today, start to `days` before end."""
    end = end or date.today().isoformat()
    return start or (date.fromisoformat(end) - timedelta(days=int(days))).isoformat(), end


def _context(gw) -> tuple[ActualContext, object]:
    ctx = ActualContext.from_bridge(gw.context())
    return ctx, history.get(ctx, refresh=True, gateway=gw)


def _enqueue(p: dict) -> dict:
    return review.enqueue(KIND, p, [p["txn"]["id"], p["proposed"]["id"]])


def scan(gw=None, *, start: str | None = None, end: str | None = None, days: int = SCAN_DAYS,
         mode: str = "uncategorised", use_llm: bool = True, account_ids: list[str] | None = None,
         llm=None) -> dict:
    """Propose categories for rows already in Actual and queue them. Learned patterns apply themselves."""
    gw = gw or actual_gateway.default()
    start, end = window(start, end, days)
    ctx, hist = _context(gw)
    rows, _ = gw.txns(start, end, account_ids)
    proposals = propose(rows, ctx, mode=mode, use_llm=use_llm, llm=llm, history=hist)
    items = [_enqueue(p) for p in proposals]
    res = _write(gw, [it for it in items if it["status"] == "auto"], {r["id"]: r for r in rows})
    return {"scanned": len(candidates(rows)), "proposed": len(proposals),
            "queued": sum(it["status"] == "pending" for it in items), "auto_applied": len(res["applied"])}


def refresh(gw=None, *, use_llm: bool = True, llm=None) -> dict:
    """Re-check waiting category fixes against what you've taught since they were queued:
    drop ones you've fixed in Actual meanwhile, update ones whose best category changed
    (your rules, mappings or history), and apply ones matching a pattern you've now approved
    enough times. Nothing else is written to Actual."""
    gw = gw or actual_gateway.default()
    pending = review.list_items("pending", KIND, limit=5000)
    fixed_elsewhere = changed = 0
    by_id: dict[str, dict] = {}
    if pending:
        ctx, hist = _context(gw)
        by_id = _rows_for(gw, pending)
        still = []
        for it in pending:
            r = by_id.get(it["payload"]["txn"]["id"])
            if _moved(it, r):
                review.set_status(it["id"], "superseded")
                fixed_elsewhere += 1
            else:
                still.append((it, r))
        props = {p["txn"]["id"]: p for p in propose([r for _, r in still], ctx, use_llm=use_llm, llm=llm,
                                                     history=hist)}
        for it, r in still:
            p = props.get(r["id"])
            if not p:
                continue            # no better idea now; keep the suggestion you already have
            if p["proposed"]["id"] != it["payload"]["proposed"]["id"]:
                review.set_status(it["id"], "superseded")
                _enqueue(p)
                changed += 1
            elif p["proposed"] != it["payload"]["proposed"]:
                review.set_payload(it["id"], p)             # same category, fresher reason/confidence
    review.promote_learned(KIND)
    res = _write(gw, review.list_items("auto", KIND, limit=5000), by_id or None)
    return {"checked": len(pending), "fixed_elsewhere": fixed_elsewhere, "changed": changed,
            "auto_applied": len(res["applied"]), "pending": review.counts().get(KIND, {}).get("pending", 0)}


def apply(gw, item_ids: list[str], *, decision: str = "apply", category_id: str | None = None) -> dict:
    """Your decision on queued category fixes (each counts as yours, so payee → category is learned),
    optionally with a different category, written to Actual in one call. Ids that aren't waiting
    category fixes are skipped. Raises ValueError for an unknown category_id or decision.
    → {applied, failed, superseded: [item ids], items: [item + its result]}"""
    gw = gw or actual_gateway.default()
    items = [it for it in (review.get(i) for i in item_ids) if it and it["kind"] == KIND
             and it["status"] in ("pending", "failed")]
    if not items:
        return {"applied": [], "failed": [], "superseded": [], "items": []}
    if decision not in review.DECISIONS[KIND]:
        raise ValueError(f"{decision!r} is not valid for {KIND}: {sorted(review.DECISIONS[KIND])}")
    if category_id and decision == "apply":
        cat = ActualContext.from_bridge(gw.context()).cat_by_id.get(category_id)
        if not cat:
            raise ValueError("unknown category_id")
        for it in items:
            it["payload"]["proposed"] = {**it["payload"]["proposed"], "id": cat["id"], "name": cat["name"],
                                         "group": cat.get("group_name"), "source": "manual", "confidence": 1.0}
    by_id = _rows_for(gw, items) if decision == "apply" else {}
    decided, superseded, results = [], [], {}
    for it in items:
        if decision == "apply" and _moved(it, by_id.get(it["payload"]["txn"]["id"])):
            review.set_status(it["id"], "superseded")     # changed in Actual since queued: don't learn, don't write
            superseded.append(it["id"])
            results[it["id"]] = MOVED
            continue
        review.set_payload(it["id"], it["payload"])
        decided.append(review.decide(it["id"], decision, by="you"))
    res = _write(gw, decided, by_id)
    res["superseded"] += superseded
    res["results"].update(results)
    res["items"] = [{**review.get(it["id"]), "applied": res["results"].get(it["id"])} for it in items]
    del res["results"]
    return res


def accept_confident(gw=None, min_confidence: float = 0.8) -> dict:
    """Approve every waiting category fix whose proposal is at least min_confidence."""
    ids = [it["id"] for it in review.list_items("pending", KIND, limit=5000)
           if (it["payload"]["proposed"].get("confidence") or 0) >= min_confidence]
    return apply(gw, ids)


# ── the one write path ───────────────────────────────────────────────────────

MOVED = {"ok": False, "superseded": True, "errors": ["the row changed in Actual since this was queued"]}


def _moved(it: dict, row: dict | None) -> bool:
    """The row is gone, or its category isn't the one the proposal was made against."""
    return row is None or row.get("category") != (it["payload"].get("current") or {}).get("id")


def _rows_for(gw, items: list[dict]) -> dict[str, dict]:
    dates = sorted(it["payload"]["txn"]["date"] for it in items)
    return {r["id"]: r for r in gw.txns(dates[0], dates[-1])[0]} if dates else {}


def _write(gw, items: list[dict], by_id: dict[str, dict] | None = None) -> dict:
    """Write decided items to Actual in ONE gateway call and settle each item on its own outcome."""
    out = {"applied": [], "failed": [], "superseded": [], "results": {}}
    for it in items:
        if it.get("decision") == "keep":
            out["results"][it["id"]] = {"ok": True, "results": [], "errors": []}
            review.mark_done(it["id"], out["results"][it["id"]])
    todo = [it for it in items if it.get("decision") == "apply"]
    if not todo:
        return out
    if by_id is None:
        by_id = _rows_for(gw, todo)
    live = []
    for it in todo:
        if _moved(it, by_id.get(it["payload"]["txn"]["id"])):
            review.set_status(it["id"], "superseded")
            out["superseded"].append(it["id"])
            out["results"][it["id"]] = MOVED
        else:
            live.append(it)
    try:
        errors = gw.categorize([update_for(it["payload"]) for it in live]) if live else {}
    except actual_gateway.GatewayError as e:
        errors = {it["payload"]["txn"]["id"]: str(e) for it in live}
    done_txns = set()
    for it in live:
        tid = it["payload"]["txn"]["id"]
        err = errors.get(tid)
        result = {"ok": err is None, "errors": [err] if err else []}
        review.mark_done(it["id"], result, ok=err is None)
        out["results"][it["id"]] = result
        (out["failed"] if err else out["applied"]).append(it["id"])
        if not err:
            done_txns.add(tid)
    # other waiting fixes for a row just recategorised were proposed against its old category
    for other in review.list_items("pending", KIND, limit=5000):
        if other["payload"]["txn"]["id"] in done_txns:
            review.set_status(other["id"], "superseded")
            out["superseded"].append(other["id"])
    return out
