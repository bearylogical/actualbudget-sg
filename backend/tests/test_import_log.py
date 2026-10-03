"""Import history: the log itself, the UI flow through the API, and undo."""
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


def test_ui_flow_through_api(monkeypatch):
    from fastapi.testclient import TestClient
    import main

    tx = [{"date": "2026-09-02", "description": "GRAB", "amount": 12.5, "is_credit": False}]
    monkeypatch.setattr(main, "parse_statement_bytes", lambda content, name: (list(tx), "UOB", INFO))

    async def no_ctx():
        return None
    monkeypatch.setattr(main, "get_context", no_ctx)
    calls = []

    async def fake_bridge(method, path, body=None, timeout=30):
        calls.append((path, body))
        if path == "/import":
            if body.get("dryRun"):
                return {"dryRun": True, "added": 1, "skipped": 0, "toVerify": []}
            return {"added": 1, "skipped": 0, "addedIds": ["new-1"], "unreviewed": [], "errors": []}
        if path == "/txns/delete":
            return {"deleted": len(body["ids"])}
        raise AssertionError(path)
    monkeypatch.setattr(main, "_bridge", fake_bridge)
    c = TestClient(main.app)

    r = c.post("/parse", files={"file": ("CC_TXN.xls", b"bytes-1")})
    assert r.status_code == 200, r.text
    uid = r.json()["upload_id"]
    assert r.json()["seen_before"] == []

    # dry run leaves it "parsed"; uploadId/accountName never reach the bridge
    c.post("/actual/import", json={"accountId": "acc1", "dryRun": True, "transactions": tx, "uploadId": uid,
                                   "accountName": "UOB One Card"})
    assert import_log.get(uid)["status"] == "parsed"
    assert "uploadId" not in calls[-1][1] and "accountName" not in calls[-1][1]

    c.post("/actual/import", json={"accountId": "acc1", "transactions": tx, "uploadId": uid, "accountName": "UOB One Card"})
    lst = c.get("/imports").json()
    assert lst["imports"][0]["id"] == uid and lst["imports"][0]["status"] == "imported"
    assert lst["imports"][0]["added_ids_count"] == 1 and "added_ids" not in lst["imports"][0]
    d = c.get(f"/imports/{uid}").json()
    assert d["account_name"] == "UOB One Card" and d["added_ids"] == ["new-1"]

    f = c.get(f"/imports/{uid}/file")
    assert f.status_code == 200 and f.content == b"bytes-1" and "CC_TXN.xls" in f.headers["content-disposition"]

    # same file again → seen_before points at the first upload
    r2 = c.post("/parse", files={"file": ("renamed.xls", b"bytes-1")}).json()
    assert [s["id"] for s in r2["seen_before"]] == [uid]

    u = c.post(f"/imports/{uid}/undo")
    assert u.status_code == 200 and calls[-1] == ("/txns/delete", {"ids": ["new-1"]})
    assert c.get(f"/imports/{uid}").json()["status"] == "undone"
    assert c.post(f"/imports/{uid}/undo").status_code == 409


def test_parse_failure_is_recorded(monkeypatch):
    from fastapi.testclient import TestClient
    import main

    def boom(content, name):
        raise ValueError("not a UOB/POSB export")
    monkeypatch.setattr(main, "parse_statement_bytes", boom)
    c = TestClient(main.app)
    assert c.post("/parse", files={"file": ("weird.csv", b"x")}).status_code == 422
    r = c.get("/imports").json()["imports"][0]
    assert (r["status"], r["stage"], r["filename"]) == ("failed", "parse", "weird.csv")
