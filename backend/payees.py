"""
Payee normalisation: turn raw statement text into a stable, human payee name.

  "SHENG SIONG SUPERMARKET SINGAPORE SG"   -> "Sheng Siong"            (merchant table)
  "TST* MOTHER DOUGH BAKERY SINGAPORE SG"  -> "Mother Dough Bakery"    (heuristic)
  "PAYPAL *NINTENDO 4029357733 LU"         -> "Nintendo"               (merchant table)
  "ZHANG LIANG MALATANG  SINGAPORE SG"     -> "Zhang Liang Malatang"   (heuristic)

Why it matters: Actual learns categories *per payee*. With raw descriptions every
Grab ride is a brand-new payee, so learned rules never fire again.
"""
from __future__ import annotations

import re

from merchants import match_merchant

_PREFIXES = re.compile(
    r"^(?:(?:pos|nets|visa|mastercard|master|mst|dcr?|debit card transaction|point-of-sale transaction|"
    r"purchase|pymt|ipay|contactless|apple pay|google pay|samsung pay)\b[\s:.\-]*)+",
    re.I,
)
# payment-processor prefixes: "SQ *", "TST*", "PAYPAL *", "SP *", "ZTL*", "STRIPE*"
# payment-processor / aggregator prefixes: "SQ *", "TST*", "PAYPAL *", "SMP**", "EzypaySGD*", "GRB*"
_PROCESSOR = re.compile(r"^[a-z0-9]{2,12}\s?\*{1,2}\s*(?=\S)", re.I)
_TRAILING_LOCATION = re.compile(
    r"(?:\s+(?:singapore|s'pore|sgp|sin))?(?:\s+(?:sg|sgp|sgd))?\s*$", re.I
)
_NOISE = [
    re.compile(r"\bx{2,}[\dx-]*\b", re.I),                  # masked card numbers
    re.compile(r"\b\d{4}[- ]?\d{2,4}[- ]?\d{2,4}[- ]?\d{0,4}\b"),  # long ref numbers
    re.compile(r"\b\d{5,}\b"),                               # standalone long digits
    re.compile(r"\b\d{1,2}/\d{1,2}(?:/\d{2,4})?\b"),         # dates
    re.compile(r"\bref(?:erence)?\s*(?:no\.?|#)?\s*\S+", re.I),
    re.compile(r"#\S+"),
    re.compile(r"\b(?:pte\.?\s*ltd\.?|pte|ltd\.?|llc|inc\.?|co\.?\s*ltd\.?|sdn\.?\s*bhd\.?)\b", re.I),
]
_KEEP_UPPER = {"kfc", "mcd", "hdb", "ntuc", "sp", "aws", "ikea", "gv", "sia", "iras", "lta",
               "nus", "ntu", "smu", "cpf", "srs", "atm", "bbq", "dbs", "uob", "ocbc", "posb", "m1",
               "spc", "twp", "bjb", "cdp", "esso", "sgd", "usd"}


def _title(s: str) -> str:
    words = []
    for w in s.split():
        lw = w.lower()
        if lw in _KEEP_UPPER:
            words.append(lw.upper())
        elif any(ch.isdigit() for ch in w) and len(w) <= 4:
            words.append(w.upper())
        else:
            words.append(w[:1].upper() + w[1:].lower())
    return " ".join(words)


def clean_description(raw: str) -> str:
    """Heuristic cleanup, no merchant lookup."""
    s = re.sub(r"\s+", " ", str(raw or "").split("\n")[0]).strip()
    if not s:
        return ""
    original = s
    s = _PREFIXES.sub("", s)
    s = _PROCESSOR.sub("", s)
    for rx in _NOISE:
        s = rx.sub(" ", s)
    s = re.sub(r"\s+", " ", s).strip(" -*.,:/")
    s = _TRAILING_LOCATION.sub("", s).strip(" -*.,:/")
    # branch suffixes: "JOLLIBEE@WNC", "SPC - TAMPINES AVE 4", "YA KUN KAYA TOAST - T3"
    stem = re.sub(r"\s*@.*$|\s+-\s+.*$", "", s).strip(" -*.,:/")
    if len(stem) >= 3:
        s = stem
    # foreign merchants end with an ISO country code: "NINTENDO LU", "AMAZON.CO.UK GB"
    if len(s.split()) > 1:
        s = re.sub(r"\s+[A-Z]{2}$", "", s)
    s = re.sub(r"\s+", " ", s).strip(" -*.,:/")
    if len(s) < 2:
        s = original
    return _title(s)[:60]


def normalize_payee(raw: str, is_credit: bool | None = None) -> str:
    m = match_merchant(raw, is_credit)
    if m and m.payee:
        return m.payee
    return clean_description(raw)


def looks_raw(name: str) -> bool:
    """Heuristic: does an existing Actual payee name look like unnormalised statement text?"""
    if not name:
        return False
    letters = [c for c in name if c.isalpha()]
    mostly_upper = letters and sum(c.isupper() for c in letters) / len(letters) > 0.8 and len(letters) > 4
    return bool(
        mostly_upper
        or re.search(r"\b(singapore|sgp?)\s*(sg)?$", name, re.I)
        or re.search(r"\d{5,}|\*|x{3,}", name, re.I)
    )
