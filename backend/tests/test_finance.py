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
                    "isExcluded": False, "platformId": None}


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
