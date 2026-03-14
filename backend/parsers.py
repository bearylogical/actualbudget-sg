"""
Multi-bank statement parsers for UOB, DBS/POSB, and OCBC.
Each parser returns a list of standardised transaction dicts.
"""
import re
from typing import Optional
import pandas as pd
from categorizer import categorize_transaction   # top-level import


def _clean_desc(raw) -> str:
    return re.sub(r'\s+', ' ', str(raw).split("\n")[0].strip())


def _std(date: str, desc: str, amount: float, currency: str = "SGD",
         foreign_amount=None, foreign_currency=None) -> dict:
    category, confidence = categorize_transaction(desc)
    return {
        "date": date,
        "description": desc,
        "amount": abs(amount),
        "currency": currency,
        "is_credit": amount < 0,
        "category": category,
        "confidence": confidence,
        "foreign_amount": float(foreign_amount) if foreign_amount is not None and pd.notna(foreign_amount) else None,
        "foreign_currency": foreign_currency if foreign_currency and str(foreign_currency) not in ("nan", "") else None,
    }


# ── UOB ───────────────────────────────────────────────────────────────────────

def parse_uob(df: pd.DataFrame) -> list[dict]:
    header_row = next(
        (i for i, row in df.iterrows() if any("Transaction Date" in str(v) for v in row.values)),
        None
    )
    if header_row is None:
        raise ValueError("UOB: could not find transaction header row")

    df = df.copy()
    df.columns = df.iloc[header_row]
    data = df.iloc[header_row + 1:].reset_index(drop=True)

    txns = []
    for _, row in data.iterrows():
        txn_date = str(row.get("Transaction Date", "")).strip()
        description = str(row.get("Description", "")).strip()
        amount_raw = row.get("Transaction Amount(Local)", None)
        currency = str(row.get("Local Currency Type", "SGD")).strip()
        foreign_amount = row.get("Transaction Amount(Foreign)", None)
        foreign_currency = str(row.get("Foreign Currency Type", "")).strip()

        if not txn_date or txn_date in ("nan", "NaT", "") or not description or description == "nan":
            continue
        try:
            amount = float(amount_raw)
        except (TypeError, ValueError):
            continue
        try:
            date_str = pd.to_datetime(txn_date, format="%d %b %Y").strftime("%Y-%m-%d")
        except Exception:
            continue

        txns.append(_std(date_str, _clean_desc(description), amount, currency, foreign_amount, foreign_currency))
    return txns


# ── DBS / POSB ────────────────────────────────────────────────────────────────

def parse_dbs(df: pd.DataFrame) -> list[dict]:
    header_row = None
    for i, row in df.iterrows():
        vals = [str(v).strip().lower() for v in row.values]
        if any("transaction date" in v or (v == "date" and "debit" in " ".join(vals)) for v in vals):
            header_row = i
            break
    if header_row is None:
        raise ValueError("DBS: could not find transaction header row")

    df = df.copy()
    df.columns = [str(c).strip() for c in df.iloc[header_row]]
    data = df.iloc[header_row + 1:].reset_index(drop=True)

    col_map: dict[str, str] = {}
    for col in data.columns:
        cl = col.lower()
        if "date" in cl:
            col_map.setdefault("date", col)
        elif "debit" in cl:
            col_map["debit"] = col
        elif "credit" in cl:
            col_map["credit"] = col
        elif any(k in cl for k in ("description", "reference", "particulars", "narration")):
            col_map.setdefault("desc", col)

    if "date" not in col_map or "desc" not in col_map:
        raise ValueError("DBS: required columns not found")

    txns = []
    for _, row in data.iterrows():
        raw_date = str(row.get(col_map["date"], "")).strip()
        if not raw_date or raw_date == "nan":
            continue
        desc = _clean_desc(row.get(col_map["desc"], ""))
        if not desc or desc == "nan":
            continue
        debit = _to_float(row.get(col_map.get("debit"), None))
        credit = _to_float(row.get(col_map.get("credit"), None))
        if debit is None and credit is None:
            continue
        amount = -(credit or 0) if credit else (debit or 0)
        try:
            date_str = _parse_date(raw_date)
        except Exception:
            continue
        txns.append(_std(date_str, desc, amount))
    return txns


# ── OCBC ──────────────────────────────────────────────────────────────────────

def parse_ocbc(df: pd.DataFrame) -> list[dict]:
    header_row = None
    for i, row in df.iterrows():
        vals = [str(v).strip().lower() for v in row.values]
        if "date" in vals and any("withdrawal" in v or "debit" in v for v in vals):
            header_row = i
            break
    if header_row is None:
        raise ValueError("OCBC: could not find transaction header row")

    df = df.copy()
    df.columns = [str(c).strip() for c in df.iloc[header_row]]
    data = df.iloc[header_row + 1:].reset_index(drop=True)

    col_map: dict[str, str] = {}
    for col in data.columns:
        cl = col.lower()
        if cl == "date":
            col_map["date"] = col
        elif "withdrawal" in cl or "debit" in cl:
            col_map["debit"] = col
        elif "deposit" in cl or "credit" in cl:
            col_map["credit"] = col
        elif any(k in cl for k in ("description", "transaction", "detail", "remarks")):
            col_map.setdefault("desc", col)

    if "date" not in col_map or "desc" not in col_map:
        raise ValueError("OCBC: required columns not found")

    txns = []
    for _, row in data.iterrows():
        raw_date = str(row.get(col_map["date"], "")).strip()
        if not raw_date or raw_date == "nan":
            continue
        desc = _clean_desc(row.get(col_map["desc"], ""))
        if not desc or desc == "nan":
            continue
        debit = _to_float(row.get(col_map.get("debit"), None))
        credit = _to_float(row.get(col_map.get("credit"), None))
        if debit is None and credit is None:
            continue
        amount = -(credit or 0) if credit else (debit or 0)
        try:
            date_str = _parse_date(raw_date)
        except Exception:
            continue
        txns.append(_std(date_str, desc, amount))
    return txns


# ── utilities ─────────────────────────────────────────────────────────────────

def _to_float(val) -> Optional[float]:
    if val is None:
        return None
    try:
        f = float(str(val).replace(",", "").strip())
        return f if f != 0 else None
    except (ValueError, TypeError):
        return None


def _parse_date(raw: str) -> str:
    for fmt in ("%d %b %Y", "%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y", "%d %B %Y", "%d-%b-%Y"):
        try:
            return pd.to_datetime(raw, format=fmt).strftime("%Y-%m-%d")
        except Exception:
            pass
    return pd.to_datetime(raw, dayfirst=True).strftime("%Y-%m-%d")


# ── auto-detect ───────────────────────────────────────────────────────────────

def detect_and_parse(df: pd.DataFrame, hint: str = "") -> tuple[list[dict], str]:
    all_text = " ".join(str(v) for row in df.values for v in row).lower()
    hint_lower = hint.lower()

    if "united overseas bank" in all_text or "uob" in hint_lower:
        return parse_uob(df), "UOB"
    if "dbs" in all_text or "posb" in all_text or "dbs" in hint_lower or "posb" in hint_lower:
        return parse_dbs(df), "DBS/POSB"
    if "ocbc" in all_text or "ocbc" in hint_lower:
        return parse_ocbc(df), "OCBC"

    errors = []
    for parser, name in [(parse_uob, "UOB"), (parse_dbs, "DBS/POSB"), (parse_ocbc, "OCBC")]:
        try:
            txns = parser(df)
            if txns:
                return txns, name
        except Exception as e:
            errors.append(f"{name}: {e}")

    raise ValueError(f"Could not detect bank format. Tried: {'; '.join(errors)}")
