"""Money signs, decided once.

A Statement row carries `amount` (SGD, never negative) and `is_credit` (money in). Actual stores
signed integer cents, money in positive. Every crossing between the two goes through here:

  signed_cents(row)        Statement row → signed cents
  statement_amount(cents)  signed cents → (amount, is_credit)
  to_actual(row)           Statement row → the row actual-bridge hands to Actual's importTransactions,
                           splits included; the bridge passes amounts through as they are
  restate_import(rows, r)  the bridge's /import answer, with its rows put back as Statement rows
"""
from __future__ import annotations


def signed_cents(t: dict, is_credit: bool | None = None) -> int:
    """Statement row (or split line) → signed cents. is_credit overrides the row's own (split lines
    take their parent's direction unless they say otherwise)."""
    c = round(abs(float(t.get("amount") or 0)) * 100)
    credit = t.get("is_credit") if is_credit is None else is_credit
    return c if credit else -c


def statement_amount(cents: int) -> tuple[float, bool]:
    """Signed cents → (amount, is_credit) as a Statement row carries them."""
    return abs(cents) / 100, cents > 0


def to_actual(t: dict) -> dict:
    """Statement row → Actual row (signed cents). payee_name is the clean payee (payees.py) so
    Actual's per-payee learning works; the raw statement text is kept in imported_payee.
    legacy_ids ride along for the bridge's duplicate check and are not stored in Actual."""
    row = {
        "date": t["date"],
        "amount": signed_cents(t),
        "imported_payee": t.get("description") or "",
        "payee_name": str(t.get("payee") or t.get("description") or "").strip(),
        "notes": t.get("notes") or "",
        "cleared": True,
    }
    if t.get("imported_id"):
        row["imported_id"] = t["imported_id"]
    legacy = [i for i in (t.get("legacy_ids") or ([t["legacy_id"]] if t.get("legacy_id") else [])) if i]
    if legacy:
        row["legacy_ids"] = legacy
    if t.get("category_id"):
        row["category"] = t["category_id"]
    splits = t.get("splits")
    if isinstance(splits, list) and len(splits) > 1:
        credit = bool(t.get("is_credit"))
        row["subtransactions"] = [
            {"amount": signed_cents(s, s.get("is_credit", credit)), "notes": s.get("notes") or "",
             **({"category": s["category_id"]} if s.get("category_id") else {})}
            for s in splits]
    return row


def restate_import(rows: list[dict], result: dict) -> dict:
    """The bridge's /import answer speaks Actual rows; hand back toVerify / unreviewed as the
    Statement rows that were sent (plus the bridge's reason), so callers never see two sign schemes."""
    by_id = {t.get("imported_id"): t for t in rows if t.get("imported_id")}

    def back(r: dict) -> dict:
        t = by_id.get(r.get("imported_id"))
        if t is None:
            amount, credit = statement_amount(int(r.get("amount") or 0))
            t = {"imported_id": r.get("imported_id"), "date": r.get("date"),
                 "description": r.get("imported_payee") or "", "amount": amount, "is_credit": credit}
        return {**t, "reason": r.get("reason")}

    out = dict(result)
    if "toVerify" in result:
        out["toVerify"] = [back(r) for r in result.get("toVerify") or []]
    if "unreviewed" in result:
        out["unreviewed"] = [{k: b.get(k) for k in ("imported_id", "date", "description", "amount", "reason")}
                             for b in (back(r) for r in result.get("unreviewed") or [])]
    return out
