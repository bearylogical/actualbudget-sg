"""The Actual gateway as the one seam to Actual: the review side (scan, apply, reconcile) driven
against FakeActual, HttpGateway's reload after a bridge restart, and the one HTTP error mapping."""
import httpx
import pytest
from fastapi.testclient import TestClient

import accounts as acct
import actual_gateway
import connection
import reconcile
import review
import review_scan
from accounts import StatementInfo
from actual_gateway import GatewayError
from fake_actual import FakeActual


def fake() -> FakeActual:
    return FakeActual(accounts=[{"id": "sav", "name": "Savings"}, {"id": "card", "name": "UOB One Card"}],
                      categories=[{"id": "c-food", "name": "Food", "group_name": "Everyday"}])


def seeded() -> FakeActual:
    gw = fake()
    # a card payment seen from both sides, not linked yet
    gw.add("sav", "2026-09-06", -152373, "Card Payment", "h1",
           imported_payee="Bill Payment Card payment mBK-UOB Cards 4265884082432583")
    gw.add("card", "2026-09-06", 152373, "Card Payment", "h2", imported_payee="PAYMT THRU E-BANK")
    # the bank listed one charge twice; the parser suffixed the repeat
    gw.add("card", "2026-09-10", -2248, "Ovhcloud", "ref-741151", imported_payee="OVHcloud Montreal CA")
    gw.add("card", "2026-09-10", -2248, "Ovhcloud", "ref-741151-1", imported_payee="OVHcloud Montreal CA")
    acct.remember("UOB|credit_card|2583", "card", "UOB One Card")
    return gw


def stmt(iid, date, amount, credit=False, rb=None):
    return {"imported_id": iid, "legacy_ids": [], "date": date, "amount": amount, "is_credit": credit,
            "payee": iid, "description": iid, **({"running_balance": rb} if rb is not None else {})}


# ── review_scan against FakeActual ───────────────────────────────────────────

def test_scan_queues_duplicates_and_transfers():
    gw = seeded()
    res = review_scan.scan_and_queue("2026-09-01", "2026-09-30", gateway=gw)
    assert (res["duplicates"], res["transfers"], res["auto_applied"]) == (1, 1, 0)
    # scoped to the savings account: the card-only duplicate is out, the transfer touching savings is in
    review.DB_FILE.unlink()
    scoped = review_scan.scan_and_queue("2026-09-01", "2026-09-30", ["sav"], gateway=gw)
    assert (scoped["duplicates"], scoped["transfers"]) == (0, 1)


def test_apply_item_deletes_links_and_supersedes():
    gw = seeded()
    review_scan.scan_and_queue("2026-09-01", "2026-09-30", gateway=gw)
    dup = review.list_items("pending", "existing_duplicate")[0]
    pair = review.list_items("pending", "transfer_pair")[0]
    out = review_scan.apply_item(review.decide(dup["id"], dup["payload"]["suggest"], by="you"), gw)
    assert out["status"] == "done" and out["applied"]["ok"]
    assert [r["imported_id"] for r in gw.rows if r["account"] == "card" and r["amount"] == -2248] == ["ref-741151"]
    # another pending item about a row the link changes becomes moot
    c1 = next(r for r in gw.rows if r["imported_id"] == "h2")
    other = review.enqueue("existing_duplicate", {"a": c1, "b": gw.rows[0], "account": "card"},
                           [c1["id"], gw.rows[0]["id"]])
    out = review_scan.apply_item(review.decide(pair["id"], "link", by="you"), gw)
    assert out["applied"]["superseded"] == 1 and review.get(other["id"])["status"] == "superseded"
    assert c1["transfer_id"] == gw.rows[0]["id"]


def test_apply_item_fails_an_import_actual_only_partly_took():
    gw = seeded()
    ok = review.enqueue("import_duplicate", {"account": "card", "incoming": stmt("ref-new", "2026-09-12", 9.5)},
                        ["card", "ref-new"])
    out = review_scan.apply_item(review.decide(ok["id"], "import", by="you"), gw)
    assert out["status"] == "done" and any(r["imported_id"] == "ref-new" for r in gw.rows)
    # already there by id → Actual skips it, so the approved import didn't happen
    dup = review.enqueue("import_duplicate", {"account": "card", "incoming": stmt("ref-741151", "2026-09-10", 22.48)},
                         ["card", "ref-741151", "again"])
    out = review_scan.apply_item(review.decide(dup["id"], "import", by="you"), gw)
    assert out["status"] == "failed" and out["applied"]["errors"] == ["only 0 of 1 rows were imported"]


def test_apply_item_records_gateway_failure():
    gw = seeded()
    it = review.enqueue("import_duplicate", {"account": "card", "incoming": stmt("x", "2026-09-12", 1)}, ["x"])
    gw.loaded = False
    out = review_scan.apply_item(review.decide(it["id"], "import", by="you"), gw)
    assert out["status"] == "failed" and "No budget loaded" in out["applied"]["errors"][0]


def test_actions_for_review_items():
    p = {"a": {"id": "1", "account": "s", "date": "d"}, "b": {"id": "2", "account": "c"}}
    assert review_scan.actions_for({"kind": "existing_duplicate", "decision": "delete_b", "payload": p}) == \
        [{"type": "delete", "ids": ["2"]}]
    assert review_scan.actions_for({"kind": "transfer_pair", "decision": "not_transfer", "payload": p}) == []
    assert review_scan.touched_ids([{"type": "link", "keep_id": "1", "other_id": "2"},
                                    {"type": "delete", "ids": ["3"]}]) == {"1", "2", "3"}


# ── reconcile.run against FakeActual ─────────────────────────────────────────

def test_reconcile_finds_missing_row_and_its_fix_applies():
    gw = fake()
    gw.add("sav", "2026-09-01", 105000, "a", "a")
    gw.add("sav", "2026-09-10", -2000, "c", "c")
    info = StatementInfo(bank="UOB", kind="deposit", balance=1000.0, period_start="2026-09-01",
                         period_end="2026-09-10")
    s = [stmt("c", "2026-09-10", 20, rb=1000.0), stmt("b", "2026-09-07", 30, rb=1020.0),
         stmt("a", "2026-09-01", 1050, credit=True, rb=1050.0)]
    res = reconcile.run("sav", info, s, use_llm=False, gateway=gw)
    assert res["engine"] == "rules" and res["report"]["gap"] == -3000
    fix = res["queued"][0]
    assert fix["payload"]["fix_type"] == "import_missing"
    out = review_scan.apply_item(review.decide(fix["id"], "apply", by="you"), gw)
    assert out["status"] == "done"
    again = reconcile.run("sav", info, s, use_llm=False, gateway=gw)
    assert again["report"]["reconciled"]


# ── HttpGateway: reload after a bridge restart ───────────────────────────────

class RestartingBridge:
    """actual-bridge that has forgotten its budget until /budgets/load is called."""

    def __init__(self):
        self.loaded, self.loads = False, 0

    def handler(self, req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path == "/health":
            return httpx.Response(200, json={"budgetLoaded": self.loaded})
        if path == "/budgets/load":
            self.loads += 1
            self.loaded = True
            return httpx.Response(200, json={"ok": True})
        if not self.loaded:
            return httpx.Response(400, json={"error": "No budget loaded"})
        if path == "/txns/range":
            return httpx.Response(200, json={"transactions": [{"id": "t1"}], "accounts": []})
        if path == "/accounts":
            return httpx.Response(200, json={"accounts": [{"id": "a", "balance": 1}]})
        return httpx.Response(404, json={"error": f"no route {path}"})


@pytest.fixture
def bridge(tmp_path, monkeypatch):
    monkeypatch.setattr(connection, "CONFIG_FILE", tmp_path / "cfg.json")
    b = RestartingBridge()
    client = httpx.Client(transport=httpx.MockTransport(b.handler))
    monkeypatch.setattr(httpx, "request", lambda m, u, **kw: client.request(m, u, **kw))
    monkeypatch.setattr(httpx, "get", lambda u, **kw: client.get(u, **kw))
    monkeypatch.setattr(httpx, "post", lambda u, **kw: client.post(u, **kw))
    return b


def test_ensure_loaded_reloads_saved_connection(bridge):
    gw = actual_gateway.HttpGateway()
    assert gw.ensure_loaded() is False and bridge.loads == 0          # nothing saved → can't help
    connection.save({"serverURL": "http://actual:5006", "password": "pw", "budgetId": "sync-1"})
    assert gw.ensure_loaded() is True and bridge.loads == 1
    assert gw.ensure_loaded() is True and bridge.loads == 1          # already loaded → no second load


def test_calls_reload_after_a_restart_mid_session(bridge):
    connection.save({"serverURL": "http://actual:5006", "password": "pw", "budgetId": "sync-1"})
    gw = actual_gateway.HttpGateway()
    assert gw.list_accounts() == [{"id": "a", "balance": 1}] and bridge.loads == 1
    bridge.loaded = False                                            # bridge restarted
    assert gw.txns("2026-09-01", "2026-09-30") == ([{"id": "t1"}], []) and bridge.loads == 2


def test_gateway_error_carries_status(bridge):
    gw = actual_gateway.HttpGateway()
    with pytest.raises(GatewayError) as e:
        gw.list_accounts()
    assert e.value.no_budget and e.value.status == 400
    connection.save({"serverURL": "http://actual:5006", "password": "pw", "budgetId": "sync-1"})
    with pytest.raises(GatewayError) as e:
        gw.budget_month("2026-09")
    assert e.value.status == 404 and not e.value.no_budget


# ── routes: through the gateway, one error mapping ───────────────────────────

@pytest.fixture
def client(monkeypatch, tmp_path):
    import main
    monkeypatch.setattr(connection, "CONFIG_FILE", tmp_path / "cfg.json")
    gw = seeded()
    monkeypatch.setattr(actual_gateway, "_default", gw)
    return TestClient(main.app), gw


class Failing:
    def __init__(self, err):
        self.err = err

    def __getattr__(self, name):
        def fail(*a, **kw):
            raise self.err
        return fail


@pytest.mark.parametrize("err,code", [
    (GatewayError("No budget loaded", status=400), 400),
    (GatewayError("actual-bridge unreachable: refused"), 502),
    (GatewayError("did not answer", timeout=True), 504),
    (GatewayError("Account not found", status=404), 404),
    (GatewayError("boom", status=500), 502),
])
def test_gateway_errors_map_to_one_status(client, monkeypatch, err, code):
    c, _ = client
    monkeypatch.setattr(actual_gateway, "_default", Failing(err))
    for method, path, body in [("POST", "/review/scan", {}), ("GET", "/actual/accounts", None),
                               ("POST", "/reconcile", {"account_id": "sav"}), ("GET", "/actual/rules", None)]:
        r = c.request(method, path, json=body)
        assert r.status_code == code, (path, r.text)
        assert r.json()["detail"]
    if err.no_budget:
        assert "connect and load a budget" in r.json()["detail"]


def test_review_routes_go_through_the_gateway(client):
    c, gw = client
    res = c.post("/review/scan", json={"start": "2026-09-01", "end": "2026-09-30"}).json()
    assert (res["duplicates"], res["transfers"]) == (1, 1)
    pair = review.list_items("pending", "transfer_pair")[0]
    r = c.post(f"/review/{pair['id']}/decide", json={"decision": "link"})
    assert r.status_code == 200 and r.json()["status"] == "done"
    assert sum(1 for x in gw.rows if x["transfer_id"]) == 2
    r = c.post("/reconcile", json={"account_id": "card", "transactions": [], "balance": 0, "use_llm": False,
                                   "as_of": "2026-09-30"})
    assert r.status_code == 200 and r.json()["engine"] == "rules"


def test_actual_passthroughs(client):
    c, gw = client
    assert [a["id"] for a in c.get("/actual/accounts").json()["accounts"]] == ["sav", "card"]
    assert c.get("/actual/categories").json()["categoryGroups"][0]["categories"][0]["id"] == "c-food"
    made = c.post("/actual/categories", json={"name": "Pets", "groupName": "Everyday"}).json()
    assert made["name"] == "Pets" and any(x["name"] == "Pets" for x in gw.categories)
    assert c.post("/actual/categories", json={"name": "Orphan"}).status_code == 400
    assert c.post("/actual/rules", json={"mappings": [{"description": "Grab", "categoryId": "c-food"}]}
                  ).json()["created"] == 1
    assert c.get("/actual/budget-month/2026-09").json()["budget"]["month"] == "2026-09"
    creds = {"serverURL": "http://actual:5006", "password": "pw", "budgetId": "sync-1"}
    assert c.post("/actual/budgets", json=creds).json()["budgets"][0]["groupId"] == "sync-1"
    assert c.post("/actual/budgets/load", json=creds).json()["ok"] and connection.load_body()
    assert c.post("/actual/reset").json() == {"ok": True} and not gw.loaded and not connection.load_body()
