"""
Multi-bank statement parsers for UOB, DBS/POSB, and OCBC.

Spreadsheet exports (.xls/.xlsx) arrive as DataFrames; PDF e-statements arrive
as layout-preserved text. Each parser returns a list of standardised
transaction dicts.
"""
import re
import hashlib
from typing import Optional
import pandas as pd
from categorizer import categorize_transaction   # top-level import


def _clean_desc(raw) -> str:
    return re.sub(r'\s+', ' ', str(raw).split("\n")[0].strip())


def _clean_ref(raw) -> Optional[str]:
    """Return a non-empty, non-nan ref string or None."""
    val = str(raw).strip()
    return val if val and val not in ("nan", "NaT", "None", "") else None


# UOB packs several lines into a single description cell rather than giving the
# reference its own column: card rows carry the 23-digit acquirer reference
# ("Ref No: 7412345…"), account rows a PayNow/bank reference ("PIB1234…").
_REF_LABELLED = re.compile(r"^Ref No\.?\s*:\s*(\S+)$", re.IGNORECASE)
_REF_BARE = re.compile(r"^(?:PIB|MBK)\d{10,}$", re.IGNORECASE)

# Identifier-only lines that add nothing to a payee description.
_DESC_NOISE = (
    re.compile(r"^x{4,}\d+$", re.IGNORECASE),         # masked card number
    re.compile(r"^\d{6,}$"),                          # bare account number
    re.compile(r"^OTHR\s+\S{12,}$", re.IGNORECASE),   # opaque transfer code
)


def _desc_and_ref(raw) -> tuple[str, Optional[str]]:
    """
    Split a statement description into a readable payee and its reference.

    The reference is the reliable de-duplication key and is the same value in
    the .xls export and the PDF statement, so it is pulled out rather than
    discarded. Identifier-only lines are dropped and what remains is joined,
    which keeps the payee — often on a continuation line — available to the
    categoriser instead of leaving it as a bare "PAYNOW-FAST".
    """
    ref = None
    parts: list[str] = []
    for line in str(raw).split("\n"):
        line = re.sub(r"\s+", " ", line).strip()
        if not line or line.lower() in ("nan", "nat", "none"):
            continue

        labelled = _REF_LABELLED.match(line)
        if labelled:
            ref = ref or labelled.group(1)
            continue
        if _REF_BARE.match(line):
            ref = ref or line.upper()
            continue

        if any(p.match(line) for p in _DESC_NOISE):
            continue
        line = re.sub(r"^OTHR\s+", "", line, flags=re.IGNORECASE)
        if line and line not in parts:
            parts.append(line)

    return " - ".join(parts), _clean_ref(ref)


def _std(date: str, desc: str, amount: float, currency: str = "SGD",
         foreign_amount=None, foreign_currency=None, ref=None) -> dict:
    category, confidence = categorize_transaction(desc)
    if ref:
        imported_id = f"ref-{ref}"
    else:
        imported_id = hashlib.sha256(f"{date}|{desc}|{amount:.2f}|{currency}".encode()).hexdigest()[:16]
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
        "ref": ref,
        "imported_id": imported_id,
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

    ref_col = next((c for c in data.columns if "ref" in str(c).lower()), None)

    # Detect format: new (Withdrawal/Deposit) vs old (Transaction Amount(Local))
    cols_lower = {str(c).lower(): c for c in data.columns}
    new_format = "withdrawal" in cols_lower and "deposit" in cols_lower

    txns = []
    for _, row in data.iterrows():
        txn_date = str(row.get("Transaction Date", "")).strip()
        raw_desc = str(row.get("Transaction Description", row.get("Description", ""))).strip()
        description, embedded_ref = _desc_and_ref(raw_desc)

        if not txn_date or txn_date in ("nan", "NaT", "") or not description:
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

        ref = (_clean_ref(row.get(ref_col)) if ref_col else None) or embedded_ref
        txns.append(_std(date_str, description, amount, currency, foreign_amount, foreign_currency, ref=ref))
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


# ── UOB PDF (credit card e-statement) ─────────────────────────────────────────

_MONTHS = "JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC"
_AMOUNT = r"\d{1,3}(?:,\d{3})*\.\d{2}"

# "07 MAY   03 MAY   BUS/MRT 846451010 SINGAPORE            2.36"
# Trailing "CR" marks a credit (payment / refund / rebate).
_PDF_TXN = re.compile(
    rf"^\s*(?P<post>\d{{1,2}}\s+(?:{_MONTHS}))"
    rf"\s+(?P<trans>\d{{1,2}}\s+(?:{_MONTHS}))"
    rf"\s+(?P<desc>\S.*?)\s\s+(?P<amount>{_AMOUNT})(?:\s+(?P<cr>CR))?\s*$",
    re.IGNORECASE,
)
_PDF_REF = re.compile(r"^\s*Ref No\.?\s*:\s*(\S+)\s*$", re.IGNORECASE)
_PDF_FOREIGN = re.compile(rf"^\s*(?P<ccy>[A-Z]{{3}})\s+(?P<amount>{_AMOUNT})\s*$")
_PDF_STMT_DATE = re.compile(
    rf"Statement\s+Date\s+(\d{{1,2}}\s+(?:{_MONTHS})\s+\d{{4}})", re.IGNORECASE
)
_PDF_TXN_END = re.compile(r"End of Transaction Details", re.IGNORECASE)


def _pdf_normalise(text: str) -> str:
    """Drop NUL/control bytes that PDF text extraction can emit."""
    return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", text)


def _pdf_statement_date(text: str) -> pd.Timestamp:
    """Statement date anchors the year, which line items omit."""
    m = _PDF_STMT_DATE.search(text)
    if m:
        try:
            return pd.to_datetime(re.sub(r"\s+", " ", m.group(1)), format="%d %b %Y")
        except Exception:
            pass
    return pd.Timestamp.today().normalize()


def _pdf_date(day_month: str, stmt_date: pd.Timestamp) -> str:
    """
    Resolve a bare "09 MAY" against the statement date.

    A statement covers roughly one month back, so a day/month that lands after
    the statement date belongs to the previous year (Dec entries on a Jan
    statement). A week of slack absorbs post dates on the statement date itself.
    """
    day_month = re.sub(r"\s+", " ", day_month).strip().upper()
    for year in (stmt_date.year, stmt_date.year - 1):
        try:
            dt = pd.to_datetime(f"{day_month} {year}", format="%d %b %Y")
        except Exception:
            continue          # e.g. 29 FEB against a non-leap year
        if dt <= stmt_date + pd.Timedelta(days=7):
            return dt.strftime("%Y-%m-%d")
    raise ValueError(f"unparseable date: {day_month}")


def parse_uob_pdf(text: str) -> list[dict]:
    """
    Parse a UOB credit card PDF e-statement from layout-preserved text.

    Each transaction is a dated line optionally followed by continuation lines:

        11 MAY   09 MAY   EXAMPLE STORE SINGAPORE             12.00
                          Ref No. : 74123456789012345678901
                          USD 38.85          ← foreign amount, when applicable

    Summary rows (PREVIOUS BALANCE, SUB TOTAL, TOTAL BALANCE) carry no dates and
    are skipped by the line pattern. Multi-card statements simply concatenate.
    """
    text = _pdf_normalise(text)
    stmt_date = _pdf_statement_date(text)

    txns: list[dict] = []
    pending: Optional[dict] = None      # transaction awaiting its continuation lines

    def flush():
        nonlocal pending
        if pending is None:
            return
        txns.append(_std(
            pending["date"],
            pending["desc"],
            pending["amount"],
            foreign_amount=pending["foreign_amount"],
            foreign_currency=pending["foreign_currency"],
            ref=pending["ref"],
        ))
        pending = None

    for line in text.split("\n"):
        if _PDF_TXN_END.search(line):
            break

        m = _PDF_TXN.match(line)
        if m:
            flush()
            try:
                date_str = _pdf_date(m.group("trans"), stmt_date)
            except Exception:
                continue
            amount = float(m.group("amount").replace(",", ""))
            if m.group("cr"):
                amount = -amount          # credits are negative, as in the .xls parsers
            pending = {
                "date": date_str,
                "desc": _clean_desc(m.group("desc")),
                "amount": amount,
                "ref": None,
                "foreign_amount": None,
                "foreign_currency": None,
            }
            continue

        if pending is None:
            continue

        ref = _PDF_REF.match(line)
        if ref:
            pending["ref"] = _clean_ref(ref.group(1))
            continue

        foreign = _PDF_FOREIGN.match(line)
        if foreign and foreign.group("ccy").upper() != "SGD":
            pending["foreign_currency"] = foreign.group("ccy").upper()
            pending["foreign_amount"] = float(foreign.group("amount").replace(",", ""))

    flush()
    if not txns:
        raise ValueError("UOB PDF: no transactions found")
    return txns


# ── UOB account (savings / current) PDF statement ─────────────────────────────

_PDF_ACCT_COLUMNS = ("Withdrawals", "Deposits", "Balance")
_PDF_ACCT_HEADER = re.compile(r"Withdrawals\s+Deposits", re.IGNORECASE)
_PDF_PERIOD = re.compile(
    r"Period:\s*\d{1,2}\s+[A-Za-z]{3,9}\s+\d{4}\s+to\s+(\d{1,2}\s+[A-Za-z]{3,9}\s+\d{4})",
    re.IGNORECASE,
)
_PDF_ACCT_ROW = re.compile(rf"^\s*(?P<date>\d{{1,2}}\s+(?:{_MONTHS}))(?=\s\s)", re.IGNORECASE)
_PDF_ACCT_SKIP = re.compile(r"^(?:BALANCE\s+[BC]/F|Total)\b", re.IGNORECASE)


def _pdf_period_end(text: str) -> pd.Timestamp:
    """Account statements date their range as a period rather than a single date."""
    m = _PDF_PERIOD.search(text)
    if m:
        for fmt in ("%d %b %Y", "%d %B %Y"):
            try:
                return pd.to_datetime(re.sub(r"\s+", " ", m.group(1)), format=fmt)
            except Exception:
                pass
    return _pdf_statement_date(text)


def parse_uob_account_pdf(text: str) -> list[dict]:
    """
    Parse a UOB account (One Account / savings / current) PDF statement.

    Unlike the card statement there is one date column, and debit vs credit is
    decided by which column an amount sits under — so amounts are matched to
    the Withdrawals / Deposits / Balance headers by their right edge. The
    running Balance column is read and discarded; taking the last number on the
    line would silently import balances as transaction amounts.

        Date      Description        Withdrawals    Deposits     Balance
        02 May    PAYNOW-FAST               2.40                1,234.56
                    PIB1234567890123456
                    EXAMPLE PAY PTE. LTD.
    """
    text = _pdf_normalise(text)
    lines = [line.rstrip() for line in text.split("\n")]

    header = next((line for line in lines if _PDF_ACCT_HEADER.search(line)), None)
    if header is None:
        raise ValueError("UOB account PDF: could not find the transaction table header")

    # Right edge of each column heading; amounts are right-aligned beneath them.
    columns: dict[str, int] = {}
    for name in _PDF_ACCT_COLUMNS:
        m = re.search(name, header, re.IGNORECASE)
        if m:
            columns[name] = m.end()
    if "Withdrawals" not in columns or "Deposits" not in columns:
        raise ValueError("UOB account PDF: missing Withdrawals/Deposits columns")

    period_end = _pdf_period_end(text)

    txns: list[dict] = []
    pending: Optional[dict] = None
    started = False

    def flush():
        nonlocal pending
        if pending is None:
            return
        entry, pending = pending, None
        if entry["withdrawal"] is None and entry["deposit"] is None:
            return                                    # opening balance row
        desc, ref = _desc_and_ref("\n".join(entry["lines"]))
        if not desc:
            return
        amount = -entry["deposit"] if entry["deposit"] is not None else entry["withdrawal"]
        txns.append(_std(entry["date"], desc, amount, ref=ref))

    for line in lines:
        if _PDF_TXN_END.search(line):
            break
        if _PDF_ACCT_HEADER.search(line):
            flush()
            started = True
            continue
        if not started or not line.strip():
            continue

        m = _PDF_ACCT_ROW.match(line)
        if m:
            flush()

            amounts: dict[str, float] = {}
            desc_end = len(line)
            for found in re.finditer(_AMOUNT, line):
                column = min(columns, key=lambda c: abs(columns[c] - found.end()))
                amounts[column] = float(found.group().replace(",", ""))
                desc_end = min(desc_end, found.start())

            desc = re.sub(r"\s+", " ", line[m.end("date"):desc_end]).strip()
            if _PDF_ACCT_SKIP.match(desc):
                continue
            try:
                date_str = _pdf_date(m.group("date"), period_end)
            except Exception:
                continue
            pending = {
                "date": date_str,
                "lines": [desc],
                "withdrawal": amounts.get("Withdrawals"),
                "deposit": amounts.get("Deposits"),
            }
            continue

        if pending is not None:
            stripped = line.strip()
            if _PDF_ACCT_SKIP.match(stripped):
                flush()
                continue
            pending["lines"].append(stripped)

    flush()
    if not txns:
        raise ValueError("UOB account PDF: no transactions found")
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


def detect_and_parse_pdf(text: str, hint: str = "") -> tuple[list[dict], str]:
    """Detect the issuing bank of a PDF e-statement and parse it."""
    normalised = _pdf_normalise(text)
    haystack = f"{normalised}\n{hint}".lower()

    if "united overseas bank" in haystack or "uob" in haystack:
        # Account statements have a Withdrawals/Deposits table; card statements
        # a Post/Trans date pair. Try the likelier one first, then fall back.
        card = (parse_uob_pdf, "UOB Credit Card")
        account = (parse_uob_account_pdf, "UOB Account")
        order = [account, card] if _PDF_ACCT_HEADER.search(normalised) else [card, account]

        errors = []
        for parser, label in order:
            try:
                return parser(normalised), label
            except ValueError as e:
                errors.append(str(e))
        raise ValueError(
            f"Recognised a UOB PDF but could not read it ({'; '.join(errors)})."
        )

    raise ValueError(
        "Could not detect the bank for this PDF. Supported PDF formats: "
        "UOB credit card and UOB account statements."
    )
