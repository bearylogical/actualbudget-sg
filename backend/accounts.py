"""
Which Actual account does this statement belong to?

1. `extract_statement_info()` reads the header block every bank puts above the
   transactions:

     UOB card     Account Number: 4265884082432583 / Account Type: UOB ONE CARD /
                  Statement Balance: 1523.73
     UOB account  Account Number: 7613634421 / Account Type: One Account /
                  (balance from the "Available Balance" column)
     POSB/DBS     Account Details For: personal 136-51134-3 / Available Balance: SGD 1357.18

   and returns a StatementInfo with a stable `fingerprint` such as
   "UOB|credit_card|2583". Only the last 4 digits are ever kept.

2. `recommend()` ranks your open Actual accounts using, strongest first:
     remembered  you imported this fingerprint into that account before
     overlap     some of these transactions already exist in that account
     balance     the account balance equals the statement balance
     name        bank / card-vs-savings / last-4 / nickname words in the account name
   and says whether the top pick is confident enough to auto-select.

3. `remember()` stores fingerprint → account after a successful import
   (DATA_DIR/account_map.json), so next time it's instant and certain.
"""
from __future__ import annotations

import json
import os
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

import pandas as pd

DATA_DIR = Path(os.getenv("DATA_DIR", "/data"))
MAP_FILE = DATA_DIR / "account_map.json"

AUTO_MIN_SCORE = 0.6     # top suggestion must score at least this …
AUTO_MIN_MARGIN = 0.25   # … and beat the runner-up by this much to be auto-selected

CARD_WORDS = ("card", "credit", "visa", "master", "amex", "krisflyer", "one card", "cashback")
DEPOSIT_WORDS = ("saving", "savings", "current", "account", "everyday", "esaver", "one account",
                 "multiplier", "360", "checking", "debit")
BANK_ALIASES = {"UOB": ("uob",), "DBS/POSB": ("posb", "dbs"), "OCBC": ("ocbc",)}


@dataclass
class StatementInfo:
    bank: str = ""
    kind: str = "unknown"            # credit_card | deposit | unknown
    account_type: str = ""           # as printed: "UOB ONE CARD", "One Account", "personal"
    last4: str = ""
    period_start: str | None = None
    period_end: str | None = None
    balance: float | None = None     # positive = money in the account; card: amount owed
    balance_is_owed: bool = False    # card statements print what you owe
    skipped_pending: int = 0         # unposted card rows held back (see parsers._finalise)
    evidence: list[str] = field(default_factory=list)

    @property
    def fingerprint(self) -> str:
        return f"{self.bank}|{self.kind}|{self.last4 or '?'}"

    @property
    def label(self) -> str:
        kind = {"credit_card": "Credit card", "deposit": "Savings / current"}.get(self.kind, "Account")
        tail = f" •{self.last4}" if self.last4 else ""
        name = f" ({self.account_type})" if self.account_type else ""
        return f"{self.bank} {kind}{tail}{name}".strip()

    def to_dict(self) -> dict:
        return {**asdict(self), "fingerprint": self.fingerprint, "label": self.label}

    @classmethod
    def from_dict(cls, d: dict | None) -> "StatementInfo":
        d = d or {}
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in d.items() if k in known})


def _money(v) -> float | None:
    s = re.sub(r"[^\d.\-]", "", str(v or ""))
    try:
        return float(s) if s not in ("", "-", ".") else None
    except ValueError:
        return None


def _date(v) -> str | None:
    try:
        return pd.to_datetime(str(v).strip(), dayfirst=True).strftime("%Y-%m-%d")
    except Exception:
        return None


def _header_pairs(df: pd.DataFrame, rows: int = 15) -> dict[str, str]:
    """'Account Number:' → '4265…' from the first rows (label in col 0, value in the next non-empty col)."""
    out = {}
    for _, row in df.head(rows).iterrows():
        vals = [str(v).strip() for v in row.values if str(v).strip() not in ("", "nan", "None")]
        if len(vals) >= 2 and vals[0].endswith(":"):
            out[vals[0].rstrip(":").strip().lower()] = vals[1]
    return out


def extract_statement_info(df: pd.DataFrame | None, bank: str, filename: str = "",
                           transactions: list[dict] | None = None,
                           pdf_label: str | None = None) -> StatementInfo:
    info = StatementInfo(bank=bank)
    pairs = _header_pairs(df) if df is not None else {}
    fname = filename.lower()

    number = pairs.get("account number") or pairs.get("card number") or ""
    details = pairs.get("account details for") or pairs.get("card transaction details for") or ""
    if details and not number:
        # POSB/DBS: "personal 136-51134-3" → nickname + number
        m = re.match(r"^(.*?)\s*([\d-]{6,})\s*$", details)
        if m:
            info.account_type, number = m.group(1).strip(), m.group(2)
        else:
            info.account_type = details
    info.account_type = info.account_type or pairs.get("account type", "")
    digits = re.sub(r"\D", "", number)
    info.last4 = digits[-4:] if len(digits) >= 4 else ""
    if info.last4:
        info.evidence.append(f"account number ending {info.last4}")

    # card or deposit?
    type_text = f"{info.account_type} {details} {pdf_label or ''}".lower()
    if "card" in type_text or "card transaction details" in " ".join(pairs):
        info.kind = "credit_card"
    elif info.account_type or details or "account" in type_text:
        info.kind = "deposit"
    elif fname.startswith("cc_") or "card" in fname:
        info.kind = "credit_card"
    elif fname.startswith("acc_") or "transaction_history" in fname:
        info.kind = "deposit"
    if info.kind != "unknown":
        info.evidence.append(f"statement type: {info.kind.replace('_', ' ')}"
                             + (f" ({info.account_type})" if info.account_type else ""))

    # period
    period = pairs.get("statement date") or pairs.get("statement period") or ""
    m = re.match(r"(.+?)\s+to\s+(.+)", period, re.I)
    if m:
        info.period_start, info.period_end = _date(m.group(1)), _date(m.group(2))
    elif pairs.get("statement as at"):
        info.period_end = _date(pairs["statement as at"])
    if transactions and not info.period_start:
        dates = sorted(t["date"] for t in transactions if t.get("date"))
        if dates:
            info.period_start, info.period_end = dates[0], info.period_end or dates[-1]

    # balance
    if "statement balance" in pairs:
        info.balance, info.balance_is_owed = _money(pairs["statement balance"]), info.kind == "credit_card"
    elif "ledger balance" in pairs or "available balance" in pairs:
        info.balance = _money(pairs.get("ledger balance") or pairs.get("available balance"))
    elif df is not None:
        info.balance = _latest_running_balance(df)
    return info


def _latest_running_balance(df: pd.DataFrame) -> float | None:
    """UOB account exports: newest row's 'Available Balance' column."""
    for i, row in df.head(30).iterrows():
        vals = [str(v).strip().lower() for v in row.values]
        if "available balance" in vals and "transaction date" in vals:
            col = vals.index("available balance")
            dcol = vals.index("transaction date")
            best = None
            for _, r in df.iloc[i + 1:].iterrows():
                d, b = _date(r.iloc[dcol]), _money(r.iloc[col])
                if d and b is not None and (best is None or d > best[0]):
                    best = (d, b)
            return best[1] if best else None
    return None


# ── memory ────────────────────────────────────────────────────────────────────

def load_map() -> dict:
    try:
        return json.loads(MAP_FILE.read_text())
    except Exception:
        return {}


def remember(fingerprint: str, account_id: str, account_name: str = "") -> dict:
    if not fingerprint or fingerprint.endswith("|unknown|?"):
        return load_map()   # nothing identifying to remember
    data = load_map()
    prev = data.get(fingerprint, {})
    data[fingerprint] = {"account_id": account_id, "account_name": account_name,
                         "count": prev.get("count", 0) + 1 if prev.get("account_id") == account_id else 1,
                         "last_used": int(time.time())}
    MAP_FILE.parent.mkdir(parents=True, exist_ok=True)
    MAP_FILE.write_text(json.dumps(data, indent=2, sort_keys=True))
    return data


def forget(fingerprint: str) -> dict:
    data = load_map()
    data.pop(fingerprint, None)
    MAP_FILE.parent.mkdir(parents=True, exist_ok=True)
    MAP_FILE.write_text(json.dumps(data, indent=2, sort_keys=True))
    return data


def match_counts(transactions: list[dict], bridge_accounts: list[dict]) -> dict[str, dict]:
    """bridge /accounts/match rows → {account_id: {"matched": n parsed txns already there}}."""
    out = {}
    for a in bridge_accounts:
        present = set(a.get("matchedIds") or [])
        n = 0
        if present:
            for t in transactions:
                ids = {t.get("imported_id"), *(t.get("legacy_ids") or [])}
                if ids & present:
                    n += 1
        out[a["id"]] = {"matched": n}
    return out


def match_request(transactions: list[dict]) -> dict:
    ids = sorted({i for t in transactions
                  for i in [t.get("imported_id"), *(t.get("legacy_ids") or [])] if i})
    dates = sorted(t["date"] for t in transactions if t.get("date"))
    return {"ids": ids, "startDate": dates[0] if dates else None, "endDate": dates[-1] if dates else None}


def cross_account_conflict(target_id: str, transactions: list[dict],
                           matches: dict[str, dict], names: dict[str, str] | None = None) -> str | None:
    """
    Importing into the wrong account is the one duplicate the per-account checks can't
    see. If a meaningful share of these transactions already lives in ANOTHER account
    and none in the target, return a warning.
    """
    names = names or {}
    n = len(transactions)
    if not n or (matches.get(target_id) or {}).get("matched", 0):
        return None
    for acc_id, m in matches.items():
        k = m.get("matched", 0)
        if acc_id != target_id and k >= max(3, 0.2 * n):
            return (f"{k} of these {n} transactions are already in "
                    f"'{names.get(acc_id, acc_id)}' — importing here would duplicate them")
    return None


# ── recommender ───────────────────────────────────────────────────────────────

def _words(s: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", (s or "").lower()))


def recommend(info: StatementInfo, transactions: list[dict], accounts: list[dict],
              matches: dict[str, dict] | None = None, memory: dict | None = None) -> dict:
    """
    accounts: [{id, name, closed, offbudget, balance(cents)}]
    matches:  {account_id: {"matched": n}} — parsed transactions already in that account
    """
    memory = load_map() if memory is None else memory
    matches = matches or {}
    remembered = memory.get(info.fingerprint, {}).get("account_id")
    n = max(len(transactions), 1)
    bank_tokens = BANK_ALIASES.get(info.bank, (info.bank.lower(),))
    # product / nickname phrase, e.g. "one card", "one account", "personal" (bank name removed)
    type_phrase = " ".join(w for w in re.findall(r"[a-z0-9]+", info.account_type.lower())
                           if w not in ("uob", "posb", "dbs", "ocbc"))

    ranked = []
    for a in accounts:
        if a.get("closed"):
            continue
        name = (a.get("name") or "").lower()
        words = _words(name)
        score, reasons = 0.0, []

        if remembered == a["id"]:
            score += 1.0
            reasons.append(f"you imported {info.label} here before")

        m = (matches.get(a["id"]) or {}).get("matched", 0)
        if m:
            frac = m / n
            score += 0.3 + 0.5 * frac
            reasons.append(f"{m} of these transactions are already in this account")

        bal = a.get("balance")
        if info.balance is not None and bal is not None:
            expected = -round(info.balance * 100) if info.balance_is_owed else round(info.balance * 100)
            if abs(bal - expected) <= 1:
                score += 0.5
                reasons.append(f"balance matches the statement ({info.balance:,.2f})")

        bank_hit = next((t for t in bank_tokens if t in words), None)
        if bank_hit:
            score += 0.2
            reasons.append(f"name mentions {bank_hit.upper()}")
        if info.last4 and info.last4 in name:
            score += 0.4
            reasons.append(f"name contains •{info.last4}")
        if type_phrase and type_phrase in " ".join(re.findall(r"[a-z0-9]+", name)):
            score += 0.3
            reasons.append(f"name matches “{info.account_type}”")
        is_cardish = any(w in name for w in CARD_WORDS)
        is_depositish = any(w in name for w in DEPOSIT_WORDS) and not is_cardish
        if info.kind == "credit_card":
            if is_cardish:
                score += 0.2
                reasons.append("looks like a card account")
            elif is_depositish:
                score -= 0.3
                reasons.append("looks like savings, statement is a card")
        elif info.kind == "deposit":
            if is_depositish:
                score += 0.15
                reasons.append("looks like a savings/current account")
            elif is_cardish:
                score -= 0.3
                reasons.append("looks like a card, statement is savings/current")
        if a.get("offbudget"):
            score -= 0.1

        ranked.append({"account_id": a["id"], "name": a.get("name"), "score": round(score, 3),
                       "reasons": reasons, "matched": m})

    ranked.sort(key=lambda r: -r["score"])
    top = ranked[0] if ranked else None
    second = ranked[1]["score"] if len(ranked) > 1 else 0.0
    auto = bool(top and top["score"] >= AUTO_MIN_SCORE and top["score"] - second >= AUTO_MIN_MARGIN)
    return {
        "statement": info.to_dict(),
        "suggestions": ranked[:5],
        "recommended": top["account_id"] if top and top["score"] > 0 else None,
        "auto_select": auto,
        "remembered": remembered,
    }
