"""The watch-folder scheduler writes the import history: success and failure."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scheduler"))

import import_log
from accounts import StatementInfo

INFO = StatementInfo(bank="DBS/POSB", kind="deposit", account_type="POSB Savings", last4="1234",
                     period_start="2026-09-01", period_end="2026-09-30", balance=1000.0)


@pytest.fixture
def sched(tmp_path, monkeypatch):
    import scheduler as s
    for name in ("WATCH_DIR", "DONE_DIR", "ERROR_DIR"):
        d = tmp_path / name.lower()
        d.mkdir()
        monkeypatch.setattr(s, name, d)
    monkeypatch.setattr(s, "HEARTBEAT", tmp_path / "hb.json")
    tx = [{"date": "2026-09-03", "description": "NETS KOPITIAM", "amount": 4.5, "is_credit": False,
           "imported_id": "x1"}]
    monkeypatch.setattr(s, "parse_statement", lambda content, name: (list(tx), "DBS/POSB", INFO))
    monkeypatch.setattr(s, "ensure_budget_loaded", lambda: True)
    monkeypatch.setattr(s, "fetch_context", lambda: (None, {"accounts": [{"id": "posb", "name": "POSB Savings"}]}))
    monkeypatch.setattr(s, "fetch_matches", lambda t: [])
    monkeypatch.setattr(s.acct, "cross_account_conflict", lambda *a, **k: None)
    monkeypatch.setattr(s.acct, "remember", lambda *a, **k: None)
    monkeypatch.setattr(s, "enrich", lambda t, ctx, **k: {"transactions": t, "stats": {
        "actual": 0, "seed": 1, "llm": 0, "transfer": 0, "review": 0, "unmapped": 0}})
    monkeypatch.setattr(s, "post_import_checks", lambda *a: {
        "review_ids": ["rv1"], "error": None,
        "reconcile": {"reconciled": True, "gap": 0, "fixes_queued": 0}})
    return s, tmp_path


def test_success_is_logged(sched, monkeypatch):
    s, tmp = sched
    monkeypatch.setattr(s, "resolve_account", lambda *a: ("posb", "remembered for POSB •1234"))
    monkeypatch.setattr(s, "import_transactions", lambda acc, rows: {
        "added": 1, "skipped": 0, "addedIds": ["n1"], "unreviewed": [], "errors": []})
    f = s.WATCH_DIR / "ACC_TXN.csv"
    f.write_bytes(b"posb-bytes")
    s.process_file(f)

    r = import_log.listing()[0]
    assert (r["source"], r["status"], r["filename"]) == ("scheduler", "imported", "ACC_TXN.csv")
    d = import_log.get(r["id"])
    assert d["account_name"] == "POSB Savings" and d["account_why"].startswith("remembered")
    assert d["added_ids"] == ["n1"] and d["review_ids"] == ["rv1"] and d["reconcile"]["reconciled"]
    assert d["stats"]["seed"] == 1 and d["has_file"]
    assert list(s.DONE_DIR.iterdir())


def test_failure_records_stage(sched, monkeypatch):
    s, tmp = sched

    def no_account(*a):
        raise ValueError("Can't tell which account DBS/POSB belongs to")
    monkeypatch.setattr(s, "resolve_account", no_account)
    f = s.WATCH_DIR / "mystery.csv"
    f.write_bytes(b"?")
    s.process_file(f)
    r = import_log.listing()[0]
    assert (r["status"], r["stage"]) == ("failed", "account") and "which account" in r["error"]
    assert r["label"].startswith("DBS/POSB")             # parsed details survive the failure
    assert (s.ERROR_DIR / "mystery.csv").exists()


def test_history_outage_never_blocks_import(sched, monkeypatch):
    s, tmp = sched
    monkeypatch.setattr(s, "resolve_account", lambda *a: ("posb", "x"))
    monkeypatch.setattr(s, "import_transactions", lambda acc, rows: {"added": 1, "addedIds": ["n1"]})

    def broken(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(import_log, "start", broken)
    f = s.WATCH_DIR / "ok.csv"
    f.write_bytes(b"1")
    s.process_file(f)
    assert list(s.DONE_DIR.iterdir()) and s.STATE["last_result"] == "ok"
