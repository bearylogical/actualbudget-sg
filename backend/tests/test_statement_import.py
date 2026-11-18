"""Statement import: the web UI and the watch folder go through one module (statement_import.py),
tested through its interface against an in-memory Actual (FakeActual)."""
import hashlib

import pytest

import accounts as acct
import import_log
import review
import statement_import as si
from accounts import StatementInfo
from parsers import _std
from fake_actual import FakeActual

INFO = StatementInfo(bank="UOB", kind="credit_card", account_type="UOB ONE CARD", last4="2583",
                     period_start="2026-09-01", period_end="2026-09-30")
ROUTES = {"uob": "UOB One Card"}


def statement_rows():
    return [_std("2026-09-02", "GRAB *GRABFOOD SINGAPORE SG", 12.50),
            _std("2026-09-05", "COLD STORAGE SINGAPORE SG", 45.20),
            _std("2026-09-09", "NETFLIX.COM SINGAPORE SG", 19.98),
            _std("2026-09-12", "REFUND ZALORA SINGAPORE SG", -30.00)]


def fake() -> FakeActual:
    return FakeActual(accounts=[{"id": "uob", "name": "UOB One Card"}, {"id": "posb", "name": "POSB Savings"}],
                      categories=[{"id": "c-dining", "name": "Dining Out", "group_name": "Food"}])


@pytest.fixture
def parsed(monkeypatch):
    rows = statement_rows()
    monkeypatch.setattr(si, "parse_statement", lambda content, name: ([dict(r) for r in rows], "UOB", INFO))
    return rows


def _fresh(tmp_path, monkeypatch, name):
    """A separate data dir (review queue, import history, account map) for one run."""
    d = tmp_path / name
    d.mkdir()
    monkeypatch.setattr(review, "DB_FILE", d / "review.db")
    monkeypatch.setattr(import_log, "DB_FILE", d / "imports.db")
    monkeypatch.setattr(import_log, "STATEMENTS_DIR", d / "statements")
    monkeypatch.setattr(acct, "MAP_FILE", d / "account_map.json")


def _seed(gw: FakeActual, rows):
    # the Netflix row was once imported into the wrong account → a cross-account duplicate
    gw.add("posb", "2026-09-09", -1998, "Netflix", rows[2]["imported_id"])


def _queue():
    return sorted((i["kind"], i["status"]) for i in review.list_items(None))


def test_ui_and_watch_folder_end_in_the_same_state(parsed, tmp_path, monkeypatch):
    _fresh(tmp_path, monkeypatch, "ui")
    ui = fake()
    _seed(ui, parsed)
    sid = si.open_session("ui", "CC_TXN.xls", b"file")
    p = si.preview(sid, "uob", gateway=ui)
    assert p["duplicates"]["new"] == 4 and p["guard"] is None
    res_ui = si.commit(sid, "uob", include_verified=[], gateway=ui)
    ui_state, ui_queue, ui_map = ui.state(), _queue(), acct.load_map()

    _fresh(tmp_path, monkeypatch, "watch")
    watch = fake()
    _seed(watch, parsed)
    res_w = si.import_unattended("scheduler", "CC_TXN.xls", b"file", routes=ROUTES, gateway=watch)

    assert watch.state() == ui_state
    assert _queue() == ui_queue and ("existing_duplicate", "pending") in ui_queue   # cross-account, both paths
    assert acct.load_map() == ui_map and ui_map[INFO.fingerprint]["account_id"] == "uob"
    for res in (res_ui, res_w):
        assert res["import"]["added"] == 4 and all(st["ok"] for st in res["steps"].values()), res["steps"]
    assert res_w["why"] == "ACCOUNT_ROUTES['uob']"


def test_possible_duplicate_is_held_unattended_and_decided_in_the_ui(parsed, tmp_path, monkeypatch):
    def with_twin():
        gw = fake()
        gw.add("uob", "2026-09-05", -4520, "Cold Storage", "pdf-ref-777")     # same row, other export format
        return gw

    _fresh(tmp_path, monkeypatch, "watch")
    gw = with_twin()
    res = si.import_unattended("scheduler", "CC_TXN.xls", b"file", routes=ROUTES, gateway=gw)
    assert res["import"]["added"] == 3 and res["steps"]["hold_duplicates"]["held"] == 1
    assert [i["kind"] for i in review.list_items("pending")].count("import_duplicate") == 1

    _fresh(tmp_path, monkeypatch, "ui")
    gw = with_twin()
    sid = si.open_session("ui", "CC_TXN.xls", b"file")
    to_verify = si.preview(sid, "uob", gateway=gw)["duplicates"]["to_verify"]
    assert [t["description"] for t in to_verify] == ["COLD STORAGE SINGAPORE SG"]
    res = si.commit(sid, "uob", include_verified=[to_verify[0]["imported_id"]], gateway=gw)
    assert res["import"]["added"] == 4 and res["steps"]["hold_duplicates"]["held"] == 0
    assert not [i for i in review.list_items("pending") if i["kind"] == "import_duplicate"]


def test_wrong_account_guard_applies_to_every_caller(parsed, tmp_path, monkeypatch):
    gw = fake()
    for r in parsed[:3]:                                 # 3 of 4 rows already in POSB
        gw.add("posb", r["date"], -round(r["amount"] * 100), "", r["imported_id"])
    sid = si.open_session("ui", "CC_TXN.xls", b"file")
    assert "already in 'POSB Savings'" in si.preview(sid, "uob", gateway=gw)["guard"]
    with pytest.raises(si.Conflict):
        si.commit(sid, "uob", gateway=gw)
    assert import_log.get(sid)["stage"] == "account"
    assert not [c for c in gw.calls if c[0] == "import" and not c[2]]          # nothing was imported
    assert si.commit(sid, "uob", override_account_guard=True, gateway=gw)["import"]["added"] == 4

    with pytest.raises(si.Conflict):
        si.import_unattended("scheduler", "CC_TXN.xls", b"file2", routes=ROUTES, gateway=fake_with_posb(parsed))
    assert import_log.listing()[0]["stage"] == "account"


def fake_with_posb(rows):
    gw = fake()
    for r in rows[:3]:
        gw.add("posb", r["date"], -round(r["amount"] * 100), "", r["imported_id"])
    return gw


def test_manual_categories_are_imported_and_learned(parsed):
    gw = fake()
    sid = si.open_session("ui", "CC_TXN.xls", b"file")
    grab = parsed[0]["imported_id"]
    p = si.preview(sid, "uob", categories={grab: {"category": "Dining Out", "category_id": "c-dining"}}, gateway=gw)
    row = next(t for t in p["transactions"] if t["imported_id"] == grab)
    assert (row["source"], row["category_id"]) == ("manual", "c-dining")

    res = si.commit(sid, "uob", categories={grab: {"category": "Dining Out", "category_id": "c-dining"}},
                    include_verified=[], exclude=[parsed[3]["imported_id"]], gateway=gw)
    assert res["import"]["added"] == 3                                # the credit was left out
    assert [r for r in gw.rows if r["imported_id"] == grab][0]["category"] == "c-dining"
    assert res["steps"]["learn_rules"]["created"] == 1 and gw.learned[0]["category_id"] == "c-dining"
    assert si.reconcile(sid, gateway=gw)["report"]["account"] == "uob"


def test_commit_is_idempotent_and_undo_allows_a_recommit(parsed):
    gw = fake()
    sid = si.open_session("ui", "CC_TXN.xls", b"file")
    first = si.commit(sid, "uob", include_verified=[], gateway=gw)
    assert si.commit(sid, "uob", include_verified=[], gateway=gw) == first
    assert len(gw.rows) == 4 and [c for c in gw.calls if c[0] == "import" and not c[2]] == [("import", "uob", False, 4)]

    si.undo(sid, gateway=gw)
    assert gw.rows == [] and import_log.get(sid)["status"] == "undone"
    with pytest.raises(si.Conflict):
        si.undo(sid, gateway=gw)
    # Actual keeps deleted ids out of a plain re-import; committing this session again restores them
    again = si.commit(sid, "uob", include_verified=[], gateway=gw)
    assert again["import"]["added"] == 4 and import_log.get(sid)["status"] == "imported"


def test_a_failing_step_after_the_import_does_not_skip_the_rest(parsed):
    gw = fake()

    def broken(mappings):
        raise RuntimeError("rules endpoint down")
    gw.create_rules = broken
    sid = si.open_session("ui", "CC_TXN.xls", b"file")
    grab = parsed[0]["imported_id"]
    res = si.commit(sid, "uob", categories={grab: {"category": "Dining Out", "category_id": "c-dining"}},
                    include_verified=[], gateway=gw)
    assert res["import"]["added"] == 4
    assert res["steps"]["learn_rules"] == {"ok": False, "error": "rules endpoint down"}
    assert res["steps"]["scan"]["ok"] and res["steps"]["reconcile"]["ok"] and res["reconcile"]["report"]


def test_watch_folder_records_why_it_could_not_import(parsed):
    with pytest.raises(si.NotConnected):
        si.import_unattended("scheduler", "a.xls", b"a", gateway=FakeActual(loaded=False))
    assert import_log.listing()[0]["stage"] == "connect"

    with pytest.raises(si.StatementImportError, match="Can't tell which account"):
        si.import_unattended("scheduler", "b.xls", b"b",             # no route, nothing remembered, no name match
                             gateway=FakeActual(accounts=[{"id": "w", "name": "Wallet"}, {"id": "s", "name": "Savings"}]))
    r = import_log.listing()[0]
    assert (r["status"], r["stage"]) == ("failed", "account") and r["label"].startswith("UOB")


def test_parse_failure_is_recorded(monkeypatch):
    def boom(content, name):
        raise ValueError("not a UOB/POSB export")
    monkeypatch.setattr(si, "parse_statement", boom)
    with pytest.raises(si.ParseFailed):
        si.open_session("ui", "weird.csv", b"x")
    r = import_log.listing()[0]
    assert (r["status"], r["stage"], r["filename"]) == ("failed", "parse", "weird.csv")


def test_legacy_ids_cover_the_old_web_and_scheduler_formats():
    t = {"date": "2026-09-02", "description": "GRAB  FOOD", "amount": 12.0, "imported_id": "x"}
    web_stmt, web_hash, watch = si.legacy_ids(t)
    assert web_stmt == "stmt-2026-09-02-GRAB-FOOD-12"                     # JS String(12) === "12"
    assert web_hash == hashlib.sha256(b"2026-09-02|GRAB  FOOD|12").hexdigest()[:16]
    assert watch == "auto-2026-09-02-GRAB--FOOD-12.0"
    assert si.legacy_ids({**t, "amount": 12.5})[0].endswith("-12.5")


def test_ui_flow_through_the_api(parsed, monkeypatch):
    from fastapi.testclient import TestClient
    import actual_gateway
    import main

    gw = fake()
    monkeypatch.setattr(actual_gateway, "_default", gw)
    c = TestClient(main.app)

    r = c.post("/imports", files={"file": ("CC_TXN.xls", b"bytes-1")})
    assert r.status_code == 200, r.text
    sid = r.json()["session"]
    assert r.json()["seen_before"] == [] and r.json()["actual_connected"]

    p = c.post(f"/imports/{sid}/preview", json={"account_id": "uob"}).json()
    assert p["duplicates"]["new"] == 4 and import_log.get(sid)["status"] == "parsed"

    res = c.post(f"/imports/{sid}/commit", json={"account_id": "uob", "include_verified": []}).json()
    assert res["import"]["added"] == 4 and res["account_name"] == "UOB One Card"
    lst = c.get("/imports").json()
    assert lst["imports"][0]["id"] == sid and lst["imports"][0]["status"] == "imported"
    assert "rows" not in lst["imports"][0] and "added_ids" not in lst["imports"][0]
    d = c.get(f"/imports/{sid}").json()
    assert d["account_name"] == "UOB One Card" and len(d["added_ids"]) == 4 and "preview" not in d

    f = c.get(f"/imports/{sid}/file")
    assert f.status_code == 200 and f.content == b"bytes-1"
    r2 = c.post("/imports", files={"file": ("renamed.xls", b"bytes-1")}).json()
    assert [s["id"] for s in r2["seen_before"]] == [sid]

    assert c.post(f"/imports/{sid}/reconcile", json={"balance": -77.68}).status_code == 200
    assert c.post(f"/imports/{sid}/undo").status_code == 200 and gw.rows == []
    assert c.post(f"/imports/{sid}/undo").status_code == 409
    assert c.post("/imports/nope/commit", json={"account_id": "uob"}).status_code == 404
    assert c.post("/imports", files={"file": ("x.txt", b"?")}).status_code == 400
