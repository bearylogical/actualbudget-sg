"""
Balance reconciliation: make Actual agree with the bank, explain every cent of the
gap, and propose fixes — which go to the review queue, never straight to Actual.

Two layers:

1. Deterministic engine (`analyse`) — always runs, no LLM needed:
     * expected balance from the statement (deposit: +balance; card: −amount owed)
     * Actual's balance for the account as of the same date
     * matches statement rows ↔ Actual rows (bank ref / legacy id, then date+amount)
       → missing rows (in the bank, not in Actual) and extra rows (in Actual, not in
         the bank for that period)
     * duplicates inside the account, unlinked transfers touching it
     * running-balance offsets (UOB account exports carry the bank's running
       balance per row): a constant offset = missing opening balance; the first
       date the offset changes = where something went wrong
     * exact subset solver: which combination of missing/extra rows closes the gap
       to the cent, preferring the fewest changes
     * a recommended plan; any remainder with no history before the statement is an
       opening-balance adjustment

2. LLM agent (`agent`) — optional, uses the configured model with tools that expose
   the engine (summary, missing, extra, duplicates, running-balance offsets, subset
   solver, transfer candidates) plus `propose_fix`, which only queues a proposal.
   The model investigates, explains in plain words, and proposes; you approve.
"""
from __future__ import annotations

import itertools
from datetime import date as _date, timedelta

import review
from accounts import StatementInfo

EPOCH = "2000-01-01"


def _cents(t: dict) -> int:
    """parsed statement row → signed cents (money in positive)."""
    c = round(float(t["amount"]) * 100)
    return c if t.get("is_credit") else -c


def _ids(t: dict) -> set:
    return {i for i in [t.get("imported_id"), *(t.get("legacy_ids") or [])] if i}


def expected_balance(info: StatementInfo, override: float | None = None) -> tuple[int | None, str]:
    if override is not None:
        return round(override * 100), "you entered it"
    if info.balance is None:
        return None, "statement has no balance"
    if info.kind == "credit_card" or info.balance_is_owed:
        return -round(info.balance * 100), ("card statement balance — as of the last statement date, "
                                            "not the export date; enter today's owed amount for an exact check")
    return round(info.balance * 100), "statement / running balance"


def match(statement_rows: list[dict], actual_rows: list[dict]) -> tuple[list, list, list]:
    """→ (matched [(stmt, actual)], missing [stmt], extra [actual]) — one-to-one."""
    by_id: dict[str, list[dict]] = {}
    for a in actual_rows:
        if a.get("imported_id"):
            by_id.setdefault(a["imported_id"], []).append(a)
    claimed, matched, missing = set(), [], []
    pending = []
    for s in statement_rows:
        hit = None
        for i in _ids(s):
            hit = next((a for a in by_id.get(i, []) if a["id"] not in claimed), None)
            if hit:
                break
        if hit:
            claimed.add(hit["id"])
            matched.append((s, hit))
        else:
            pending.append(s)
    for s in pending:   # second pass: same date + amount (PDF vs xls, transfer counterparts, hand-typed)
        hit = next((a for a in actual_rows if a["id"] not in claimed and a["date"] == s["date"]
                    and a["amount"] == _cents(s)), None)
        if hit is None:  # allow ±2 days for hand-typed / counterpart rows
            hit = next((a for a in actual_rows if a["id"] not in claimed and a["amount"] == _cents(s)
                        and abs((_date.fromisoformat(a["date"]) - _date.fromisoformat(s["date"])).days) <= 2
                        and not a.get("imported_id")), None)
        if hit:
            claimed.add(hit["id"])
            matched.append((s, hit))
        else:
            missing.append(s)
    extra = [a for a in actual_rows if a["id"] not in claimed]
    return matched, missing, extra


def subset_to_target(candidates: list[tuple[str, int]], target: int, max_items: int = 4,
                     limit: int = 5) -> list[list[str]]:
    """Exact combinations (ids) whose effects sum to target cents, fewest items first."""
    if target == 0:
        return [[]]
    found = []
    cands = [c for c in candidates if c[1] != 0][:60]
    for k in range(1, max_items + 1):
        if k >= 4 and len(cands) > 30:
            break   # keep it fast; bigger gaps usually mean a missing opening balance
        for combo in itertools.combinations(cands, k):
            if sum(e for _, e in combo) == target:
                found.append([i for i, _ in combo])
                if len(found) >= limit:
                    return found
        if found:
            return found
    return found


def analyse(account_id: str, info: StatementInfo, statement_rows: list[dict],
            actual_rows: list[dict], override_balance: float | None = None,
            as_of: str | None = None, account_name: str = "", other_rows: list[dict] | None = None) -> dict:
    """Pure function: everything needed to explain the gap. actual_rows: this account, full history."""
    as_of = as_of or info.period_end or max((t["date"] for t in statement_rows), default=None)
    start = info.period_start or min((t["date"] for t in statement_rows), default=as_of)
    top = [a for a in actual_rows if not a.get("is_child")]
    actual_bal = sum(a["amount"] for a in top if a["date"] <= as_of)
    expected, basis = expected_balance(info, override_balance)
    # a card export's "Statement Balance" is as of the last statement date, not the export date
    balance_reliable = override_balance is not None or not (info.kind == "credit_card" or info.balance_is_owed)
    gap = None if expected is None else expected - actual_bal

    in_period = [a for a in top if start <= a["date"] <= as_of]
    stmt = [t for t in statement_rows if start <= t["date"] <= as_of]
    matched, missing, extra = match(stmt, in_period)
    history_before = any(a["date"] < start for a in top)

    dups = review.find_existing_duplicates(in_period)
    transfers = review.find_transfer_pairs(in_period + (other_rows or []))
    transfers = [p for p in transfers if account_id in (p["a"]["account"], p["b"]["account"])]
    amount_mismatch = [(s, a) for s, a in matched if a["amount"] != _cents(s)]

    # running-balance offsets (bank running balance vs Actual's balance after that day)
    offsets = []
    rb = [t for t in stmt if t.get("running_balance") is not None]
    if rb:
        by_day: dict[str, float] = {}
        # exports list newest first, so the FIRST row seen for a day is its closing balance
        for t in rb:
            by_day.setdefault(t["date"], t["running_balance"])
        for day, bank in sorted(by_day.items()):
            act = sum(a["amount"] for a in top if a["date"] <= day)
            offsets.append({"date": day, "bank": round(bank * 100), "actual": act, "offset": round(bank * 100) - act})
    first_divergence = None
    if len(offsets) > 1:
        base = offsets[0]["offset"]
        first_divergence = next((o for o in offsets[1:] if o["offset"] != base), None)

    effects = [(f"import:{s['imported_id']}", _cents(s)) for s in missing] + \
              [(f"delete:{a['id']}", -a["amount"]) for a in extra]
    exact = subset_to_target(effects, gap) if gap not in (None, 0) else ([] if gap == 0 else None)

    full_sync_effect = sum(e for _, e in effects)
    residual = None if gap is None else gap - full_sync_effect

    report = {
        "account": account_id, "account_name": account_name, "statement": info.label,
        "as_of": as_of, "period_start": start, "basis": basis,
        "expected_balance": expected, "actual_balance": actual_bal, "gap": gap,
        "balance_reliable": balance_reliable,
        "reconciled": gap == 0,
        "matched": len(matched),
        "missing": [_brief_s(s) for s in missing],
        "extra": [_brief_a(a) for a in extra],
        "amount_mismatch": [{"statement": _brief_s(s), "actual": _brief_a(a)} for s, a in amount_mismatch],
        "duplicates": dups, "transfers": transfers,
        "history_before_statement": history_before,
        "skipped_pending": info.skipped_pending,
        "running_balance": {"offsets": offsets[-10:], "first_divergence": first_divergence,
                            "constant_offset": offsets[0]["offset"] if offsets and not first_divergence else None},
        "exact_combinations": exact,
        "residual_after_full_sync": residual,
    }
    report["recommendation"] = recommend(report, missing, extra)
    return report


def _brief_s(s: dict) -> dict:
    return {"imported_id": s.get("imported_id"), "date": s["date"], "amount": _cents(s),
            "payee": s.get("payee"), "description": s.get("description")}


def _brief_a(a: dict) -> dict:
    return {"id": a["id"], "date": a["date"], "amount": a["amount"], "payee": a.get("payee"),
            "imported_id": a.get("imported_id"), "imported_payee": a.get("imported_payee"),
            "transfer": bool(a.get("transfer_id"))}


def recommend(r: dict, missing: list[dict], extra: list[dict]) -> list[dict]:
    """Deterministic plan: list of proposals, each {fix_type, title, why, actions, effect}."""
    acct = r["account"]
    plans = []
    if r["gap"] == 0 and not missing and not r["duplicates"]:
        return []
    for d in r["duplicates"]:
        a, b = d["a"], d["b"]
        drop = b if str(a.get("imported_id") or "").startswith("ref-") or not b.get("imported_id") else a
        plans.append({"fix_type": "delete_duplicate", "title": f"Delete duplicate {drop.get('payee')} {drop['amount']/100:.2f} on {drop['date']}",
                      "why": d["reason"], "actions": [{"type": "delete", "ids": [drop["id"]]}], "effect": -drop["amount"]})
    dup_ids = {p["actions"][0]["ids"][0] for p in plans}
    if missing:
        plans.append({"fix_type": "import_missing", "title": f"Import {len(missing)} transaction(s) the bank has but Actual doesn't",
                      "why": "in the statement period, not found in Actual by bank reference or date+amount",
                      "actions": [{"type": "import", "account": acct, "transactions": missing}],
                      "effect": sum(_cents(s) for s in missing)})
    explained = sum(p["effect"] for p in plans)
    gap = r["gap"]
    if gap is not None and gap != explained and not r["balance_reliable"]:
        plans.append({"fix_type": "investigate", "title": "Enter what you owe today to check the card balance",
                      "why": r["basis"], "actions": [], "effect": 0})
        return plans
    if gap is not None and gap != explained:
        remainder = gap - explained
        other_extra = [a for a in extra if a["id"] not in dup_ids]
        if not r["history_before_statement"] and not r["running_balance"]["first_divergence"]:
            day = (_date.fromisoformat(r["period_start"]) - timedelta(days=1)).isoformat()
            plans.append({"fix_type": "opening_balance", "title": f"Add opening balance {remainder/100:,.2f} on {day}",
                          "why": "Actual has no history before this statement, so the balance before its first "
                                 "transaction is missing" + (" — the bank's running balance shows the same constant offset"
                                                             if r["running_balance"]["constant_offset"] is not None else ""),
                          "actions": [{"type": "adjust", "account": acct, "amount_cents": remainder, "date": day,
                                       "payee": "Opening balance", "note": "#reconcile opening balance"}],
                          "effect": remainder})
        elif r["exact_combinations"]:
            combo = r["exact_combinations"][0]
            dels = [c.split(":", 1)[1] for c in combo if c.startswith("delete:")]
            if dels and all(c.startswith("delete:") for c in combo):
                plans.append({"fix_type": "delete_extra", "title": f"Delete {len(dels)} row(s) that aren't on the bank statement",
                              "why": "these rows exactly close the gap and don't appear in the statement",
                              "actions": [{"type": "delete", "ids": dels}], "effect": remainder})
        if not any(p["fix_type"] in ("opening_balance", "delete_extra") for p in plans) and other_extra:
            plans.append({"fix_type": "investigate", "title": f"{len(other_extra)} row(s) in Actual aren't on the statement",
                          "why": "not applied automatically — could be hand-typed, wrong account, or a transfer counterpart",
                          "actions": [], "effect": 0, "rows": [_brief_a(a) for a in other_extra][:20]})
    return plans


def queue(report: dict, plans: list[dict] | None = None, source: str = "engine") -> list[dict]:
    """Put proposals with actions into the review queue (gated)."""
    out = []
    for p in plans if plans is not None else report["recommendation"]:
        if not p.get("actions"):
            continue
        payload = {**p, "account": report["account"], "account_name": report["account_name"],
                   "gap": report["gap"], "as_of": report["as_of"], "source": source}
        key = [report["account"], p["fix_type"], report["as_of"], str(p.get("effect"))]
        out.append(review.enqueue("reconcile_fix", payload, key))
    return out


# ── LLM agent ─────────────────────────────────────────────────────────────────

AGENT_SYSTEM = """You are a careful bookkeeper reconciling one account in Actual Budget against a
Singapore bank statement. Use the tools to find out why the balances differ, then call
propose_fix for each change needed so Actual matches the bank EXACTLY. Rules:
- Every proposal must be backed by tool output (ids, amounts). Never invent ids.
- Prefer the smallest set of changes that closes the gap to the cent (use solve_gap).
- Duplicates, missing rows and unlinked transfers come before an opening-balance adjustment;
  use an adjustment only for the balance before the statement starts, or a clearly explained remainder.
- Nothing you propose is applied until the user approves it.
Finish with a short plain-English explanation: gap, cause(s), proposals, anything left unexplained."""


def agent_tools(report: dict, statement_rows: list[dict], extra_rows: list[dict]) -> tuple[list[dict], dict, list]:
    proposals: list[dict] = []
    missing_by_id = {m["imported_id"]: m for m in report["missing"]}
    stmt_by_id = {s["imported_id"]: s for s in statement_rows}
    extra_by_id = {a["id"]: a for a in report["extra"]}
    dup_ids = {x for d in report["duplicates"] for x in (d["a"]["id"], d["b"]["id"])}

    def summary():
        keys = ("account_name", "statement", "as_of", "period_start", "basis", "expected_balance",
                "actual_balance", "gap", "reconciled", "matched", "history_before_statement",
                "skipped_pending", "residual_after_full_sync")
        return {k: report[k] for k in keys} | {"amounts_are": "cents, money in positive",
                                               "n_missing": len(report["missing"]), "n_extra": len(report["extra"]),
                                               "n_duplicates": len(report["duplicates"]),
                                               "n_transfer_candidates": len(report["transfers"])}

    def list_missing():
        return report["missing"]

    def list_extra():
        return report["extra"]

    def list_duplicates():
        return report["duplicates"]

    def running_balance():
        return report["running_balance"]

    def transfer_candidates():
        return report["transfers"]

    def solve_gap(target_cents: int | None = None, max_items: int = 4):
        target = report["gap"] if target_cents is None else int(target_cents)
        effects = [(f"import:{m['imported_id']}", m["amount"]) for m in report["missing"]] + \
                  [(f"delete:{a['id']}", -a["amount"]) for a in report["extra"]]
        return {"target": target, "combinations": subset_to_target(effects, target, max_items=max_items)}

    def propose_fix(title: str, explanation: str, import_ids: list | None = None,
                    delete_ids: list | None = None, link_pairs: list | None = None,
                    adjustment_cents: int | None = None, adjustment_date: str | None = None):
        actions, effect = [], 0
        bad = [i for i in (import_ids or []) if i not in missing_by_id] + \
              [i for i in (delete_ids or []) if i not in extra_by_id and i not in dup_ids]
        if bad:
            return {"error": f"unknown ids (use ids from the tools): {bad}"}
        if import_ids:
            rows = [stmt_by_id[i] for i in import_ids if i in stmt_by_id]
            actions.append({"type": "import", "account": report["account"], "transactions": rows})
            effect += sum(_cents(r) for r in rows)
        if delete_ids:
            actions.append({"type": "delete", "ids": list(delete_ids)})
            effect -= sum(extra_by_id[i]["amount"] for i in delete_ids if i in extra_by_id)
        for pair in link_pairs or []:
            tp = next((p for p in report["transfers"] if {p["a"]["id"], p["b"]["id"]} == set(pair)), None)
            if not tp:
                return {"error": f"not a known transfer candidate: {pair}"}
            a, b = tp["a"], tp["b"]
            actions.append({"type": "link", "keep_id": a["id"], "other_id": b["id"], "keep_account": a["account"],
                            "other_account": b["account"], "date": a["date"]})
        if adjustment_cents:
            actions.append({"type": "adjust", "account": report["account"], "amount_cents": int(adjustment_cents),
                            "date": adjustment_date or report["period_start"], "payee": "Reconciliation adjustment",
                            "note": f"#reconcile {title}"[:120]})
            effect += int(adjustment_cents)
        if not actions:
            return {"error": "a proposal needs at least one action"}
        proposals.append({"fix_type": "llm_" + ("adjust" if adjustment_cents and len(actions) == 1 else "fix"),
                          "title": title, "why": explanation, "actions": actions, "effect": effect})
        return {"queued_for_review": True, "effect_cents": effect,
                "gap_after_if_applied": (report["gap"] or 0) - sum(p["effect"] for p in proposals)}

    specs = [
        {"name": "get_summary", "description": "Bank vs Actual balance, the gap (cents), dates and counts.",
         "parameters": {"type": "object", "properties": {}}},
        {"name": "list_missing", "description": "Statement rows not found in Actual (would be imported). amount in cents.",
         "parameters": {"type": "object", "properties": {}}},
        {"name": "list_extra", "description": "Actual rows in the statement period that the statement doesn't have.",
         "parameters": {"type": "object", "properties": {}}},
        {"name": "list_duplicates", "description": "Pairs inside the account that look like the same transaction twice.",
         "parameters": {"type": "object", "properties": {}}},
        {"name": "running_balance", "description": "Bank running balance vs Actual per day; constant offset = missing opening balance; first_divergence = where a problem starts.",
         "parameters": {"type": "object", "properties": {}}},
        {"name": "transfer_candidates", "description": "Unlinked transfers between this and another account.",
         "parameters": {"type": "object", "properties": {}}},
        {"name": "solve_gap", "description": "Exact combinations of importing missing rows / deleting extra rows that sum to the target (default: the gap).",
         "parameters": {"type": "object", "properties": {"target_cents": {"type": "integer"}, "max_items": {"type": "integer"}}}},
        {"name": "propose_fix", "description": "Queue ONE fix for the user's approval. Use ids from the other tools.",
         "parameters": {"type": "object", "required": ["title", "explanation"], "properties": {
             "title": {"type": "string"}, "explanation": {"type": "string"},
             "import_ids": {"type": "array", "items": {"type": "string"}, "description": "imported_id values from list_missing"},
             "delete_ids": {"type": "array", "items": {"type": "string"}, "description": "id values from list_extra / list_duplicates"},
             "link_pairs": {"type": "array", "items": {"type": "array", "items": {"type": "string"}}, "description": "[[id_a, id_b]] from transfer_candidates"},
             "adjustment_cents": {"type": "integer"}, "adjustment_date": {"type": "string"}}}},
    ]
    handlers = {"get_summary": summary, "list_missing": list_missing, "list_extra": list_extra,
                "list_duplicates": list_duplicates, "running_balance": running_balance,
                "transfer_candidates": transfer_candidates, "solve_gap": solve_gap, "propose_fix": propose_fix}
    return specs, handlers, proposals


def agent(report: dict, statement_rows: list[dict], llm) -> dict:
    from llm import run_tools
    specs, handlers, proposals = agent_tools(report, statement_rows, [])
    res = run_tools(llm, AGENT_SYSTEM,
                    f"Reconcile '{report['account_name']}' against {report['statement']} as of {report['as_of']}. "
                    "Start with get_summary.", specs, handlers)
    queued = queue(report, proposals, source=f"llm:{llm.model}")
    return {"explanation": res["text"], "tool_calls": [{"tool": c["tool"], "args": c["args"]} for c in res["calls"]],
            "queued": queued}
