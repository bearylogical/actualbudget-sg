"""
A small, conservative evaluator for Actual Budget rules.

Purpose: PREVIEW what Actual will do on import so the UI shows the category your
own Actual rules assign (and so the LLM is only asked about transactions no rule
covers). The real import still runs Actual's own rules engine — this module never
writes anything.

Supported:  fields imported_payee / payee / notes / account / amount
            ops    is, isNot, contains, doesNotContain, matches, oneOf, notOneOf,
                   gt, gte, lt, lte, isapprox, isbetween
            actions  set category / set payee
Anything else makes that rule "unsupported" and it is skipped (never guessed).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

STAGE_ORDER = {"pre": 0, None: 1, "default": 1, "": 1, "post": 2}


@dataclass
class ActualContext:
    categories: list[dict] = field(default_factory=list)   # flat: id,name,group_id,group_name,is_income,hidden
    payees: list[dict] = field(default_factory=list)       # id,name,transfer_acct
    rules: list[dict] = field(default_factory=list)
    accounts: list[dict] = field(default_factory=list)

    def __post_init__(self):
        self.cat_by_id = {c["id"]: c for c in self.categories}
        self.payee_by_id = {p["id"]: p for p in self.payees}
        self.payee_by_name = {}
        for p in self.payees:
            if p.get("name"):
                self.payee_by_name.setdefault(p["name"].strip().lower(), p)

    @classmethod
    def from_bridge(cls, data: dict) -> "ActualContext":
        cats = data.get("categories")
        if not cats:
            cats = []
            for g in data.get("categoryGroups", []) or []:
                for c in g.get("categories", []) or []:
                    cats.append({**c, "group_id": g.get("id"), "group_name": g.get("name"),
                                 "is_income": bool(g.get("is_income"))})
        return cls(categories=cats, payees=data.get("payees", []) or [],
                   rules=data.get("rules", []) or [], accounts=data.get("accounts", []) or [])


def _s(v) -> str:
    return "" if v is None else str(v).strip().lower()


def _regex(pattern: str):
    try:
        return re.compile(pattern, re.I)
    except re.error:
        return None


def _eval_condition(cond: dict, txn: dict, ctx: ActualContext):
    """True / False, or None when unsupported."""
    fld, op, val = cond.get("field"), cond.get("op"), cond.get("value")

    if fld in ("imported_payee", "notes"):
        actual = _s(txn.get("imported_payee" if fld == "imported_payee" else "notes"))
        if op == "is":
            return actual == _s(val)
        if op == "isNot":
            return actual != _s(val)
        if op == "contains":
            return _s(val) in actual
        if op == "doesNotContain":
            return _s(val) not in actual
        if op == "matches":
            rx = _regex(str(val))
            return None if rx is None else bool(rx.search(actual))
        if op in ("oneOf", "notOneOf"):
            hit = actual in {_s(v) for v in (val or [])}
            return hit if op == "oneOf" else not hit
        return None

    if fld == "payee":
        pid = txn.get("payee_id")
        pname = _s(txn.get("payee"))
        def is_payee(ref):
            p = ctx.payee_by_id.get(ref)
            return ref == pid or (p is not None and _s(p.get("name")) == pname)
        if op == "is":
            return is_payee(val)
        if op == "isNot":
            return not is_payee(val)
        if op in ("oneOf", "notOneOf"):
            hit = any(is_payee(v) for v in (val or []))
            return hit if op == "oneOf" else not hit
        if op in ("contains", "doesNotContain", "matches"):
            # string ops on payee compare against the payee *name*
            if op == "matches":
                rx = _regex(str(val))
                return None if rx is None else bool(rx.search(pname))
            hit = _s(val) in pname
            return hit if op == "contains" else not hit
        return None

    if fld == "account":
        acct = txn.get("account_id")
        if acct is None:
            return None
        if op == "is":
            return acct == val
        if op == "isNot":
            return acct != val
        if op in ("oneOf", "notOneOf"):
            hit = acct in (val or [])
            return hit if op == "oneOf" else not hit
        return None

    if fld == "amount":
        amt = txn.get("amount_cents")
        if amt is None:
            return None
        opts = cond.get("options") or {}
        if opts.get("inflow") and amt <= 0:
            return False
        if opts.get("outflow"):
            if amt >= 0:
                return False
            amt = -amt
        try:
            if op == "is":
                return amt == int(val)
            if op == "gt":
                return amt > int(val)
            if op == "gte":
                return amt >= int(val)
            if op == "lt":
                return amt < int(val)
            if op == "lte":
                return amt <= int(val)
            if op == "isapprox":
                v = int(val)
                return abs(amt - v) <= abs(v) * 0.075
            if op == "isbetween":
                lo, hi = sorted([int(val["num1"]), int(val["num2"])])
                return lo <= amt <= hi
        except (TypeError, ValueError, KeyError):
            return None
        return None

    return None


def rule_matches(rule: dict, txn: dict, ctx: ActualContext):
    conds = rule.get("conditions") or []
    if not conds:
        return None
    results = [_eval_condition(c, txn, ctx) for c in conds]
    if any(r is None for r in results):
        return None
    return any(results) if rule.get("conditionsOp") == "or" else all(results)


def apply_rules(txn: dict, ctx: ActualContext) -> dict:
    """
    Run the (supported subset of) Actual rules over a txn dict with keys:
      imported_payee, payee, notes, amount_cents, account_id
    Returns {"payee": name|None, "category_id": id|None, "rule_ids": [...]}.
    """
    t = dict(txn)
    out = {"payee": None, "category_id": None, "rule_ids": []}
    rules = sorted(ctx.rules, key=lambda r: STAGE_ORDER.get(r.get("stage"), 1))
    for rule in rules:
        if not rule_matches(rule, t, ctx):
            continue
        applied = False
        for a in rule.get("actions") or []:
            if a.get("op") != "set":
                continue
            if a.get("field") == "category" and a.get("value") in ctx.cat_by_id:
                out["category_id"] = a["value"]
                applied = True
            elif a.get("field") == "payee" and a.get("value") in ctx.payee_by_id:
                p = ctx.payee_by_id[a["value"]]
                t["payee"], t["payee_id"] = p["name"], p["id"]
                out["payee"] = p["name"]
                applied = True
        if applied:
            out["rule_ids"].append(rule.get("id"))
    return out
