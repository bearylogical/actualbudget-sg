"""
Unified money view + spend guidance.

Cash & spending come from Actual (UOB, POSB, Wise …), investments from Ghostfolio
(IBKR …). Every number here is computed deterministically; the optional AI brief
only turns these numbers into words.

Guidance is budgeting guidance about your own cash flow — not investment advice.
"""
from __future__ import annotations

import calendar
from datetime import date

CARD_WORDS = ("card", "credit", "visa", "master", "amex")


def _is_card(acct: dict, card_ids: set[str]) -> bool:
    return acct["id"] in card_ids or any(w in (acct.get("name") or "").lower() for w in CARD_WORDS)


def compute(actual: dict, ghost: dict | None, card_ids: set[str] | None = None,
            review_pending: int = 0, today: date | None = None) -> dict:
    """actual = bridge /finance/summary; ghost = ghostfolio.summary(). All money in cents."""
    today = today or date.fromisoformat(actual.get("today") or date.today().isoformat())
    card_ids = card_ids or set()
    accts = [a for a in actual["accounts"] if a.get("balance") is not None]

    cash = sum(a["balance"] for a in accts if not a["offbudget"] and not _is_card(a, card_ids) and a["balance"] > 0)
    card_owed = -sum(a["balance"] for a in accts if _is_card(a, card_ids) and a["balance"] < 0)
    offbudget = sum(a["balance"] for a in accts if a["offbudget"])
    actual_total = sum(a["balance"] for a in accts)
    invest = round((ghost or {}).get("value") * 100) if (ghost or {}).get("value") is not None else 0
    net_worth = actual_total + invest

    months = [m for m in actual["months"] if "error" not in m]
    # drop the empty months before your data starts
    while months and not months[0]["income"] and not months[0]["spent"]:
        months.pop(0)
    cur_key = today.strftime("%Y-%m")
    current = next((m for m in months if m["month"] == cur_key), None)
    past = [m for m in months if m["month"] < cur_key]
    # the first month of data is usually partial (statements start mid-month) — skip it when we can
    if len(past) > 2:
        past = past[1:]
    full = past[-3:]                                                    # last 3 complete months
    avg_spend = round(sum(-m["spent"] for m in full) / len(full)) if full else None
    avg_income = round(sum(m["income"] for m in full) / len(full)) if full else None
    savings_rate = None
    if full and sum(m["income"] for m in full) > 0:
        inc = sum(m["income"] for m in full)
        savings_rate = (inc - sum(-m["spent"] for m in full)) / inc
    emergency_months = (cash / avg_spend) if avg_spend else None

    days_in = calendar.monthrange(today.year, today.month)[1]
    day = today.day
    progress = day / days_in
    spent_now = -current["spent"] if current else 0
    budgeted_now = current["budgeted"] if current else 0
    usual_by_today = round(avg_spend * progress) if avg_spend else None
    projected = round(spent_now / progress) if progress > 0 else None
    days_left = days_in - day + 1

    # safe to spend: against your budget if you set one, else against your usual month
    if budgeted_now > 0:
        remaining = sum(max(0, c["balance"]) for c in (current or {}).get("categories", []) if not c["is_income"])
        basis = "remaining budget"
    elif avg_spend:
        remaining = max(0, avg_spend - spent_now)
        basis = "your 3-month average spending"
    else:
        remaining, basis = None, None
    safe_per_day = round(remaining / days_left) if remaining is not None else None

    # category pacing vs your own 3-month average
    cats = []
    if current:
        hist: dict[str, list[int]] = {}
        for m in full:
            for c in m["categories"]:
                if not c["is_income"]:
                    hist.setdefault(c["id"], []).append(-c["spent"])
        for c in current["categories"]:
            if c["is_income"]:
                continue
            avg = round(sum(hist.get(c["id"], [])) / len(full)) if full else 0
            spent = -c["spent"]
            if not spent and not avg:
                continue
            expected = avg * progress
            status = "good"
            if avg and spent > expected * 1.25 and spent - expected > 3000:
                status = "serious" if spent > avg else "warning"
            elif not avg and spent > 5000:
                status = "warning"
            cats.append({"id": c["id"], "name": c["name"], "group": c["group"], "spent": spent, "avg": avg,
                         "expected_by_today": round(expected), "budgeted": c["budgeted"], "status": status})
        cats.sort(key=lambda c: -c["spent"])

    guidance = []

    def g(level, title, detail):
        guidance.append({"level": level, "title": title, "detail": detail})

    unc = actual.get("uncategorised") or {}
    if review_pending:
        g("warning", f"{review_pending} item(s) waiting in Review",
          "Possible duplicates, unlinked transfers or balance fixes — numbers below may be off until they're resolved.")
    if unc.get("count"):
        g("warning", f"{unc['count']} uncategorised transaction(s) this month ({-unc['amount']/100:,.2f})",
          "Uncategorised spending isn't counted in the category figures.")
    if usual_by_today is not None and spent_now > usual_by_today * 1.15 and spent_now - usual_by_today > 10000:
        g("serious", "Spending is ahead of your usual pace",
          f"{spent_now/100:,.0f} spent by day {day} vs {usual_by_today/100:,.0f} usually — on track for "
          f"{projected/100:,.0f} this month vs {avg_spend/100:,.0f} average.")
    elif usual_by_today is not None:
        g("good", "Spending is on or under your usual pace",
          f"{spent_now/100:,.0f} spent by day {day} vs {usual_by_today/100:,.0f} usually.")
    for c in [c for c in cats if c["status"] in ("warning", "serious")][:3]:
        g(c["status"], f"{c['name']} is running hot",
          f"{c['spent']/100:,.0f} so far vs {c['avg']/100:,.0f} in a usual month.")
    if card_owed and cash and card_owed > cash * 0.5:
        g("serious", "Card balance is large relative to cash", f"{card_owed/100:,.0f} owed vs {cash/100:,.0f} cash.")
    if emergency_months is not None:
        if emergency_months < 3:
            g("serious", f"Emergency buffer is {emergency_months:.1f} months", "Common guidance is 3–6 months of spending in cash.")
        elif emergency_months > 12 and savings_rate is not None:
            g("info", f"{emergency_months:.0f} months of spending sits in cash",
              "Well above a typical 3–6 month buffer — worth deciding what the extra is for.")
    if savings_rate is not None:
        level = "good" if savings_rate >= 0.2 else ("warning" if savings_rate >= 0 else "critical")
        g(level, f"Savings rate {savings_rate:.0%} (last 3 months)",
          f"Income {avg_income/100:,.0f} vs spending {avg_spend/100:,.0f} per month on average.")
    if safe_per_day is not None:
        g("info", f"Safe to spend ≈ {safe_per_day/100:,.0f}/day for the rest of the month",
          f"Based on {basis}, {days_left} day(s) left.")

    return {
        "as_of": today.isoformat(),
        "net_worth": net_worth, "cash": cash, "card_owed": card_owed, "offbudget": offbudget,
        "investments": invest, "investments_detail": ghost or {"configured": False},
        "accounts": sorted(accts, key=lambda a: -abs(a["balance"])),
        "savings_rate": savings_rate, "emergency_months": emergency_months,
        "baseline_months": [m["month"] for m in full],
        "avg_spend": avg_spend, "avg_income": avg_income,
        "month": {"key": cur_key, "day": day, "days": days_in, "progress": progress, "spent": spent_now,
                  "usual_by_today": usual_by_today, "projected": projected, "budgeted": budgeted_now,
                  "safe_per_day": safe_per_day, "safe_basis": basis, "days_left": days_left},
        "cashflow": [{"month": m["month"], "income": m["income"], "spent": -m["spent"]} for m in months],
        "categories": cats[:14],
        "uncategorised": unc,
        "review_pending": review_pending,
        "guidance": guidance,
    }


BRIEF_PROMPT = """You write a short weekly money brief for a person in Singapore from the JSON
figures provided (amounts in cents, SGD). Be concrete and kind, no jargon, max 6 bullets:
where money went, pace vs usual, anything to act on this week, and one encouraging note.
Do not give investment or product recommendations. Respond as JSON: {"bullets": ["..."]}"""


def brief(summary: dict, llm) -> list[str]:
    from llm import complete_json
    slim = {k: summary[k] for k in ("net_worth", "cash", "card_owed", "investments", "savings_rate",
                                     "emergency_months", "avg_spend", "avg_income", "month", "guidance")}
    slim["categories"] = [{k: c[k] for k in ("name", "spent", "avg", "status")} for c in summary["categories"]]
    data = complete_json(llm, BRIEF_PROMPT, __import__("json").dumps(slim, default=str)) or {}
    return [str(b) for b in data.get("bullets", [])][:6]
