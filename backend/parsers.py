"""
Multi-bank statement parsers for UOB, DBS/POSB, and OCBC.
Each parser returns a list of standardised transaction dicts.

Loading (xls / xlsx / csv, sniffed by content not extension) lives in
`load_table()` so the web backend and the scheduler share one code path.
"""
import csv
import hashlib
import io
import os
import re
import subprocess
import tempfile
from typing import Optional

import pandas as pd

from categorizer import categorize   # top-level import


# ── loading ───────────────────────────────────────────────────────────────────

XLS_MAGIC = b"\xd0\xcf\x11\xe0"   # OLE2 (legacy .xls) — UOB exports these, sometimes named .csv
XLSX_MAGIC = b"PK\x03\x04"


def sniff_format(content: bytes, filename: str = "") -> str:
    if content[:4] == XLS_MAGIC:
        return "xls"
    if content[:4] == XLSX_MAGIC:
        return "xlsx"
    head = content[:2048].lstrip(b"\xef\xbb\xbf").lower()
    if b"<html" in head or b"<table" in head:
        return "html"   # some banks' "xls" is really an HTML table
    return "csv"


def _read_xls(content: bytes) -> pd.DataFrame:
    try:
        return pd.read_excel(io.BytesIO(content), header=None, engine="xlrd")
    except Exception:
        # fallback: convert with LibreOffice
        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, "in.xls")
            with open(src, "wb") as f:
                f.write(content)
            subprocess.run(["libreoffice", "--headless", "--convert-to", "xlsx", src, "--outdir", d],
                           capture_output=True, check=True)
            return pd.read_excel(os.path.join(d, "in.xlsx"), header=None)


def _read_csv(content: bytes) -> pd.DataFrame:
    text = None
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            text = content.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    rows = list(csv.reader(io.StringIO(text or "")))
    width = max((len(r) for r in rows), default=0)
    return pd.DataFrame([r + [""] * (width - len(r)) for r in rows])


def load_table(content: bytes, filename: str = "") -> pd.DataFrame:
    fmt = sniff_format(content, filename)
    if fmt == "xls":
        return _read_xls(content)
    if fmt == "xlsx":
        return pd.read_excel(io.BytesIO(content), header=None)
    if fmt == "html":
        return pd.read_html(io.BytesIO(content), header=None)[0]
    return _read_csv(content)


SUPPORTED_EXTENSIONS = (".xls", ".xlsx", ".csv")


# ── helpers ───────────────────────────────────────────────────────────────────

def _clean_desc(raw) -> str:
    return re.sub(r'\s+', ' ', str(raw).split("\n")[0].strip())


def _clean_ref(raw) -> Optional[str]:
    """Return a non-empty, non-nan ref string or None."""
    val = str(raw).strip()
    return val if val and val not in ("nan", "NaT", "None", "") else None


def _hash_id(date: str, desc: str, amount: float, currency: str) -> str:
    return hashlib.sha256(f"{date}|{desc}|{amount:.2f}|{currency}".encode()).hexdigest()[:16]


def _std(date: str, desc: str, amount: float, currency: str = "SGD",
         foreign_amount=None, foreign_currency=None, ref=None,
         merchant: Optional[str] = None, legacy_ids: Optional[list] = None) -> dict:
    """amount > 0 = money out (debit), amount < 0 = money in (credit)."""
    cat = categorize(desc, is_credit=amount < 0, payee_hint=merchant)
    hash_id = _hash_id(date, desc, amount, currency)
    imported_id = f"ref-{ref}" if ref else hash_id
    legacy = [i for i in (legacy_ids or []) if i and i != imported_id]
    if ref and hash_id not in legacy:
        legacy.append(hash_id)   # matches rows imported before refs were extracted
    return {
        "date": date,
        "description": desc,
        "merchant": merchant,
        "amount": abs(amount),
        "currency": currency,
        "is_credit": amount < 0,
        "category": cat["category"],
        "confidence": cat["confidence"],
        "payee": cat["payee"],
        "kind": cat["kind"],
        "source": cat["source"],
        "foreign_amount": float(foreign_amount) if foreign_amount is not None and pd.notna(foreign_amount) else None,
        "foreign_currency": foreign_currency if foreign_currency and str(foreign_currency) not in ("nan", "") else None,
        "ref": ref,
        "imported_id": imported_id,
        "legacy_ids": legacy,
    }


# ── UOB ───────────────────────────────────────────────────────────────────────

_UOB_REF = re.compile(r"Ref\s*No\.?\s*:?\s*([A-Za-z0-9]+)", re.I)


def _uob_merchant(line1: str) -> Optional[str]:
    """UOB card lines are fixed-width: 25-char merchant, city, country code."""
    line1 = line1.rstrip()
    if len(line1) >= 30 and re.search(r"\s[A-Z]{2,3}$", line1):
        return line1[:25].strip() or None
    return None


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

    ref_col = next((c for c in data.columns if "ref" in str(c).lower()), None)

    # Detect format: new (Withdrawal/Deposit) vs old (Transaction Amount(Local))
    cols_lower = {str(c).lower(): c for c in data.columns}
    new_format = "withdrawal" in cols_lower and "deposit" in cols_lower

    txns = []
    for _, row in data.iterrows():
        txn_date = str(row.get("Transaction Date", "")).strip()
        description = str(row.get("Transaction Description", row.get("Description", ""))).strip()

        if not txn_date or txn_date in ("nan", "NaT", "") or not description or description == "nan":
            continue

        if new_format:
            withdrawal = _to_float(row.get(cols_lower.get("withdrawal")))
            deposit = _to_float(row.get(cols_lower.get("deposit")))
            if withdrawal is None and deposit is None:
                continue
            amount = -(deposit or 0) if deposit else (withdrawal or 0)
            currency = "SGD"
            foreign_amount = None
            foreign_currency = None
        else:
            amount_raw = row.get("Transaction Amount(Local)", None)
            try:
                amount = float(amount_raw)
            except (TypeError, ValueError):
                continue
            currency = str(row.get("Local Currency Type", "SGD")).strip()
            foreign_amount = row.get("Transaction Amount(Foreign)", None)
            foreign_currency = str(row.get("Foreign Currency Type", "")).strip()

        try:
            date_str = pd.to_datetime(txn_date, format="%d %b %Y").strftime("%Y-%m-%d")
        except Exception:
            continue

        # Card exports put the bank reference on line 2: "…SG\nRef No: 7477…"
        ref = _clean_ref(row.get(ref_col)) if ref_col else None
        if not ref:
            m = _UOB_REF.search(description)
            ref = m.group(1) if m else None
        line1 = description.split("\n")[0]
        txns.append(_std(date_str, _clean_desc(description), amount, currency,
                         foreign_amount, foreign_currency, ref=ref, merchant=_uob_merchant(line1)))
    return txns


# ── DBS / POSB ────────────────────────────────────────────────────────────────

# Transaction codes in DBS/POSB CSV exports
_DBS_CODE_HINT = {
    "INT": "INTEREST CREDIT",
    "SC": "SERVICE CHARGE",
    "CDP": "CDP",
}
_DATE_SUFFIX = re.compile(r"\s+\d{2}[A-Z]{3}$")   # "… SGP 07AUG"


def _dbs_merchant(code: str, r1: str, r2: str, r3: str) -> Optional[str]:
    code = code.upper()
    if code in ("MST", "POS", "NETS", "UMC", "VISA"):
        return _DATE_SUFFIX.sub("", r1).strip() or None
    m = re.match(r"^\s*(?:to|from)\s*:\s*(.+)$", r2, re.I)
    if m:
        return m.group(1).strip()
    m = re.match(r"^\s*ib\s*:\s*(.+)$", r2, re.I)
    if m:
        return m.group(1).strip()
    if code == "IBG" and r1:
        return r1
    return None


def parse_dbs_v2(df: pd.DataFrame, header_row: int) -> list[dict]:
    """Current digibank CSV: Transaction Date, Transaction Code, Description,
    Transaction Ref1-3, Status, Debit Amount, Credit Amount."""
    df = df.copy()
    df.columns = [str(c).strip() for c in df.iloc[header_row]]
    data = df.iloc[header_row + 1:].reset_index(drop=True)
    cols = {c.lower(): c for c in data.columns}

    def g(row, name):
        v = row.get(cols.get(name, ""), "")
        return "" if v is None or str(v).strip().lower() in ("nan", "none") else str(v).strip()

    txns = []
    for _, row in data.iterrows():
        raw_date = g(row, "transaction date")
        if not raw_date:
            continue
        status = g(row, "status")
        if status and status.lower() not in ("settled", "posted", "completed"):
            continue   # pending rows re-appear with final details later
        code = g(row, "transaction code")
        desc_col = g(row, "description")
        r1, r2, r3 = g(row, "transaction ref1"), g(row, "transaction ref2"), g(row, "transaction ref3")
        debit, credit = _to_float(g(row, "debit amount")), _to_float(g(row, "credit amount"))
        if debit is None and credit is None:
            continue
        amount = -(credit or 0) if credit else (debit or 0)
        try:
            date_str = _parse_date(raw_date)
        except Exception:
            continue

        desc = re.sub(r"\s+", " ", desc_col).strip()
        hint = _DBS_CODE_HINT.get(code.upper())
        if hint and hint.lower() not in desc.lower():
            desc = f"{hint} {desc}".strip()
        if not desc:
            desc = code or "UNKNOWN"

        # refs: Ref1 alone isn't unique (card rows = merchant + date); use all three + code
        ref_key = "|".join([code, r1, r2, r3])
        ref = hashlib.sha1(ref_key.encode()).hexdigest()[:20] if (r1 or r2 or r3) else None
        legacy = []
        if r1:
            legacy.append(f"ref-{r1}")                                      # old parser: Ref1 as ref
        legacy.append(_hash_id(date_str, _clean_desc(desc_col), amount, "SGD"))  # old hash
        txns.append(_std(date_str, desc, amount, ref=ref,
                         merchant=_dbs_merchant(code, r1, r2, r3), legacy_ids=legacy))
    return txns


def parse_dbs(df: pd.DataFrame) -> list[dict]:
    header_row = None
    for i, row in df.iterrows():
        vals = [str(v).strip().lower() for v in row.values]
        if any("transaction date" in v or (v == "date" and "debit" in " ".join(vals)) for v in vals):
            header_row = i
            break
    if header_row is None:
        raise ValueError("DBS: could not find transaction header row")

    header_vals = [str(v).strip().lower() for v in df.iloc[header_row].values]
    if "transaction ref1" in header_vals and "transaction code" in header_vals:
        return parse_dbs_v2(df, header_row)

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
        if "ref" in cl and col_map.get("desc") != col:
            col_map.setdefault("ref", col)

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
        ref = _clean_ref(row.get(col_map["ref"])) if "ref" in col_map else None
        txns.append(_std(date_str, desc, amount, ref=ref))
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
        if "ref" in cl and col_map.get("desc") != col:
            col_map.setdefault("ref", col)

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
        ref = _clean_ref(row.get(col_map["ref"])) if "ref" in col_map else None
        txns.append(_std(date_str, desc, amount, ref=ref))
    return txns


# ── utilities ─────────────────────────────────────────────────────────────────

def _to_float(val) -> Optional[float]:
    if val is None:
        return None
    s = str(val).replace(",", "").strip()
    if not s or s.lower() in ("nan", "none"):
        return None
    try:
        return float(s)
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
    all_text = " ".join(str(v) for row in df.head(15).values for v in row).lower()
    hint_lower = hint.lower()
    header_text = " ".join(str(v) for row in df.head(30).values for v in row).lower()

    if "united overseas bank" in all_text or "uob" in hint_lower:
        return parse_uob(df), "UOB"
    if ("dbs" in all_text or "posb" in all_text or "dbs" in hint_lower or "posb" in hint_lower
            or ("transaction ref1" in header_text and "transaction code" in header_text)):
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


def parse_bytes(content: bytes, filename: str = "") -> tuple[list[dict], str]:
    return detect_and_parse(load_table(content, filename), hint=filename)
