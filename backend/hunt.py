"""
AI hunt — an LLM agent that searches what is already in Actual for duplicates and
unreconciled transfers, beyond what the fixed rules in review.py catch.

The rule finders (review.find_*) are deliberately strict so the queue stays quiet.
The hunt widens the net and lets the model judge with context:

  * loose duplicate candidates  same amount in one account within a week, or the same
                                charge in two accounts, that the rules let through
                                (different wording, a typed-in copy days later …)
  * orphan transfer legs        rows that read like a transfer / card payment but have
                                no linked or equal-and-opposite partner — the other leg
                                is missing, or was imported with a different amount
  * search                      free-text / amount search over the window, so the model
                                can check whether a repeat is normal (subscriptions,
                                two coffees) before calling it a duplicate

Every conclusion goes through `propose_duplicate` / `propose_transfer`, which validate the
ids and amounts against the loaded rows and only put an item in the review queue (with the
model's reason as its AI verdict). Things that need a human but have no safe automatic fix
(a card payment whose amounts differ by S$0.36) come back as `findings`. Nothing in Actual
changes until you approve an item.
"""
from __future__ import annotations

import re
from datetime import date as _date

import review

MAX_ROWS = 60           # rows per tool reply — keeps each tool result well under the loop's 20k cap

HUNT_SYSTEM = """You are a meticulous bookkeeper auditing a household's Actual Budget (Singapore banks:
UOB, DBS/POSB, OCBC; credit cards and savings accounts). Your job: find transactions that
are recorded twice, and transfers between the user's own accounts that are not reconciled
(not linked, or one leg missing / a different amount).

How to work:
- Start with overview, then rule_candidates, loose_duplicates and orphan_transfers.
- Before calling a repeat a duplicate, check whether it is a normal pattern with search
  (same payee every month = subscription; two small same-day food/transport charges can
  both be real). Two different bank references (src "ref" on both) are two real transactions.
- Strong duplicate clues: one copy typed in by hand and one imported; the same charge
  exported twice (xls vs PDF: "ref" vs "hash" src, slightly different text, 0-2 days apart);
  the same bank text in two accounts.
- Strong transfer clues: card-payment / I-BANK / FAST / GIRO wording, the other account's
  number in the text, equal and opposite amounts within a few days.
- Call propose_duplicate / propose_transfer once per conclusion you are confident in, and
  flag for anything a person must look at (amount mismatch, a missing leg, unsure cases).
  Never invent ids — only use ids from tool output. Proposals are only queued for the
  user's approval; nothing is changed by you.
Finish with a short plain-English summary (at most 4 sentences, no markdown): what you checked,
what you proposed, what you flagged. Tool amounts are in cents; always write money in dollars,
e.g. S$25.90, never cents."""


def _days(a: str, b: str) -> int:
    return abs((_date.fromisoformat(a) - _date.fromisoformat(b)).days)


def _src(t: dict) -> str:
    return review._id_kind(t)


def _brief(t: dict) -> dict:
    """Compact row for the model."""
    out = {"id": t["id"], "account": t.get("account_name") or t["account"], "date": t["date"],
           "amount": t["amount"], "payee": t.get("payee"), "text": (t.get("imported_payee") or "")[:70],
           "src": _src(t)}
    if t.get("notes"):
        out["notes"] = str(t["notes"])[:50]
    if t.get("transfer_id"):
        out["linked_transfer"] = True
    return out


def _cap(rows: list, n: int = MAX_ROWS) -> dict:
    return {"rows": rows[:n], "total": len(rows), "truncated": len(rows) > n}


def _plain(t: dict) -> bool:
    """A normal row the hunt may reason about (not a split child, opening balance, or empty)."""
    return bool(t.get("amount")) and not t.get("is_child") and not t.get("starting_balance_flag")


def _unlinked(t: dict) -> bool:
    return _plain(t) and not t.get("transfer_id") and not t.get("payee_transfer_acct")


def loose_duplicate_pairs(rows: list[dict], max_days: int = 7, exclude: set[frozenset] | None = None) -> list[dict]:
    """Same amount, same account within max_days (or same account-independent charge in two
    accounts within 2 days with similar text) — minus distinct bank refs and pairs in `exclude`."""
    exclude = exclude or set()
    by_amount: dict[int, list[dict]] = {}
    for t in rows:
        if _unlinked(t):
            by_amount.setdefault(t["amount"], []).append(t)
    out = []
    for grp in by_amount.values():
        if len(grp) < 2:
            continue
        grp.sort(key=lambda t: t["date"])
        for i, a in enumerate(grp):
            for b in grp[i + 1:]:
                gap = _days(a["date"], b["date"])
                if gap > max_days:
                    break
                if frozenset((a["id"], b["id"])) in exclude:
                    continue
                if a.get("imported_id") and a.get("imported_id") == b.get("imported_id") and a["account"] == b["account"]:
                    continue
                if _src(a) == _src(b) == "ref" and a.get("imported_id") != b.get("imported_id"):
                    continue        # two bank references = two transactions
                cross = a["account"] != b["account"]
                if cross and (gap > 2 or not (review._similar_text(a, b) or
                                              (a.get("imported_id") and a.get("imported_id") == b.get("imported_id")))):
                    continue
                out.append({"a": _brief(a), "b": _brief(b), "days_apart": gap, "cross_account": cross,
                            "same_payee": review._norm(a.get("payee")) == review._norm(b.get("payee")),
                            "similar_text": review._similar_text(a, b)})
    # most suspicious first: same text, close together, typed-in vs imported
    out.sort(key=lambda p: (-(p["same_payee"] or p["similar_text"]), p["days_apart"],
                            -((p["a"]["src"] == "manual") != (p["b"]["src"] == "manual"))))
    return out


_ACCT_NO = re.compile(r"\d{8,}")


def orphan_transfer_legs(rows: list[dict], max_days: int = 5) -> list[dict]:
    """Rows that read like an own-account transfer / card payment but have no unlinked
    equal-and-opposite partner in another account. Each comes with the nearest look-alikes
    (opposite sign, another account, within max_days, amount within 2% or S$5)."""
    cands = [t for t in rows if _unlinked(t)]
    out = []
    for t in cands:
        text = f"{t.get('imported_payee') or ''} {t.get('payee') or ''}"
        if not (review.TRANSFER_TEXT.search(text) or _ACCT_NO.search(text)):
            continue
        exact = [o for o in cands if o["account"] != t["account"] and o["amount"] == -t["amount"]
                 and _days(o["date"], t["date"]) <= max_days]
        if exact:
            continue            # rule_candidates / a normal scan handles exact pairs
        near = [o for o in cands if o["account"] != t["account"] and (o["amount"] > 0) != (t["amount"] > 0)
                and _days(o["date"], t["date"]) <= max_days
                and abs(abs(o["amount"]) - abs(t["amount"])) <= max(500, abs(t["amount"]) * 0.02)]
        near.sort(key=lambda o: (abs(abs(o["amount"]) - abs(t["amount"])), _days(o["date"], t["date"])))
        out.append({"row": _brief(t), "near_matches": [
            {**_brief(o), "amount_diff": abs(o["amount"]) - abs(t["amount"])} for o in near[:3]]})
    out.sort(key=lambda x: -abs(x["row"]["amount"]))
    return out


def tools(rows: list[dict], accounts: list[dict], window: tuple[str, str],
          account_last4: dict[str, str] | None = None, source: str = "llm") -> tuple[list[dict], dict, dict]:
    """Tool specs + handlers over a fixed set of Actual rows. state collects what was queued/flagged."""
    by_id = {t["id"]: t for t in rows}
    state: dict = {"queued": [], "findings": []}
    rule_dups = review.find_existing_duplicates(rows) + review.find_cross_account_duplicates(rows)
    rule_xfers = review.find_transfer_pairs(rows, account_last4 or {})
    rule_pairs = {frozenset((p["a"]["id"], p["b"]["id"])) for p in rule_dups + rule_xfers}

    def _status(kind, a, b):
        st = review.statuses([review.item_id(kind, a, b)])
        return st[0]["status"] if st else None

    def overview():
        per: dict[str, dict] = {}
        for t in rows:
            if not _plain(t):
                continue
            a = per.setdefault(t["account"], {"account": t.get("account_name") or t["account"], "rows": 0,
                                              "money_in": 0, "money_out": 0, "unlinked_transfer_like": 0})
            a["rows"] += 1
            a["money_in" if t["amount"] > 0 else "money_out"] += t["amount"]
            if _unlinked(t) and review.TRANSFER_TEXT.search(f"{t.get('imported_payee') or ''} {t.get('payee') or ''}"):
                a["unlinked_transfer_like"] += 1
        return {"window": {"start": window[0], "end": window[1]}, "amounts_are": "cents, money in positive",
                "accounts": list(per.values()),
                "open_accounts": [{"id": a["id"], "name": a["name"], "offbudget": a.get("offbudget")}
                                  for a in accounts if not a.get("closed")],
                "rule_duplicate_candidates": len(rule_dups), "rule_transfer_candidates": len(rule_xfers)}

    def rule_candidates():
        def tag(kind, p):
            return {**p, "a": _brief(by_id.get(p["a"]["id"], p["a"])), "b": _brief(by_id.get(p["b"]["id"], p["b"])),
                    "review_status": _status(kind, p["a"]["id"], p["b"]["id"])}
        return {"duplicates": [tag("existing_duplicate", p) for p in rule_dups][:40],
                "transfers": [tag("transfer_pair", p) for p in rule_xfers][:40],
                "note": "review_status 'done'/'dismissed' = the user already decided; skip those"}

    def loose_duplicates(max_days: int = 7, account: str = ""):
        pairs = loose_duplicate_pairs(rows, max_days=min(int(max_days), 31), exclude=rule_pairs)
        if account:
            pairs = [p for p in pairs if account.lower() in (p["a"]["account"] + p["b"]["account"]).lower()]
        live = []
        for p in pairs:
            st = _status("existing_duplicate", p["a"]["id"], p["b"]["id"])
            if st in ("done", "dismissed", "approved"):
                continue
            live.append(p)
        return _cap(live, 40)

    def orphan_transfers(max_days: int = 5):
        return _cap(orphan_transfer_legs(rows, max_days=min(int(max_days), 14)), 30)

    def search(text: str = "", account: str = "", amount_cents: int | None = None, tolerance_cents: int = 0,
               start: str = "", end: str = ""):
        q = (text or "").lower()
        out = []
        for t in rows:
            if not _plain(t):
                continue
            if q and q not in f"{t.get('payee') or ''} {t.get('imported_payee') or ''} {t.get('notes') or ''}".lower():
                continue
            if account and account.lower() not in f"{t.get('account_name') or ''} {t['account']}".lower():
                continue
            if amount_cents is not None and abs(abs(t["amount"]) - abs(int(amount_cents))) > int(tolerance_cents or 0):
                continue
            if (start and t["date"] < start) or (end and t["date"] > end):
                continue
            out.append(_brief(t))
        out.sort(key=lambda r: r["date"])
        return _cap(out)

    def propose_duplicate(keep_id: str, delete_id: str, reason: str, confidence: float = 0.7):
        a, b = by_id.get(keep_id), by_id.get(delete_id)
        if not a or not b:
            return {"error": f"unknown id(s): {[i for i in (keep_id, delete_id) if i not in by_id]}"}
        if keep_id == delete_id:
            return {"error": "keep_id and delete_id are the same row"}
        if a["amount"] != b["amount"]:
            return {"error": "amounts differ — not a duplicate; use flag if it needs a look"}
        if not (_unlinked(a) and _unlinked(b)):
            return {"error": "one of these is a linked transfer or a split line; flag it instead"}
        if _src(a) == _src(b) == "ref" and a.get("imported_id") != b.get("imported_id"):
            return {"error": "two different bank references are two real transactions"}
        cross = a["account"] != b["account"]
        payload = {"a": review._slim(a), "b": review._slim(b), "account": a["account"],
                   "reason": str(reason)[:300], "confidence": "high" if confidence >= 0.85 else "medium",
                   "suggest": "delete_b", "source": source}
        if cross:
            payload["cross_account"] = True
        it = review.enqueue("existing_duplicate", payload, [a["id"], b["id"]])
        if it["status"] != "pending":
            return {"skipped": f"already {it['status']} in the review queue", "item_id": it["id"]}
        _verdict(it, "duplicate", confidence, reason, delete_id=delete_id)
        return {"queued_for_review": True, "item_id": it["id"]}

    def propose_transfer(out_id: str, in_id: str, reason: str, confidence: float = 0.7):
        a, b = by_id.get(out_id), by_id.get(in_id)
        if not a or not b:
            return {"error": f"unknown id(s): {[i for i in (out_id, in_id) if i not in by_id]}"}
        if a["account"] == b["account"]:
            return {"error": "a transfer needs two different accounts"}
        if a["amount"] != -b["amount"]:
            return {"error": f"not equal and opposite ({a['amount']} vs {b['amount']}) — flag the mismatch instead"}
        if not (_unlinked(a) and _unlinked(b)):
            return {"error": "already linked as a transfer"}
        if a["amount"] > 0:
            a, b = b, a
        payload = {"a": review._slim(a), "b": review._slim(b), "reason_code": "llm",
                   "reason": str(reason)[:300], "source": source}
        it = review.enqueue("transfer_pair", payload, [a["id"], b["id"]])
        if it["status"] != "pending":
            return {"skipped": f"already {it['status']} in the review queue", "item_id": it["id"]}
        _verdict(it, "transfer", confidence, reason)
        return {"queued_for_review": True, "item_id": it["id"]}

    def flag(ids: list, note: str, kind: str = "other"):
        known = [i for i in ids or [] if i in by_id]
        if not known:
            return {"error": "flag needs at least one id from the tools"}
        state["findings"].append({"kind": kind, "note": str(note)[:400],
                                  "rows": [_brief(by_id[i]) for i in known]})
        return {"flagged": True}

    def _verdict(it, verdict, confidence, reason, **extra):
        try:
            conf = max(0.0, min(1.0, float(confidence)))
        except (TypeError, ValueError):
            conf = 0.5
        v = {"verdict": verdict, "confidence": conf, "reason": str(reason)[:300], "model": source, **extra}
        review.set_llm(it["id"], v)
        state["queued"].append(review.get(it["id"]))

    specs = [
        {"name": "overview", "description": "Accounts in the window with row counts and totals (cents), and how many candidates the strict rules found.",
         "parameters": {"type": "object", "properties": {}}},
        {"name": "rule_candidates", "description": "Duplicates and unlinked transfers the strict rules found, with their review status.",
         "parameters": {"type": "object", "properties": {}}},
        {"name": "loose_duplicates", "description": "Same-amount pairs the strict rules let through (same account within max_days, or the same charge in two accounts). Most suspicious first. Many are legitimate repeats — check with search.",
         "parameters": {"type": "object", "properties": {"max_days": {"type": "integer", "description": "default 7"},
                                                          "account": {"type": "string", "description": "account name filter"}}}},
        {"name": "orphan_transfers", "description": "Rows that read like an own-account transfer or card payment but have no equal-and-opposite partner; each has the nearest look-alikes with amount_diff (cents).",
         "parameters": {"type": "object", "properties": {"max_days": {"type": "integer", "description": "default 5"}}}},
        {"name": "search", "description": "Search rows in the window by text (payee / bank text / notes), account name, amount (cents, sign ignored, optional tolerance) and dates.",
         "parameters": {"type": "object", "properties": {
             "text": {"type": "string"}, "account": {"type": "string"}, "amount_cents": {"type": "integer"},
             "tolerance_cents": {"type": "integer"}, "start": {"type": "string", "description": "YYYY-MM-DD"},
             "end": {"type": "string", "description": "YYYY-MM-DD"}}}},
        {"name": "propose_duplicate", "description": "Queue a duplicate for the user's approval: keep one row, delete the other. Same amount required. Keep the copy with a bank reference (src 'ref') over a file import over a typed-in one.",
         "parameters": {"type": "object", "required": ["keep_id", "delete_id", "reason"], "properties": {
             "keep_id": {"type": "string"}, "delete_id": {"type": "string"},
             "reason": {"type": "string", "description": "one sentence the user will read"},
             "confidence": {"type": "number", "description": "0..1"}}}},
        {"name": "propose_transfer", "description": "Queue linking two rows as one transfer between the user's accounts, for approval. Amounts must be equal and opposite.",
         "parameters": {"type": "object", "required": ["out_id", "in_id", "reason"], "properties": {
             "out_id": {"type": "string", "description": "money-out row"}, "in_id": {"type": "string", "description": "money-in row"},
             "reason": {"type": "string"}, "confidence": {"type": "number"}}}},
        {"name": "flag", "description": "Report something a person must look at that has no safe automatic fix (transfer legs with different amounts, a missing leg, an unsure duplicate).",
         "parameters": {"type": "object", "required": ["ids", "note"], "properties": {
             "ids": {"type": "array", "items": {"type": "string"}}, "note": {"type": "string"},
             "kind": {"type": "string", "enum": ["amount_mismatch", "missing_leg", "possible_duplicate", "other"]}}}},
    ]
    handlers = {"overview": overview, "rule_candidates": rule_candidates, "loose_duplicates": loose_duplicates,
                "orphan_transfers": orphan_transfers, "search": search, "propose_duplicate": propose_duplicate,
                "propose_transfer": propose_transfer, "flag": flag}
    return specs, handlers, state


def run(rows: list[dict], accounts: list[dict], window: tuple[str, str], llm,
        account_last4: dict[str, str] | None = None, instructions: str = "", max_steps: int = 20) -> dict:
    """Run the hunt agent over `rows` (from bridge_client.txns). Returns what it queued and flagged."""
    from llm import run_tools
    specs, handlers, state = tools(rows, accounts, window, account_last4, source=f"llm:{llm.model}")
    ask = (f"Audit Actual from {window[0]} to {window[1]} for duplicates and unreconciled transfers. "
           "Start with overview.")
    if instructions:
        ask += f"\nThe user adds: {instructions.strip()[:500]}"
    res = run_tools(llm, HUNT_SYSTEM, ask, specs, handlers, max_steps=max_steps)
    seen, queued = set(), []
    for it in state["queued"]:          # the model may propose the same pair twice
        if it["id"] not in seen:
            seen.add(it["id"])
            queued.append(it)
    return {"explanation": res["text"], "queued": queued, "findings": state["findings"],
            "tool_calls": [{"tool": c["tool"], "args": c["args"]} for c in res["calls"]],
            "scanned": sum(_plain(t) for t in rows), "window": {"start": window[0], "end": window[1]}}
