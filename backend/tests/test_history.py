from actual_rules import ActualContext
from history import History
from pipeline import enrich
from tests.test_pipeline import CTX, FakeLLM, row

CATS = {c["id"]: c for c in CTX.categories}


def tx(payee, cat, amount=-500, **kw):
    return {"payee": payee, "category": cat, "amount": amount, "account": "a1", **kw}


def test_lookup_needs_consistency_and_repeats():
    h = History([tx("Jollibee", "c-food"), tx("Jollibee", "c-food"), tx("Once Only", "c-food"),
                 tx("Mixed", "c-food"), tx("Mixed", "c-misc")], CATS)
    assert h.lookup("jollibee", False)["category_id"] == "c-food"
    assert h.lookup("Once Only", False) is None            # only once
    assert h.lookup("Mixed", False) is None                 # 50/50
    assert h.lookup("Jollibee", True) is None               # refunds are tracked separately


def test_untrusted_rows_ignored():
    rows = [tx("Grab", "c-misc", notes="#llm"), tx("Grab", "c-misc", notes="#llm"),
            tx("Card Bill", "c-misc", transfer_id="x"), tx("Card Bill", "c-misc", transfer_id="y")]
    h = History(rows, CATS)
    assert len(h) == 0


def test_examples_are_similar_payees():
    h = History([tx("Kopitiam Bedok", "c-food"), tx("Shopee", "c-misc")], CATS)
    ex = h.examples("Kopitiam Tampines", False)
    assert ex and ex[0] == {"payee": "Kopitiam Bedok", "category": "Food & Dining"}
    assert all(e["payee"] != "Shopee" for e in ex)
    assert h.category_examples()["Misc"] == ["Shopee"]


def test_history_beats_seed_and_skips_llm():
    # Netflix: seed says Subscriptions (unmapped here) but you always file it under Misc
    h = History([tx("Netflix", "c-misc"), tx("Netflix", "c-misc")], CATS)
    llm = FakeLLM("Food & Dining")
    t = enrich([row("NETFLIX.COM")], CTX, llm=llm, history=h)["transactions"][0]
    assert (t["source"], t["category_id"], t["history_times"]) == ("history", "c-misc", 2)
    assert llm.calls == []


def test_llm_gets_examples_when_no_history_match():
    h = History([tx("Qqwrty Trading Bedok", "c-food"), tx("Qqwrty Trading Bedok", "c-food")], CATS)
    llm = FakeLLM("Food & Dining")
    t = enrich([row("QQWRTY TRADING TAMPINES")], CTX, llm=llm, history=h)["transactions"][0]
    items, _ = llm.calls[0]
    assert items[0]["examples"][0]["payee"] == "Qqwrty Trading Bedok"
    assert t["source"] == "llm"
