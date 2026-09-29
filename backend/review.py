"""
Review queue — nothing that looks like a duplicate or a transfer is changed in Actual
until you approve it. An LLM can advise on each item; your decisions are remembered.

Item kinds and the decisions they accept:

  import_duplicate    a statement row held back at import because the same date+amount
                      is already in the account under another id
                        skip   → it IS the same transaction, don't import
                        import → it's a different transaction, import it
  existing_duplicate  two rows already in Actual that look like the same transaction
                        delete_a / delete_b → delete that copy
                        keep_both
  transfer_pair       money out of one account and into another (card bill payment,
                      own-account transfer) imported as two unrelated rows
                        link           → make it one Actual transfer
                        keep_separate
  recategorize        a better category for a row already in Actual (recategorize.py)
                        apply → set the proposed category (or one you pick instead)
                        keep  → leave it as it is

Learning (table `memory`):
  * pair memory     — the exact pair is never asked again
  * pattern memory  — after LEARN_AFTER identical decisions for the same pattern
                      (e.g. "same-day repeats at Encik Tan are real",
                      "UOB One Account → UOB One Card card payments are transfers")
                      new items matching it resolve automatically, marked as learned.
                      Forget a pattern any time from the UI / API.

Storage: SQLite in DATA_DIR (shared by backend and scheduler).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import time
from contextlib import contextmanager
from datetime import date as _date
from pathlib import Path

DATA_DIR = Path(os.getenv("DATA_DIR", "/data"))
DB_FILE = DATA_DIR / "review.db"
LEARN_AFTER = int(os.getenv("REVIEW_LEARN_AFTER", "2"))

DECISIONS = {
    "import_duplicate": {"skip", "import"},
    "existing_duplicate": {"delete_a", "delete_b", "keep_both"},
    "transfer_pair": {"link", "keep_separate"},
    "reconcile_fix": {"apply", "reject"},
    "recategorize": {"apply", "keep"},
}
# which decision means "yes, these are the same money movement"
POSITIVE = {"import_duplicate": "skip", "existing_duplicate": None, "transfer_pair": "link"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
  id TEXT PRIMARY KEY, kind TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
  pattern TEXT, payload TEXT NOT NULL, llm TEXT, decision TEXT, decided_by TEXT,
  result TEXT, created REAL NOT NULL, updated REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS items_status ON items(status);
CREATE TABLE IF NOT EXISTS memory (
  key TEXT PRIMARY KEY, kind TEXT NOT NULL, decision TEXT NOT NULL,
  count INTEGER NOT NULL DEFAULT 1, label TEXT, updated REAL NOT NULL
);
"""


@contextmanager
def db():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_FILE, timeout=15)
    con.row_factory = sqlite3.Row
    try:
        con.executescript(SCHEMA)
        yield con
        con.commit()
    finally:
        con.close()


# ── keys & patterns ───────────────────────────────────────────────────────────

def _norm(s: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def item_id(kind: str, *parts) -> str:
    return hashlib.sha1("|".join([kind, *sorted(str(p) for p in parts)]).encode()).hexdigest()[:16]


def pattern_for(kind: str, payload: dict) -> tuple[str, str]:
    """(pattern key, human label) used for learning."""
    if kind == "transfer_pair":
        a, b = payload["a"], payload["b"]
        src, dst = (a, b) if a["amount"] < 0 else (b, a)
        return (f"transfer|{src['account']}|{dst['account']}|{payload.get('reason_code', '')}",
                f"{src.get('account_name')} → {dst.get('account_name')} ({payload.get('reason_code', 'match')})")
    if kind == "import_duplicate":
        t = payload["incoming"]
        return (f"importdup|{payload.get('account')}|{_norm(t.get('payee'))}",
                f"re-imported rows for “{t.get('payee')}”")
    if kind == "recategorize":
        t, new = payload["txn"], payload["proposed"]
        who = t.get("payee") or t.get("imported_payee")
        return (f"recat|{_norm(who)}|{new['id']}", f"“{who}” → {new.get('name')}")
    if kind == "reconcile_fix":
        return (f"reconcile|{payload.get('account')}|{payload.get('fix_type', '')}",
                f"{payload.get('fix_type', 'fix')} on {payload.get('account_name', 'account')}")
    a = payload["a"]
    return (f"existingdup|{a['account']}|{_norm(a.get('payee'))}",
            f"same-day repeats at “{a.get('payee')}”")


# ── queue ─────────────────────────────────────────────────────────────────────

def enqueue(kind: str, payload: dict, key_parts: list) -> dict:
    """Add an item unless it's already known. Applies learned memory. Returns the item."""
    iid = item_id(kind, *key_parts)
    pattern, label = pattern_for(kind, payload)
    now = time.time()
    with db() as con:
        row = con.execute("SELECT * FROM items WHERE id=?", (iid,)).fetchone()
        if row and row["status"] != "superseded":
            return _row(row)
        remembered = con.execute("SELECT * FROM memory WHERE key=?", (f"pair|{iid}",)).fetchone()
        learned = con.execute("SELECT * FROM memory WHERE key=? AND count>=?", (pattern, LEARN_AFTER)).fetchone()
        status, decision, by = "pending", None, None
        if remembered:
            status, decision, by = "auto", remembered["decision"], "remembered"
        elif learned:
            status, decision, by = "auto", learned["decision"], f"learned: {learned['label']} ×{learned['count']}"
        if row:    # superseded earlier (e.g. a refresh flip-flopped) and now proposed again
            con.execute("UPDATE items SET status=?, pattern=?, payload=?, decision=?, decided_by=?, "
                        "result=NULL, updated=? WHERE id=?",
                        (status, pattern, json.dumps(payload, default=str), decision, by, now, iid))
        else:
            con.execute("INSERT INTO items(id,kind,status,pattern,payload,decision,decided_by,created,updated) "
                        "VALUES(?,?,?,?,?,?,?,?,?)",
                        (iid, kind, status, pattern, json.dumps(payload, default=str), decision, by, now, now))
        return _row(con.execute("SELECT * FROM items WHERE id=?", (iid,)).fetchone())


def _row(r: sqlite3.Row) -> dict:
    d = dict(r)
    for k in ("payload", "llm", "result"):
        d[k] = json.loads(d[k]) if d.get(k) else None
    return d


def list_items(status: str | None = "pending", kind: str | None = None, limit: int = 500) -> list[dict]:
    q, args = "SELECT * FROM items WHERE 1=1", []
    if status:
        q += " AND status=?"
        args.append(status)
    if kind:
        q += " AND kind=?"
        args.append(kind)
    q += " ORDER BY created DESC LIMIT ?"
    args.append(limit)
    with db() as con:
        return [_row(r) for r in con.execute(q, args)]


def get(iid: str) -> dict | None:
    with db() as con:
        r = con.execute("SELECT * FROM items WHERE id=?", (iid,)).fetchone()
        return _row(r) if r else None


def counts() -> dict:
    with db() as con:
        rows = con.execute("SELECT kind, status, COUNT(*) n FROM items GROUP BY kind, status").fetchall()
    out: dict = {"pending": 0}
    for r in rows:
        out.setdefault(r["kind"], {})[r["status"]] = r["n"]
        if r["status"] == "pending":
            out["pending"] += r["n"]
    return out


def set_payload(iid: str, payload: dict):
    with db() as con:
        con.execute("UPDATE items SET payload=?, updated=? WHERE id=?",
                    (json.dumps(payload, default=str), time.time(), iid))


def set_llm(iid: str, verdict: dict):
    with db() as con:
        con.execute("UPDATE items SET llm=?, updated=? WHERE id=?", (json.dumps(verdict), time.time(), iid))


def decide(iid: str, decision: str, by: str = "you") -> dict:
    """Record a human decision (does NOT touch Actual — see apply())."""
    item = get(iid)
    if not item:
        raise KeyError(iid)
    if decision not in DECISIONS[item["kind"]]:
        raise ValueError(f"{decision!r} is not valid for {item['kind']}: {sorted(DECISIONS[item['kind']])}")
    now = time.time()
    with db() as con:
        con.execute("UPDATE items SET status='approved', decision=?, decided_by=?, updated=? WHERE id=?",
                    (decision, by, now, iid))
        if by == "you":
            _learn(con, item, decision, now)
    return get(iid)


def _learn(con, item: dict, decision: str, now: float):
    con.execute("INSERT INTO memory(key,kind,decision,count,label,updated) VALUES(?,?,?,1,?,?) "
                "ON CONFLICT(key) DO UPDATE SET decision=excluded.decision, updated=excluded.updated",
                (f"pair|{item['id']}", item["kind"], decision, "this exact pair", now))
    # deletions and reconciliation fixes are one-offs; never generalise them into a pattern
    if decision in ("delete_a", "delete_b") or item["kind"] == "reconcile_fix":
        return
    pattern, label = pattern_for(item["kind"], item["payload"])
    row = con.execute("SELECT decision, count FROM memory WHERE key=?", (pattern,)).fetchone()
    if row and row["decision"] == decision:
        con.execute("UPDATE memory SET count=count+1, updated=? WHERE key=?", (now, pattern))
    else:   # first time, or you changed your mind → start counting again
        con.execute("INSERT INTO memory(key,kind,decision,count,label,updated) VALUES(?,?,?,1,?,?) "
                    "ON CONFLICT(key) DO UPDATE SET decision=excluded.decision, count=1, "
                    "label=excluded.label, updated=excluded.updated",
                    (pattern, item["kind"], decision, label, now))


def mark_done(iid: str, result: dict, ok: bool = True):
    with db() as con:
        con.execute("UPDATE items SET status=?, result=?, updated=? WHERE id=?",
                    ("done" if ok else "failed", json.dumps(result, default=str), time.time(), iid))


def supersede(ids: set[str], except_id: str | None = None) -> int:
    """Pending items that reference transactions just deleted/linked are no longer valid."""
    if not ids:
        return 0
    n = 0
    with db() as con:
        for r in con.execute("SELECT id, payload FROM items WHERE status='pending'").fetchall():
            if r["id"] == except_id:
                continue
            p = json.loads(r["payload"])
            refs = {p.get(k, {}).get("id") for k in ("a", "b", "txn") if isinstance(p.get(k), dict)}
            for act in p.get("actions") or []:
                refs.update(act.get("ids") or [])
                refs.update(x for x in (act.get("keep_id"), act.get("other_id")) if x)
            if refs & ids:
                con.execute("UPDATE items SET status='superseded', updated=? WHERE id=?", (time.time(), r["id"]))
                n += 1
    return n


def set_status(iid: str, status: str):
    with db() as con:
        con.execute("UPDATE items SET status=?, updated=? WHERE id=?", (status, time.time(), iid))


def promote_learned(kind: str | None = None) -> list[dict]:
    """Pending items whose pattern has since been learned (≥ LEARN_AFTER identical decisions)
    become 'auto' with that decision — so what you taught applies to what's already queued."""
    out = []
    with db() as con:
        q = "SELECT i.id, m.decision, m.label, m.count FROM items i JOIN memory m ON m.key = i.pattern " \
            "WHERE i.status='pending' AND m.count >= ?" + (" AND i.kind=?" if kind else "")
        rows = con.execute(q, (LEARN_AFTER, kind) if kind else (LEARN_AFTER,)).fetchall()
        for r in rows:
            con.execute("UPDATE items SET status='auto', decision=?, decided_by=?, updated=? WHERE id=?",
                        (r["decision"], f"learned: {r['label']} ×{r['count']}", time.time(), r["id"]))
    for r in rows:
        out.append(get(r["id"]))
    return out


def dismiss(iid: str):
    with db() as con:
        con.execute("UPDATE items SET status='dismissed', updated=? WHERE id=?", (time.time(), iid))


def memory(include_pairs: bool = False) -> list[dict]:
    with db() as con:
        q = "SELECT * FROM memory" + ("" if include_pairs else " WHERE key NOT LIKE 'pair|%'")
        return [dict(r) for r in con.execute(q + " ORDER BY updated DESC")]


def forget(key: str):
    with db() as con:
        con.execute("DELETE FROM memory WHERE key=?", (key,))


# ── finders ───────────────────────────────────────────────────────────────────

TRANSFER_TEXT = re.compile(
    r"paymt thru|card payment|payment - thank you|i-?bank|funds trf|fast transfer|inward credit|"
    r"own account|transfer|\btrf\b|giro|wise|youtrip|top ?up", re.I)


def _days(a: str, b: str) -> int:
    return abs((_date.fromisoformat(a) - _date.fromisoformat(b)).days)


def _slim(t: dict) -> dict:
    return {k: t.get(k) for k in ("id", "account", "account_name", "date", "amount", "payee",
                                   "imported_payee", "imported_id", "category", "notes")}


def find_transfer_pairs(txns: list[dict], account_last4: dict[str, str] | None = None,
                        max_days: int = 3) -> list[dict]:
    """
    Equal-and-opposite rows in two different accounts, neither already a transfer.
    reason_code: 'account_ref'  one side's bank text contains the other account's number
                 'transfer_text' either side reads like a transfer (card payment, I-BANK …)
    Plain PayNow-to-a-person look-alikes are NOT proposed.
    """
    account_last4 = account_last4 or {}
    cands = [t for t in txns if not t.get("transfer_id") and not t.get("payee_transfer_acct")
             and not t.get("is_child") and not t.get("starting_balance_flag") and t.get("amount")]
    out, used = [], set()
    for a in sorted(cands, key=lambda t: t["date"]):
        if a["amount"] >= 0 or a["id"] in used:
            continue
        best = None
        for b in cands:
            if (b["id"] in used or b["account"] == a["account"] or b["amount"] != -a["amount"]
                    or _days(a["date"], b["date"]) > max_days):
                continue
            text = f"{a.get('imported_payee') or ''} {a.get('payee') or ''} | {b.get('imported_payee') or ''} {b.get('payee') or ''}"
            digits = set(re.findall(r"\d{8,}", text))
            ref = any(d[-4:] == account_last4.get(b["account"]) or d[-4:] == account_last4.get(a["account"])
                      for d in digits if len(d) >= 8)
            code = "account_ref" if ref else ("transfer_text" if TRANSFER_TEXT.search(text) else None)
            if not code:
                continue
            score = (2 if code == "account_ref" else 1, -_days(a["date"], b["date"]))
            if not best or score > best[0]:
                best = (score, b, code)
        if best:
            _, b, code = best
            used.update({a["id"], b["id"]})
            out.append({"a": _slim(a), "b": _slim(b), "reason_code": code,
                        "reason": ("the bank text references the other account's number" if code == "account_ref"
                                   else "equal and opposite, reads like a transfer")})
    return out


def find_existing_duplicates(txns: list[dict], max_days: int = 2) -> list[dict]:
    """
    Rows inside ONE account that look like the same transaction twice:
    same amount, within max_days, and (same payee or same bank text) — but with
    different imported ids (a true re-import of the same id can't happen).
    """
    by_acct: dict[str, list[dict]] = {}
    for t in txns:
        if t.get("transfer_id") or t.get("is_child") or t.get("starting_balance_flag") or not t.get("amount"):
            continue
        by_acct.setdefault(t["account"], []).append(t)
    out = []
    for rows in by_acct.values():
        rows.sort(key=lambda t: t["date"])
        for i, a in enumerate(rows):
            for b in rows[i + 1:]:
                if _days(a["date"], b["date"]) > max_days:
                    break
                if a["amount"] != b["amount"]:
                    continue
                if a.get("imported_id") and a.get("imported_id") == b.get("imported_id"):
                    continue
                # two different bank references = two different transactions, however similar
                if str(a.get("imported_id") or "").startswith("ref-") and str(b.get("imported_id") or "").startswith("ref-"):
                    continue
                same_payee = _norm(a.get("payee")) and _norm(a.get("payee")) == _norm(b.get("payee"))
                same_text = _norm(a.get("imported_payee")) and _norm(a.get("imported_payee")) == _norm(b.get("imported_payee"))
                if not (same_payee or same_text):
                    continue
                one_manual = bool(a.get("imported_id")) != bool(b.get("imported_id"))
                out.append({"a": _slim(a), "b": _slim(b),
                            "reason": ("one was typed in by hand, one imported" if one_manual else
                                       "same amount and payee within %d day(s), different import ids" % _days(a["date"], b["date"]))})
    return out


# ── LLM advisor ───────────────────────────────────────────────────────────────

ADVISOR_PROMPT = """You review a personal budget (Singapore) for duplicate transactions.
For each case decide:
  import_duplicate / existing_duplicate:  "duplicate" (same real-world transaction) or "not_duplicate"
  transfer_pair:                           "transfer" (same money moving between the user's own accounts) or "not_transfer"
Clues: bank reference numbers, the same merchant text in two export formats (xls vs PDF),
card-payment wording, account numbers, and whether two identical small purchases on
one day are plausible (e.g. two coffees). Say "unsure" rather than guess.
Respond with JSON only: {"results":[{"id":"<id>","verdict":"...","confidence":0..1,"reason":"<one short sentence>"}]}"""


def advise(items: list[dict], llm) -> dict[str, dict]:
    """Ask the configured LLM about pending items. Advisory only — never applied automatically."""
    if not items or not llm.enabled:
        return {}
    cases = [{"id": it["id"], "kind": it["kind"], **it["payload"]} for it in items]
    prompt = ("Cases:\n" + json.dumps(cases, default=str, ensure_ascii=False)[:60000])
    from llm import complete_json
    try:
        data = complete_json(llm, ADVISOR_PROMPT, prompt) or {}
    except Exception as e:
        return {it["id"]: {"verdict": "unsure", "confidence": 0, "reason": f"LLM error: {e}"[:200]} for it in items}
    out = {}
    valid = {"duplicate", "not_duplicate", "transfer", "not_transfer", "unsure"}
    for r in data.get("results", []):
        if r.get("id") in {it["id"] for it in items} and r.get("verdict") in valid:
            out[r["id"]] = {"verdict": r["verdict"], "confidence": max(0.0, min(1.0, float(r.get("confidence", 0.5)))),
                            "reason": str(r.get("reason", ""))[:300], "model": llm.model, "ts": int(time.time())}
    for iid, v in out.items():
        set_llm(iid, v)
    return out


def suggested_decision(item: dict) -> str | None:
    """Map an LLM verdict onto the item's decision vocabulary (for 'accept AI suggestion')."""
    v = (item.get("llm") or {}).get("verdict")
    kind = item["kind"]
    if kind == "import_duplicate":
        return {"duplicate": "skip", "not_duplicate": "import"}.get(v)
    if kind == "transfer_pair":
        return {"transfer": "link", "not_transfer": "keep_separate"}.get(v)
    if kind == "existing_duplicate":
        if v == "not_duplicate":
            return "keep_both"
        if v == "duplicate":
            # delete the hand-typed or newer copy, keep the one with a bank reference
            a, b = item["payload"]["a"], item["payload"]["b"]
            ref = lambda t: str(t.get("imported_id") or "").startswith("ref-")
            if ref(a) and not ref(b):
                return "delete_b"
            if ref(b) and not ref(a):
                return "delete_a"
            if not a.get("imported_id"):
                return "delete_a"
            return "delete_b"
    return None
