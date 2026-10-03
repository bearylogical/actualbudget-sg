"""
Import history — an audit trail of every statement file, from the web UI or the watch folder.

One row per uploaded / dropped file:

  ui         created at /parse (status "parsed"), completed by /actual/import ("imported" /
             "nothing_new"), "undone" if you undo it; an upload you never imported stays "parsed"
  scheduler  created when the file is picked up, "imported" / "nothing_new" or "failed"
             (with the stage it failed at and the error)

Each row keeps: file name, SHA-256 and size, bank / account / last 4 / period / closing
balance as parsed, the Actual account it went into and why, counts (parsed, added, updated,
skipped, held for review), the ids Actual added (so it can be undone later), review items
queued, the reconciliation result, and any errors. The original file is kept in
DATA_DIR/statements/ (KEEP_STATEMENTS=false to turn that off) so you can download exactly
what was imported.

The SHA-256 also answers "have I imported this exact file before?" — /parse returns earlier
imports of the same file.

Storage: SQLite DATA_DIR/imports.db, shared by backend and scheduler.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

DATA_DIR = Path(os.getenv("DATA_DIR", "/data"))
DB_FILE = DATA_DIR / "imports.db"
STATEMENTS_DIR = DATA_DIR / "statements"
KEEP_STATEMENTS = os.getenv("KEEP_STATEMENTS", "true").lower() == "true"

STATUSES = {"parsed", "imported", "nothing_new", "failed", "undone"}
JSON_COLS = ("statement", "stats", "added_ids", "review_ids", "reconcile", "errors")

SCHEMA = """
CREATE TABLE IF NOT EXISTS imports (
  id TEXT PRIMARY KEY,
  created REAL NOT NULL,
  updated REAL NOT NULL,
  source TEXT NOT NULL,                 -- ui | scheduler
  status TEXT NOT NULL,
  filename TEXT NOT NULL,
  sha256 TEXT,
  size INTEGER,
  stored TEXT,                          -- file name under statements/, if kept
  bank TEXT, label TEXT, kind TEXT, last4 TEXT,
  period_start TEXT, period_end TEXT,
  parsed INTEGER DEFAULT 0,
  account_id TEXT, account_name TEXT, account_why TEXT,
  added INTEGER DEFAULT 0, updated_rows INTEGER DEFAULT 0, skipped INTEGER DEFAULT 0, held INTEGER DEFAULT 0,
  stage TEXT,                           -- where a failure happened: parse | account | import | checks
  error TEXT,
  statement TEXT, stats TEXT, added_ids TEXT, review_ids TEXT, reconcile TEXT, errors TEXT
);
CREATE INDEX IF NOT EXISTS imports_created ON imports(created DESC);
CREATE INDEX IF NOT EXISTS imports_sha ON imports(sha256);
"""


@contextmanager
def _db():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(DB_FILE), timeout=10)
    con.row_factory = sqlite3.Row
    try:
        con.executescript(SCHEMA)
        yield con
        con.commit()
    finally:
        con.close()


def sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _row(r: sqlite3.Row | None) -> dict | None:
    if r is None:
        return None
    d = dict(r)
    for c in JSON_COLS:
        d[c] = json.loads(d[c]) if d.get(c) else ([] if c in ("added_ids", "review_ids", "errors") else None)
    d["updated"] = d.pop("updated_rows")
    d["updated_at"] = r["updated"]
    d["has_file"] = bool(d.get("stored")) and (STATEMENTS_DIR / d["stored"]).exists()
    return d


def _safe_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", Path(name).name)[:120] or "statement"


def _keep(content: bytes, digest: str, filename: str) -> str | None:
    if not KEEP_STATEMENTS or not content:
        return None
    STATEMENTS_DIR.mkdir(parents=True, exist_ok=True)
    same = sorted(p.name for p in STATEMENTS_DIR.glob(f"{digest[:16]}-*") if not p.name.endswith(".tmp"))
    if same:                                   # identical content already kept
        return same[0]
    stored = f"{digest[:16]}-{_safe_name(filename)}"
    p = STATEMENTS_DIR / stored
    if not p.exists():
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_bytes(content)
        tmp.replace(p)
    return stored


def start(source: str, filename: str, content: bytes, info=None, parsed: int = 0) -> str:
    """Record a file as soon as it's read. info: accounts.StatementInfo (or None if it didn't parse)."""
    digest = sha256(content) if content else None
    now = time.time()
    iid = uuid.uuid4().hex[:16]
    st = info.to_dict() if info is not None else None
    with _db() as con:
        con.execute(
            "INSERT INTO imports (id, created, updated, source, status, filename, sha256, size, stored, bank, label,"
            " kind, last4, period_start, period_end, parsed, statement) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (iid, now, now, source, "parsed", filename, digest, len(content or b""), _keep(content, digest, filename),
             st and st.get("bank"), st and st.get("label"), st and st.get("kind"), st and st.get("last4"),
             st and st.get("period_start"), st and st.get("period_end"), parsed, json.dumps(st) if st else None))
    return iid


_UPDATABLE = {"status", "account_id", "account_name", "account_why", "added", "skipped", "held", "stage", "error",
              "parsed", "bank", "label", "kind", "last4", "period_start", "period_end"} | set(JSON_COLS)


def update(iid: str, **fields) -> dict | None:
    if not iid:
        return None
    if "status" in fields and fields["status"] not in STATUSES:
        raise ValueError(f"unknown status {fields['status']}")
    if "updated" in fields:
        fields["updated_rows"] = fields.pop("updated")
    sets, vals = [], []
    for k, v in fields.items():
        if k not in _UPDATABLE and k != "updated_rows":
            continue
        sets.append(f"{k}=?")
        vals.append(json.dumps(v) if k in JSON_COLS and v is not None else v)
    if not sets:
        return get(iid)
    with _db() as con:
        con.execute(f"UPDATE imports SET {', '.join(sets)}, updated=? WHERE id=?", (*vals, time.time(), iid))
    return get(iid)


def set_statement(iid: str, info, parsed: int):
    st = info.to_dict()
    return update(iid, statement=st, parsed=parsed, bank=st.get("bank"), label=st.get("label"), kind=st.get("kind"),
                  last4=st.get("last4"), period_start=st.get("period_start"), period_end=st.get("period_end"))


def record_result(iid: str, result: dict, account_id: str = "", account_name: str = "", why: str = "") -> dict | None:
    """After a real (not dry-run) import: counts from the bridge's /import response."""
    added = int(result.get("added") or 0)
    held = len(result.get("unreviewed") or [])
    fields = dict(status="imported" if added or held else "nothing_new",
                  added=added, updated=int(result.get("updated") or 0), skipped=int(result.get("skipped") or 0),
                  held=held, added_ids=list(result.get("addedIds") or []), errors=list(result.get("errors") or []))
    if account_id:
        fields.update(account_id=account_id, account_name=account_name or None)
    if why:
        fields["account_why"] = why
    return update(iid, **fields)


def add_review_ids(iid: str, ids: list[str]):
    cur = get(iid)
    if cur is None or not ids:
        return cur
    return update(iid, review_ids=sorted(set(cur["review_ids"]) | set(ids)))


def fail(iid: str, stage: str, error: str):
    return update(iid, status="failed", stage=stage, error=str(error)[:1000])


def get(iid: str) -> dict | None:
    with _db() as con:
        return _row(con.execute("SELECT * FROM imports WHERE id=?", (iid,)).fetchone())


def listing(limit: int = 100, source: str = "", status: str = "", account_id: str = "",
            include_abandoned: bool = True) -> list[dict]:
    q, args = "SELECT * FROM imports WHERE 1=1", []
    if source:
        q += " AND source=?"; args.append(source)
    if status:
        q += " AND status=?"; args.append(status)
    if account_id:
        q += " AND account_id=?"; args.append(account_id)
    if not include_abandoned:
        q += " AND status != 'parsed'"
    q += " ORDER BY created DESC LIMIT ?"
    args.append(max(1, min(int(limit), 1000)))
    with _db() as con:
        rows = [_row(r) for r in con.execute(q, args).fetchall()]
    for r in rows:                       # the list doesn't need every added id
        r["added_ids_count"] = len(r.pop("added_ids") or [])
    return rows


def seen_before(digest: str, exclude: str = "") -> list[dict]:
    """Earlier records of the exact same file (by SHA-256), newest first."""
    if not digest:
        return []
    with _db() as con:
        rows = con.execute("SELECT id, created, source, status, account_name, added FROM imports "
                           "WHERE sha256=? AND id != ? ORDER BY created DESC LIMIT 10", (digest, exclude)).fetchall()
    return [dict(r) for r in rows]


def summary() -> dict:
    with _db() as con:
        rows = con.execute("SELECT status, COUNT(*) n FROM imports GROUP BY status").fetchall()
        last = con.execute("SELECT created, status, filename FROM imports WHERE status != 'parsed' "
                           "ORDER BY created DESC LIMIT 1").fetchone()
    return {"counts": {r["status"]: r["n"] for r in rows}, "last": dict(last) if last else None}


def reconcile_summary(report: dict, queued: list[dict] | None = None) -> dict:
    """The part of a reconcile report worth keeping in the history (amounts in cents)."""
    return {k: report.get(k) for k in ("reconciled", "gap", "expected_balance", "actual_balance", "as_of",
                                       "balance_reliable", "basis")} | {
        "missing": len(report.get("missing") or []), "extra": len(report.get("extra") or []),
        "fixes_queued": len(queued or [])}


def file_path(iid: str) -> tuple[Path, str] | None:
    r = get(iid)
    if not r or not r.get("stored"):
        return None
    p = STATEMENTS_DIR / r["stored"]
    return (p, r["filename"]) if p.exists() else None
