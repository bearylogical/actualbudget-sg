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


def test_import_links_account_skips_duplicates_and_clears_cash(monkeypatch):
    from fastapi.testclient import TestClient
    import ghostfolio
    import main

    acct = {"id": "ib", "name": "IBKR", "currency": "USD", "balance": 6463.28}
    sent, cleared = {}, []
    monkeypatch.setattr(ghostfolio, "accounts", lambda: [acct])
    monkeypatch.setattr(ghostfolio, "orders", lambda account_id=None: [
        {"comment": "ibkr:t1", "date": "2026-09-01T10:00:00.000Z", "type": "BUY", "quantity": 5,
         "SymbolProfile": {"symbol": "VWRA.L"}}])
    monkeypatch.setattr(ghostfolio, "import_activities",
                        lambda acts, dry_run=True: sent.update(acts=acts, dry=dry_run) or {"activities": acts})
    monkeypatch.setattr(ghostfolio, "clear_cash",
                        lambda ids, dry_run=True: cleared.append((ids, dry_run)) or [{**acct}])
    monkeypatch.setattr(ghostfolio, "holdings", lambda account_id=None: [])

    acts = m.map_trades(TRADES, POS, None, {"XYZ": "XYZ.DE"})["activities"]   # no accountId on purpose
    r = TestClient(main.app).post("/investments/ibkr/import", json={"activities": acts, "account_id": "ib"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert (body["imported"], body["already_imported"]) == (1, 1)
    assert [a["symbol"] for a in sent["acts"]] == ["XYZ.DE"] and sent["dry"] is False
    assert all(a["accountId"] == "ib" for a in sent["acts"])
    assert cleared == [(["ib"], False)] and body["cash_cleared"][0]["balance"] == 6463.28

    r = TestClient(main.app).post("/investments/ibkr/import", json={"activities": [{**acts[0], "comment": "hand"}]})
    assert r.status_code == 400 and "ibkr:" in r.json()["detail"]
