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
"""
from __future__ import annotations

import re

from actual_rules import ActualContext
from pipeline import enrich

TAG_REVIEW = re.compile(r"\s*#review\b")


def _to_parsed(t: dict) -> dict:
    """Actual row → the shape enrich() expects from a statement parser."""
    text = t.get("imported_payee") or t.get("payee") or ""
    return {"description": text, "match_text": text, "amount": abs(t["amount"]) / 100,
            "is_credit": t["amount"] > 0, "date": t["date"]}


def candidates(rows: list[dict]) -> list[dict]:
    return [t for t in rows
            if t.get("amount") and not t.get("offbudget") and not t.get("transfer_id")
            and not t.get("payee_transfer_acct") and not t.get("is_parent") and not t.get("is_child")
            and not t.get("starting_balance_flag")]


def propose(rows: list[dict], ctx: ActualContext, *, mode: str = "uncategorised",
            use_llm: bool = True, llm=None) -> list[dict]:
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
                                       use_llm=use_llm, llm=llm)["transactions"])
        if cat:
            results += zip(cat, enrich([_to_parsed(t) for t in cat], ctx, account_id=account_id,
                                       use_llm=False, llm=llm)["transactions"])
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
