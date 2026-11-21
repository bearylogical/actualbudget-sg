from datetime import date

import httpx

import finance
import ghostfolio


def month(m, income, spent, cats=()):
    return {"month": m, "income": income, "spent": -spent, "budgeted": 0, "toBudget": 0,
            "categories": [{"id": n, "name": n, "group": "G", "is_income": False, "budgeted": 0,
                            "spent": -v, "balance": -v} for n, v in cats]}


ACTUAL = {
    "today": "2026-09-15",
    "accounts": [{"id": "sav", "name": "UOB One Account", "offbudget": False, "balance": 2_000_000},
                 {"id": "card", "name": "UOB One Card", "offbudget": False, "balance": -150_000},
                 {"id": "cpf", "name": "CPF OA", "offbudget": True, "balance": 5_000_000}],
    "months": [month("2026-05", 0, 0), month("2026-06", 100_000, 50_000),           # partial first month
               month("2026-07", 800_000, 300_000, [("Dining", 60_000), ("Shopping", 20_000)]),
               month("2026-08", 800_000, 300_000, [("Dining", 60_000), ("Shopping", 20_000)]),
               month("2026-09", 400_000, 200_000, [("Dining", 25_000), ("Shopping", 60_000)])],
    "uncategorised": {"count": 2, "amount": -3_000},
}


def test_compute_core_numbers_and_guidance():
    ghost = {"configured": True, "value": 10_000.0}
    r = finance.compute(ACTUAL, ghost, {"card"}, review_pending=1, today=date(2026, 9, 15))
    assert r["cash"] == 2_000_000 and r["card_owed"] == 150_000
    assert r["net_worth"] == 2_000_000 - 150_000 + 5_000_000 + 1_000_000
    assert r["baseline_months"] == ["2026-07", "2026-08"]          # empty + partial first month skipped
    assert r["avg_spend"] == 300_000 and round(r["savings_rate"], 3) == 0.625
    assert r["month"]["usual_by_today"] == 150_000 and r["month"]["projected"] == 400_000
    assert r["month"]["safe_per_day"] == round(100_000 / 16)
    titles = [g["title"] for g in r["guidance"]]
    assert any("waiting in Review" in t for t in titles)
    assert any("ahead of your usual pace" in t for t in titles)
    shop = next(c for c in r["categories"] if c["name"] == "Shopping")
    assert shop["status"] == "serious"                             # 600 vs usual 200 for the whole month
    assert r["cashflow"][0]["month"] == "2026-06"


def test_ghostfolio_summary_and_auth(monkeypatch):
    monkeypatch.setenv("GHOSTFOLIO_URL", "http://gf")
    monkeypatch.setenv("GHOSTFOLIO_TOKEN", "tok")
    ghostfolio._jwt.update(token=None, exp=0)

    def handler(req):
        p = req.url.path
        if p == "/api/v1/auth/anonymous":
            return httpx.Response(200, json={"authToken": "jwt"})
        assert req.headers["authorization"] == "Bearer jwt"
        if p == "/api/v1/account":
            return httpx.Response(200, json={"accounts": [{"name": "IBKR", "valueInBaseCurrency": 100.5}],
                                             "totalValueInBaseCurrency": 103.5, "totalBalanceInBaseCurrency": 3})
        if p == "/api/v2/portfolio/performance":
            return httpx.Response(200, json={"performance": {"netPerformancePercentageWithCurrencyEffect": 0.05}})
        if p == "/api/v1/portfolio/holdings":
            return httpx.Response(200, json={"holdings": [{"name": "VWRA", "symbol": "VWRA.L", "valueInBaseCurrency": 100.5}]})
        return httpx.Response(200, json={})
    s = ghostfolio.summary(httpx.MockTransport(handler))
    assert s["value"] == 100.5 and s["performance_ytd"] == 0.05 and s["holdings"][0]["symbol"] == "VWRA.L"


def test_ghostfolio_orders_activities_with_order_fallback(monkeypatch):
    monkeypatch.setenv("GHOSTFOLIO_URL", "http://gf")
    monkeypatch.setenv("GHOSTFOLIO_TOKEN", "tok")
    for legacy in (False, True):
        ghostfolio._jwt.update(token=None, exp=0)

        def handler(req):
            p = req.url.path
            if p == "/api/v1/auth/anonymous":
                return httpx.Response(200, json={"authToken": "jwt"})
            if p == ("/api/v1/order" if legacy else "/api/v1/activities"):
                assert req.url.params["accounts"] == "acc"
                return httpx.Response(200, json={"activities": [{"comment": "ibkr:t1"}], "count": 1})
            return httpx.Response(404)
        assert ghostfolio.orders("acc", httpx.MockTransport(handler)) == [{"comment": "ibkr:t1"}]

def test_ghostfolio_holdings_symbol_from_asset_profile(monkeypatch):
    monkeypatch.setenv("GHOSTFOLIO_URL", "http://gf")
    monkeypatch.setenv("GHOSTFOLIO_TOKEN", "tok")
    ghostfolio._jwt.update(token=None, exp=0)

    def handler(req):
        if req.url.path == "/api/v1/auth/anonymous":
            return httpx.Response(200, json={"authToken": "jwt"})
        return httpx.Response(200, json={"holdings": [{"quantity": 7, "assetProfile": {"symbol": "ASML.AS", "name": "ASML"}}]})
    h = ghostfolio.holdings("acc", httpx.MockTransport(handler))
    assert h[0]["symbol"] == "ASML.AS" and h[0]["name"] == "ASML" and h[0]["quantity"] == 7

def test_ghostfolio_not_configured(monkeypatch):
    monkeypatch.delenv("GHOSTFOLIO_URL", raising=False)
    assert ghostfolio.summary() == {"configured": False}


def test_week_vs_usual():
    from datetime import date
    rows = [
        {"id": "1", "date": "2026-09-25", "amount": -5000, "category": "c1", "payee": "Koufu", "account_name": "UOB"},
        {"id": "2", "date": "2026-09-24", "amount": -20000, "category": None, "payee": "Shopee", "account_name": "UOB"},
        {"id": "3", "date": "2026-09-23", "amount": -152373, "transfer_id": "x", "payee": "Card"},   # transfer: ignored
        {"id": "4", "date": "2026-09-23", "amount": 788973, "category": "inc", "payee": "Salary"},    # income: ignored
        {"id": "5", "date": "2026-09-10", "amount": -8000, "category": "c1", "payee": "Koufu"},       # baseline
        {"id": "6", "date": "2026-09-03", "amount": -8000, "category": "c1", "payee": "Koufu"},       # baseline
    ]
    w = finance.week(rows, {"c1": "Dining & Hawker"}, today=date(2026, 9, 27))
    assert w["spent"] == 25000 and w["usual_week"] == 4000 and w["transactions"] == 2
    assert w["categories"][0] == {"category": "Uncategorised", "spent": 20000, "usual_week": 0}
    assert w["uncategorised"] == {"count": 1, "amount": 20000}
    assert w["largest"][0]["payee"] == "Shopee"


def _gf_env(monkeypatch):
    monkeypatch.setenv("GHOSTFOLIO_URL", "http://gf")
    monkeypatch.setenv("GHOSTFOLIO_TOKEN", "tok")
    ghostfolio._jwt.update(token=None, exp=0)


def test_ghostfolio_summary_counts_securities_only(monkeypatch):
    _gf_env(monkeypatch)

    def handler(req):
        p = req.url.path
        if p == "/api/v1/auth/anonymous":
            return httpx.Response(200, json={"authToken": "jwt"})
        if p == "/api/v1/account":
            return httpx.Response(200, json={"accounts": [
                {"name": "IBKR", "valueInBaseCurrency": 1100, "balanceInBaseCurrency": 100},
                {"name": "POSB", "valueInBaseCurrency": 50, "balanceInBaseCurrency": 50}],
                "totalValueInBaseCurrency": 1150, "totalBalanceInBaseCurrency": 150})
        if p == "/api/v1/portfolio/holdings":
            return httpx.Response(200, json={"holdings": [
                {"symbol": "VWRA.L", "valueInBaseCurrency": 1000, "assetSubClass": "ETF"},
                {"symbol": "USD", "valueInBaseCurrency": 100, "assetSubClass": "CASH"}]})
        return httpx.Response(200, json={})
    s = ghostfolio.summary(httpx.MockTransport(handler))
    assert s["value"] == 1000 and s["cash"] == 150
    assert [h["symbol"] for h in s["holdings"]] == ["VWRA.L"]
    assert s["accounts"] == [{"name": "IBKR", "value": 1000, "platform": None}]   # cash-only POSB dropped


def test_ghostfolio_clear_cash(monkeypatch):
    _gf_env(monkeypatch)
    puts = []

    def handler(req):
        p = req.url.path
        if p == "/api/v1/auth/anonymous":
            return httpx.Response(200, json={"authToken": "jwt"})
        if req.method == "PUT":
            puts.append((p, __import__("json").loads(req.content)))
            return httpx.Response(200, json={})
        return httpx.Response(200, json={"accounts": [
            {"id": "ib", "name": "IBKR", "currency": "USD", "balance": 6463.28, "isExcluded": None, "platformId": None},
            {"id": "posb", "name": "POSB", "currency": "SGD", "balance": 3010.69},
            {"id": "z", "name": "Zero", "currency": "SGD", "balance": 0}]})
    t = httpx.MockTransport(handler)
    assert [a["id"] for a in ghostfolio.clear_cash(None, dry_run=True, transport=t)] == ["ib", "posb"] and not puts
    assert [a["id"] for a in ghostfolio.clear_cash(["ib"], dry_run=False, transport=t)] == ["ib"]
    path, body = puts[0]
    assert path == "/api/v1/account/ib"
    assert body == {"id": "ib", "name": "IBKR", "currency": "USD", "balance": 0, "comment": None,
                    "platformId": None}   # no isExcluded: newer Ghostfolio rejects it


TXNS = [
    {"id": "1", "date": "2026-09-25", "amount": -5000, "category": "c1", "payee": "Koufu", "account": "sav", "account_name": "UOB One Account"},
    {"id": "2", "date": "2026-09-24", "amount": -20000, "category": None, "payee": "Shopee", "notes": "headphones", "account": "card", "account_name": "UOB One Card"},
    {"id": "3", "date": "2026-09-23", "amount": -150000, "transfer_id": "x", "payee": "UOB One Card", "account": "sav", "account_name": "UOB One Account"},
    {"id": "4", "date": "2026-09-01", "amount": 800000, "category": "inc", "payee": "Salary", "account": "sav", "account_name": "UOB One Account"},
    {"id": "5", "date": "2026-08-10", "amount": -8000, "category": "c1", "payee": "Koufu", "account": "sav", "account_name": "UOB One Account"},
    {"id": "6", "date": "2026-08-10", "amount": -3000, "is_child": True, "category": "c1", "account": "sav"},   # split child: skipped
    {"id": "7", "date": "2026-07-31", "amount": 10_000, "category": "inc", "payee": "Dividend", "account": "cpf", "account_name": "CPF OA", "offbudget": True},
]
NAMES = {"c1": "Dining & Hawker", "inc": "Salary"}


def test_transactions_filters_and_totals():
    r = finance.transactions(TXNS, NAMES)
    assert r["count"] == 6 and [t["id"] for t in r["transactions"]][:3] == ["1", "2", "3"]
    assert r["money_out"] == 33000 and r["money_in"] == 810_000          # transfer excluded from totals
    assert finance.transactions(TXNS, NAMES, kind="spend")["count"] == 3
    assert [t["id"] for t in finance.transactions(TXNS, NAMES, kind="transfer")["transactions"]] == ["3"]
    assert finance.transactions(TXNS, NAMES, q="HEADPHONES")["transactions"][0]["category"] == "Uncategorised"
    assert finance.transactions(TXNS, NAMES, category="dining")["count"] == 2
    assert finance.transactions(TXNS, NAMES, account="card")["count"] == 1
    page = finance.transactions(TXNS, NAMES, limit=2)
    assert page["returned"] == 2 and page["truncated"] and page["count"] == 6


def test_trends_months_and_month_end_balances():
    actual = {**ACTUAL, "today": "2026-09-30"}
    r = finance.trends(actual, TXNS, today=date(2026, 9, 30))
    assert [m["month"] for m in r["months"]] == ["2026-06", "2026-07", "2026-08", "2026-09"]   # empty May dropped
    aug = next(m for m in r["months"] if m["month"] == "2026-08")
    assert aug["spent"] == 300_000 and aug["net"] == 500_000 and aug["savings_rate"] == 0.625
    assert aug["categories"][0] == {"name": "Dining", "group": "G", "spent": 60_000}
    # end of Aug = today's balance minus September's rows (salary in, Koufu/transfer out; card's Shopee)
    assert aug["balance_end"]["on_budget"] == (2_000_000 - (800_000 - 5000 - 150_000)) + (-150_000 + 20_000)
    jul = next(m for m in r["months"] if m["month"] == "2026-07")
    assert jul["balance_end"]["off_budget"] == 5_000_000                  # dividend dated 31 Jul is inside July
    assert r["months"][-1]["balance_end"]["total"] == 2_000_000 - 150_000 + 5_000_000
    assert not r["months"][-1]["partial"]


def _activity_handler(calls, legacy=False):
    act = {"id": "a1", "date": "2026-03-22T16:00:00.000Z", "type": "BUY", "quantity": 9.1261, "unitPrice": 164.36,
           "fee": 0, "currency": "USD", "comment": None, "accountId": "ib", "tags": [{"id": "t1"}],
           "SymbolProfile": {"symbol": "VWRA.L", "dataSource": "YAHOO", "currency": "USD"}}

    def handler(req):
        p = req.url.path
        if p == "/api/v1/auth/anonymous":
            return httpx.Response(200, json={"authToken": "jwt"})
        if req.method == "GET" and p == "/api/v1/activities":
            return httpx.Response(200, json={"activities": [act]})
        if legacy and p.startswith("/api/v1/activities/"):
            return httpx.Response(404)
        if req.method in ("PUT", "DELETE"):
            calls.append((req.method, p, __import__("json").loads(req.content) if req.content else None))
            return httpx.Response(200, json={**act, **(calls[-1][2] or {})} if req.method == "PUT" else {})
        return httpx.Response(404)
    return handler


def test_ghostfolio_update_activity(monkeypatch):
    _gf_env(monkeypatch)
    for legacy in (False, True):
        calls = []
        t = httpx.MockTransport(_activity_handler(calls, legacy))
        dry = ghostfolio.update_activity("a1", {"fee": 1.70}, dry_run=True, transport=t)
        assert dry["before"]["fee"] == 0 and dry["after"]["fee"] == 1.70 and not calls
        ghostfolio.update_activity("a1", {"fee": 1.70}, dry_run=False, transport=t)
        method, path, body = calls[0]
        assert method == "PUT" and path == ("/api/v1/order/a1" if legacy else "/api/v1/activities/a1")
        assert body["fee"] == 1.70 and body["symbol"] == "VWRA.L" and body["dataSource"] == "YAHOO"
        assert body["date"] == "2026-03-22T16:00:00.000Z" and body["tags"] == ["t1"] and body["accountId"] == "ib"


def test_ghostfolio_update_activity_rejects_symbol(monkeypatch):
    _gf_env(monkeypatch)
    import pytest
    with pytest.raises(ghostfolio.GhostfolioError):
        ghostfolio.update_activity("a1", {"symbol": "X"}, transport=httpx.MockTransport(_activity_handler([])))


def test_ghostfolio_delete_activity(monkeypatch):
    _gf_env(monkeypatch)
    calls = []
    t = httpx.MockTransport(_activity_handler(calls))
    assert ghostfolio.delete_activity("a1", dry_run=True, transport=t)["deleted"]["symbol"] == "VWRA.L" and not calls
    ghostfolio.delete_activity("a1", dry_run=False, transport=t)
    assert calls == [("DELETE", "/api/v1/activities/a1", None)]
    import pytest
    with pytest.raises(ghostfolio.GhostfolioError):
        ghostfolio.delete_activity("nope", transport=t)


# ── the Snapshot, driven against FakeActual ──

import pytest

from actual_gateway import GatewayError
from fake_actual import FakeActual


def fake_budget() -> FakeActual:
    fa = FakeActual(
        accounts=[{"id": "sav", "name": "UOB One Account"}, {"id": "card", "name": "UOB One Card"},
                  {"id": "cpf", "name": "CPF OA", "offbudget": True}],
        categories=[{"id": "din", "name": "Dining", "group_name": "Spend"},
                    {"id": "shop", "name": "Shopping", "group_name": "Spend"},
                    {"id": "sal", "name": "Salary", "group_name": "Income", "is_income": True}])
    fa.today = "2026-09-15"
    fa.add("sav", "2026-06-20", 1_000_000, "Opening", starting_balance_flag=True)
    fa.add("cpf", "2026-06-20", 5_000_000, "Opening", starting_balance_flag=True)
    for mo in ("06", "07", "08"):
        fa.add("sav", f"2026-{mo}-25", 800_000, "ACME PTE LTD", category="sal")
        fa.add("card", f"2026-{mo}-10", -60_000, "Ya Kun", category="din")
        fa.add("card", f"2026-{mo}-12", -20_000, "Uniqlo", category="shop")
        fa.add("sav", f"2026-{mo}-28", -200_000, "Rent", category="shop")
    fa.add("card", "2026-09-10", -25_000, "Ya Kun", category="din")
    fa.add("card", "2026-09-12", -60_000, "Uniqlo", category="shop")
    fa.add("card", "2026-09-13", -3_000, "Mystery")                                  # uncategorised
    fa.add("sav", "2026-09-14", -50_000, "To card", transfer_id="x1")                # a transfer, not spend
    fa.add("card", "2026-09-14", 50_000, "From savings", transfer_id="x2")
    return fa


def snapshot(fa, **kw) -> finance.Snapshot:
    kw.setdefault("today", lambda: date(2026, 9, 15))
    return finance.Snapshot(fa, **kw)


def test_snapshot_summary_composes_actual_ghostfolio_and_review():
    fa = fake_budget()
    s = snapshot(fa, ghost=lambda: {"configured": True, "value": 1_000.0},
                 card_ids=lambda: {"card"}, review_counts=lambda: {"pending": 2}).summary(6)
    assert s["as_of"] == "2026-09-15"
    sav = 1_000_000 + 3 * (800_000 - 200_000) - 50_000
    card = -3 * 80_000 - 88_000 + 50_000
    assert s["cash"] == sav and s["card_owed"] == -card and s["offbudget"] == 5_000_000
    assert s["investments"] == 100_000 and s["net_worth"] == sav + card + 5_000_000 + 100_000
    assert s["baseline_months"] == ["2026-07", "2026-08"]                  # partial June skipped
    assert s["avg_spend"] == 280_000 and s["avg_income"] == 800_000
    assert s["month"]["spent"] == 85_000              # budget-month spending: no transfer, no uncategorised row
    assert s["uncategorised"] == {"count": 1, "amount": -3_000} and s["review_pending"] == 2
    assert [c["name"] for c in s["categories"]] == ["Shopping", "Dining"]


def test_snapshot_week_names_categories_and_skips_transfers():
    w = snapshot(fake_budget()).week(7)
    assert (w["from"], w["to"]) == ("2026-09-09", "2026-09-15")
    assert w["spent"] == 88_000 and w["transactions"] == 3
    assert {c["category"]: c["spent"] for c in w["categories"]} == {"Shopping": 60_000, "Dining": 25_000,
                                                                     "Uncategorised": 3_000}
    assert w["usual_week"] == round((20_000 + 200_000) / 4)                 # Aug 12 + 28; Aug 10 is before the baseline


def test_snapshot_transactions_defaults_and_validation():
    snap = snapshot(fake_budget())
    r = snap.transactions(kind="spend")
    assert (r["from"], r["to"]) == ("2026-08-17", "2026-09-15")
    assert r["count"] == 4 and r["money_out"] == 288_000                    # Aug rent + 3 September card rows
    assert r["transactions"][0]["payee"] == "Mystery" and r["transactions"][0]["category"] == "Uncategorised"
    assert snap.transactions(category="dining", start="2026-01-01")["count"] == 4
    for bad in ({"start": "15/09/2026"}, {"start": "2026-09-20"}, {"kind": "refund"}):
        with pytest.raises(ValueError):
            snap.transactions(**bad)


def test_snapshot_trends_works_balances_back_from_today():
    t = snapshot(fake_budget()).trends(3)
    assert [m["month"] for m in t["months"]] == ["2026-07", "2026-08", "2026-09"]
    jul, sep = t["months"][0], t["months"][-1]
    assert jul["income"] == 800_000 and jul["spent"] == 280_000 and not jul["partial"] and sep["partial"]
    assert jul["balance_end"]["off_budget"] == 5_000_000
    assert jul["balance_end"]["on_budget"] == 1_000_000 + 2 * (800_000 - 280_000)


def test_snapshot_weekly_and_gateway_errors():
    fa = fake_budget()
    w = snapshot(fa, review_counts=lambda: {"pending": 1, "transfer_pair": {"pending": 1}}).weekly()
    assert set(w) == {"week", "summary", "review"} and w["review"]["pending"] == 1
    assert w["summary"]["review_pending"] == 1 and w["week"]["spent"] == 88_000
    fa.loaded = False
    with pytest.raises(GatewayError):
        snapshot(fa).summary()


def test_finance_routes_go_through_the_snapshot(monkeypatch):
    from fastapi.testclient import TestClient
    import actual_gateway
    import health
    import main

    fa = fake_budget()
    monkeypatch.setattr(actual_gateway, "default", lambda: fa)
    monkeypatch.setattr(ghostfolio, "summary", lambda: {"configured": False})

    async def fake_health(url, refresh_llm=False):
        return {"status": "ok", "components": {"bridge": {"status": "ok"}}}
    monkeypatch.setattr(health, "full_report", fake_health)
    c = TestClient(main.app)

    r = c.get("/finance/weekly")
    assert r.status_code == 200, r.text
    assert set(r.json()) == {"week", "summary", "review", "health"}
    assert c.get("/finance/summary").json()["as_of"] == "2026-09-15"
    assert c.get("/finance/trends?months=2").status_code == 200
    assert c.get("/finance/transactions?kind=nope").status_code == 400
    fa.loaded = False
    assert c.get("/finance/week").status_code == 502
