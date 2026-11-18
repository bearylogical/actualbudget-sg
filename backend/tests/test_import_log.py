"""Import history: the log itself."""
import pytest

import import_log
from accounts import StatementInfo

INFO = StatementInfo(bank="UOB", kind="credit_card", account_type="UOB ONE CARD", last4="2583",
                     period_start="2026-09-01", period_end="2026-09-30", balance=812.4, balance_is_owed=True)


def test_lifecycle_and_seen_before():
    a = import_log.start("ui", "CC_TXN.xls", b"file-bytes")
    import_log.set_statement(a, INFO, 33)
    r = import_log.get(a)
    assert r["status"] == "parsed" and r["label"].startswith("UOB Credit card") and r["parsed"] == 33
    assert r["has_file"] and r["sha256"] == import_log.sha256(b"file-bytes")

    import_log.record_result(a, {"added": 30, "skipped": 3, "addedIds": ["t1", "t2"],
                                 "unreviewed": [{"date": "2026-09-02"}]}, "acc1", "UOB One Card", "remembered")
    r = import_log.get(a)
    assert (r["status"], r["added"], r["skipped"], r["held"], r["added_ids"]) == ("imported", 30, 3, 1, ["t1", "t2"])
    assert r["account_name"] == "UOB One Card" and r["account_why"] == "remembered"

    b = import_log.start("scheduler", "CC_TXN copy.xls", b"file-bytes")
    seen = import_log.seen_before(import_log.sha256(b"file-bytes"), exclude=b)
    assert [s["id"] for s in seen] == [a]
    # same content is stored once
    assert import_log.get(b)["stored"] == import_log.get(a)["stored"]


def test_nothing_new_failed_and_listing_filters():
    a = import_log.start("scheduler", "a.csv", b"1")
    import_log.record_result(a, {"added": 0, "skipped": 12})
    assert import_log.get(a)["status"] == "nothing_new"
    b = import_log.start("scheduler", "b.csv", b"2")
    import_log.fail(b, "account", ValueError("Can't tell which account"))
    r = import_log.get(b)
    assert (r["status"], r["stage"]) == ("failed", "account") and "which account" in r["error"]
    c = import_log.start("ui", "c.csv", b"3")      # uploaded, never imported
    assert {x["id"] for x in import_log.listing(source="scheduler")} == {a, b}
    assert c not in {x["id"] for x in import_log.listing(include_abandoned=False)}
    assert import_log.summary()["counts"] == {"nothing_new": 1, "failed": 1, "parsed": 1}
    with pytest.raises(ValueError):
        import_log.update(a, status="bogus")


def test_keep_statements_off(monkeypatch):
    monkeypatch.setattr(import_log, "KEEP_STATEMENTS", False)
    a = import_log.start("ui", "x.csv", b"abc")
    assert import_log.get(a)["has_file"] is False and import_log.file_path(a) is None


def test_reconcile_summary():
    s = import_log.reconcile_summary({"reconciled": False, "gap": -1250, "expected_balance": 100, "actual_balance": 1350,
                                      "as_of": "2026-09-30", "missing": [1, 2], "extra": [], "basis": "statement"},
                                     [{"id": "r1"}])
    assert s["gap"] == -1250 and s["missing"] == 2 and s["fixes_queued"] == 1 and not s["reconciled"]

# The UI flow through the API (upload → preview → commit → undo) lives in test_statement_import.py.
