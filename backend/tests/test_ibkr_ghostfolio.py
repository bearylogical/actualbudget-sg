from datetime import date, timedelta

import ibkr_ghostfolio as m

POS = [{"contract_description": "VWRA @LSEETF", "position": 20, "currency": "USD", "average_price": 150.0, "asset_class": "STK"},
       {"contract_description": "VWCE @IBIS2", "position": 5, "currency": "EUR", "average_price": 100.0, "asset_class": "STK"},
       {"contract_description": "GOOGL", "position": 3, "currency": "USD", "average_price": 120.0, "asset_class": "STK"}]
TRADES = [
    {"trade_id": "fx1", "symbol": "USD.SGD", "sec_type": "CASH", "currency": "SGD", "side": "BUY", "size": 10, "price": 1.3,
     "trade_time": "2026-09-01T10:00:00Z", "exchange": "FXCONV", "commission": 0},
    {"trade_id": "t1", "symbol": "VWRA", "sec_type": "STK", "currency": "USD", "side": "BUY", "size": 5, "price": 190,
     "trade_time": "2026-09-01T10:00:00Z", "exchange": "IBRECINV", "commission": 1.7},
    {"trade_id": "t2", "symbol": "XYZ", "sec_type": "STK", "currency": "EUR", "side": "SELL", "size": 1, "price": 10,
     "trade_time": "2026-09-02T10:00:00Z", "exchange": "SMART", "commission": 1},
    {"trade_id": "t3", "symbol": "SPY", "sec_type": "OPT", "currency": "USD", "side": "BUY", "size": 1, "price": 1,
     "trade_time": "2026-09-02T10:00:00Z", "exchange": "CBOE", "commission": 1},
]


def test_map_trades():
    r = m.map_trades(TRADES, POS, "acc", {})
    assert [a["symbol"] for a in r["activities"]] == ["VWRA.L"]
    a = r["activities"][0]
    assert (a["type"], a["quantity"], a["fee"], a["dataSource"], a["accountId"]) == ("BUY", 5.0, 1.7, "YAHOO", "acc")
    assert a["date"] == "2026-09-01T10:00:00.000Z" and a["comment"].startswith("ibkr:t1")
    assert {s["trade_id"] for s in r["skipped"]} == {"fx1", "t3"}          # FX + options
    assert r["unmapped"][0]["symbol"] == "XYZ"                              # EUR stock with unknown listing
    r2 = m.map_trades(TRADES, POS, "acc", {"XYZ": "XYZ.DE"})
    assert "XYZ.DE" in [x["symbol"] for x in r2["activities"]]


def test_dedup_by_trade_id_and_key():
    acts = m.map_trades(TRADES, POS, "acc", {"XYZ": "XYZ.DE"})["activities"]
    existing = [{"comment": "ibkr:t1", "date": "2026-09-01T10:00:00.000Z", "type": "BUY", "quantity": 5,
                 "SymbolProfile": {"symbol": "VWRA.L"}}]
    new, dup = m.already_imported(acts, existing)
    assert [a["symbol"] for a in dup] == ["VWRA.L"] and [a["symbol"] for a in new] == ["XYZ.DE"]


def test_positions_and_opening_lots():
    diffs = m.compare_positions(POS, [{"symbol": "VWRA.L", "quantity": 15}, {"symbol": "ES3.SI", "quantity": 1}], {})
    d = {x["symbol"]: x["difference"] for x in diffs}
    assert d == {"VWRA.L": 5.0, "VWCE.DE": 5.0, "GOOGL": 3.0, "ES3.SI": -1.0}
    lots = m.opening_lots(POS, diffs, "acc", "2026-01-01", {})
    assert {(l["symbol"], l["quantity"], l["unitPrice"]) for l in lots} == {("VWRA.L", 5.0, 150.0), ("VWCE.DE", 5.0, 100.0), ("GOOGL", 3.0, 120.0)}


def test_dedup_matches_hand_entered_local_midnight():
    acts = m.map_trades(TRADES, POS, "acc", {"XYZ": "XYZ.DE"})["activities"]
    vwra = next(a for a in acts if a["symbol"] == "VWRA.L")
    prev_day = date.fromisoformat(vwra["date"][:10]) - timedelta(days=1)
    existing = [{"comment": None, "date": f"{prev_day}T16:00:00.000Z", "type": "BUY",
                 "quantity": vwra["quantity"], "assetProfile": {"symbol": "VWRA.L"}}]
    new, dup = m.already_imported(acts, existing)
    assert [a["symbol"] for a in dup] == ["VWRA.L"] and [a["symbol"] for a in new] == ["XYZ.DE"]


def test_cash_holding_ignored_in_position_check():
    h = [{"symbol": "GOOGL", "quantity": 3}, {"symbol": "USD", "quantity": 6463.28, "assetProfile": {"assetSubClass": "CASH"}}]
    assert [d["symbol"] for d in m.compare_positions(POS, h, {})] == ["VWRA.L", "VWCE.DE"]


# ── preview / import through the module, against a fake Ghostfolio (httpx.MockTransport) ──

import json

import httpx
import pytest

import ghostfolio


class FakeGhostfolio:
    """Just enough of Ghostfolio's HTTP API for the sync: accounts, activities, holdings, import."""

    def __init__(self, accounts, orders=()):
        self.accounts = {a["id"]: dict(a) for a in accounts}
        self.orders = [dict(o) for o in orders]
        self.account_puts: list[str] = []
        self.fail: dict[tuple[str, str], int] = {}     # (method, path prefix) → status to answer with
        self.down = False

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, req: httpx.Request) -> httpx.Response:
        if self.down:
            raise httpx.ConnectError("connection refused", request=req)
        path, method = req.url.path, req.method
        for (m_, prefix), status in self.fail.items():
            if m_ == method and path.startswith(prefix):
                return httpx.Response(status, text="boom")
        acct = req.url.params.get("accounts")
        if path == "/api/v1/auth/anonymous":
            return httpx.Response(200, json={"authToken": "jwt"})
        if path == "/api/v1/account" and method == "GET":
            return httpx.Response(200, json={"accounts": list(self.accounts.values())})
        if path.startswith("/api/v1/account/") and method == "PUT":
            body = json.loads(req.content)
            self.accounts[body["id"]]["balance"] = body["balance"]
            self.account_puts.append(body["id"])
            return httpx.Response(200, json=body)
        if path == "/api/v1/activities" and method == "GET":
            return httpx.Response(200, json={"activities": [o for o in self.orders if not acct or o["accountId"] == acct]})
        if path == "/api/v1/portfolio/holdings":
            q: dict[str, float] = {}
            for o in self.orders:
                if not acct or o["accountId"] == acct:
                    sym = o["SymbolProfile"]["symbol"]
                    q[sym] = q.get(sym, 0) + o["quantity"] * (1 if o["type"] == "BUY" else -1)
            return httpx.Response(200, json={"holdings": [{"symbol": s, "quantity": v} for s, v in q.items()]})
        if path == "/api/v1/import" and method == "POST":
            acts = json.loads(req.content)["activities"]
            if req.url.params.get("dryRun") == "false":
                for a in acts:
                    self.orders.append({**{k: a[k] for k in ("type", "quantity", "unitPrice", "fee", "date",
                                                             "comment", "accountId", "currency")},
                                        "id": f"o{len(self.orders) + 1}", "SymbolProfile": {"symbol": a["symbol"]}})
            return httpx.Response(201, json={"activities": acts})
        return httpx.Response(404)


IB = {"id": "ib", "name": "IBKR", "currency": "USD", "balance": 6463.28, "platform": {"name": "Interactive Brokers"}}
BANK = {"id": "bank", "name": "DBS", "currency": "SGD", "balance": 1000.0, "platform": {"name": "DBS"}}
HAND_VWRA = {"id": "o0", "accountId": "ib", "type": "BUY", "quantity": 15, "unitPrice": 140, "fee": 0,
             "date": "2025-06-01T16:00:00.000Z", "comment": None, "SymbolProfile": {"symbol": "VWRA.L"}}


@pytest.fixture
def gf(monkeypatch):
    monkeypatch.setenv("GHOSTFOLIO_URL", "http://gf.test")
    monkeypatch.setenv("GHOSTFOLIO_TOKEN", "t")
    monkeypatch.setattr(ghostfolio, "_jwt", {"token": None, "exp": 0.0})
    monkeypatch.setattr(m, "load_symbol_map", lambda: {"XYZ": "XYZ.DE"})
    return FakeGhostfolio([IB, BANK], [HAND_VWRA])


def test_preview_writes_nothing_and_suggests_opening_lots(gf):
    p = m.preview(ghostfolio.Client(gf.transport()), TRADES, POS)          # account found by platform name
    assert p["account"] == {"id": "ib", "name": "IBKR", "currency": "USD", "cash_balance_to_clear": 6463.28}
    assert sorted(a["symbol"] for a in p["new"]) == ["VWRA.L", "XYZ.DE"] and p["already_imported"] == 0
    assert all(a["accountId"] == "ib" for a in p["new"])
    # VWRA: 15 by hand + 5 new = IBKR's 20; VWCE and GOOGL predate the trade history
    assert {d["symbol"]: d["difference"] for d in p["position_diff_before"]}["VWRA.L"] == 5.0
    lots = {(x["symbol"], x["quantity"], x["date"][:10]) for x in p["suggested_opening_lots"]}
    assert lots == {("VWCE.DE", 5.0, "2026-08-31"), ("GOOGL", 3.0, "2026-08-31")}   # day before the first trade
    assert len(gf.orders) == 1 and gf.account_puts == [] and gf.accounts["ib"]["balance"] == 6463.28


def test_rerun_after_partial_import_does_not_duplicate_lots(gf):
    client = ghostfolio.Client(gf.transport())
    p = m.preview(client, TRADES, POS)
    approved = p["new"] + p["suggested_opening_lots"]
    gf.fail[("PUT", "/api/v1/account/")] = 500                           # import lands, clearing cash fails
    with pytest.raises(ghostfolio.GhostfolioError, match="account update failed"):
        m.import_activities(client, approved, POS)
    assert len(gf.orders) == 1 + len(approved)

    del gf.fail[("PUT", "/api/v1/account/")]
    r = m.import_activities(client, approved, POS)                        # the same call again
    assert (r["imported"], r["already_imported"]) == (0, len(approved))
    assert len(gf.orders) == 1 + len(approved)                            # nothing twice
    assert r["position_diff_after"] == [{"symbol": "XYZ.DE", "ibkr": 0.0, "ghostfolio": -1.0, "difference": 1.0,
                                         "hint": "in Ghostfolio but not in IBKR — sold elsewhere, or held at another broker"}]
    again = m.preview(client, TRADES, POS)
    assert again["new"] == [] and again["suggested_opening_lots"] == []


def test_import_clears_cash_of_the_ibkr_account_only(gf):
    client = ghostfolio.Client(gf.transport())
    acts = m.map_trades(TRADES, POS, None, {"XYZ": "XYZ.DE"})["activities"]   # no accountId on purpose
    r = m.import_activities(client, acts, account_id="ib")
    assert gf.account_puts == ["ib"] and gf.accounts["bank"]["balance"] == 1000.0
    assert r["cash_cleared"] == [{"id": "ib", "name": "IBKR", "currency": "USD", "balance": 6463.28}]
    assert all(o["accountId"] == "ib" for o in gf.orders)
    assert m.import_activities(client, acts, keep_cash=True)["cash_cleared"] == []


def test_import_refuses_untagged_or_empty_activities(gf):
    client = ghostfolio.Client(gf.transport())
    acts = m.map_trades(TRADES, POS, None, {"XYZ": "XYZ.DE"})["activities"]
    with pytest.raises(m.SyncError, match="ibkr:"):
        m.import_activities(client, [{**acts[0], "comment": "hand"}])
    with pytest.raises(m.SyncError, match="nothing"):
        m.import_activities(client, [])
    assert len(gf.orders) == 1


def test_list_activities_filters_and_sorts(gf):
    client = ghostfolio.Client(gf.transport())
    m.import_activities(client, m.map_trades(TRADES, POS, "ib", {"XYZ": "XYZ.DE"})["activities"])
    r = m.list_activities(client, "ib", "vwra")
    assert [a["date"] for a in r["activities"]] == ["2026-09-01", "2025-06-01"] and r["count"] == 2


# ── the routes: thin HTTP translation, one exception handler ──

@pytest.fixture
def api(gf, monkeypatch):
    from fastapi.testclient import TestClient
    import main
    monkeypatch.setattr(main, "_ghostfolio", lambda: ghostfolio.Client(gf.transport()))
    return TestClient(main.app)


def test_routes_keep_their_response_shapes(api, gf):
    p = api.post("/investments/ibkr/preview", json={"trades": TRADES, "positions": POS}).json()
    assert set(p) == {"account", "new", "already_imported", "skipped", "unmapped", "ghostfolio_check",
                      "position_diff_before", "position_diff_after_import", "suggested_opening_lots"}
    r = api.post("/investments/ibkr/import", json={"activities": p["new"], "positions": POS})
    assert r.status_code == 200 and set(r.json()) == {"account", "imported", "already_imported", "cash_cleared",
                                                      "position_diff_after"}
    c = api.post("/investments/ghostfolio/clear-cash", json={}).json()
    assert c == {"dry_run": True, "accounts": [{"id": "bank", "name": "DBS", "currency": "SGD", "balance": 1000.0}]}
    acts = api.get("/investments/ghostfolio/activities?symbol=XYZ").json()
    assert acts["count"] == 1
    d = api.post(f"/investments/ghostfolio/activities/{acts['activities'][0]['id']}/delete").json()
    assert d["dry_run"] is True and d["deleted"]["symbol"] == "XYZ.DE"


@pytest.mark.parametrize("setup, call, status, detail", [
    (lambda gf: None, ("post", "/investments/ibkr/preview", {"trades": TRADES, "account_id": "nope"}), 400,
     "no account nope"),
    (lambda gf: None, ("post", "/investments/ibkr/import", {"activities": []}), 400, "nothing to import"),
    (lambda gf: gf.fail.update({("POST", "/api/v1/import"): 400}), ("post", "/investments/ibkr/preview",
     {"trades": TRADES, "positions": POS}), 400, "Ghostfolio import failed (400)"),
    (lambda gf: setattr(gf, "down", True), ("post", "/investments/ibkr/preview", {"trades": TRADES}), 502,
     "Ghostfolio unreachable"),
    (lambda gf: gf.fail.update({("GET", "/api/v1/activities"): 500}), ("get", "/investments/ghostfolio/activities",
     None), 502, "Ghostfolio unreachable"),
    (lambda gf: None, ("post", "/investments/ghostfolio/activities/o0/update", {"changes": {"symbol": "X"}}), 400,
     "can't edit"),
    (lambda gf: setattr(gf, "down", True), ("post", "/investments/ghostfolio/clear-cash", {"dry_run": False}), 502,
     "Ghostfolio unreachable"),
])
def test_ghostfolio_errors_map_to_http_status(api, gf, setup, call, status, detail):
    setup(gf)
    method, url, body = call
    r = api.request(method.upper(), url, json=body)
    assert r.status_code == status and detail in r.json()["detail"], r.text
