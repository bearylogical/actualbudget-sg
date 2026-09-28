from actual_rules import ActualContext
from categorizer import categorize
from pipeline import enrich


class FakeLLM:
    enabled = True
    def __init__(self, answer):
        self.answer, self.calls = answer, []
    def status(self):
        return {"enabled": True, "provider": "fake", "model": "fake", "cached_payees": 0}
    def categorize(self, items, allowed):
        self.calls.append((items, allowed))
        return {it["key"]: {"category": self.answer, "confidence": 0.7} for it in items}


def row(desc, amount=10.0, credit=False, **kw):
    c = categorize(desc, credit)
    return {"date": "2026-09-01", "description": desc, "amount": amount, "is_credit": credit,
            "category": c["category"], "payee": c["payee"], "kind": c["kind"], "source": c["source"], **kw}


CTX = ActualContext(
    categories=[{"id": "c-food", "name": "Food & Dining"}, {"id": "c-groc", "name": "Groceries"},
                {"id": "c-misc", "name": "Misc"}],
    payees=[{"id": "p-ss", "name": "Sheng Siong"}],
    rules=[{"id": "r", "stage": None, "conditions": [{"field": "payee", "op": "is", "value": "p-ss"}],
            "actions": [{"field": "category", "op": "set", "value": "c-misc"}]}],
)


def test_actual_rule_beats_seed():
    t = enrich([row("SHENG SIONG SUPERMARKET SINGAPORE SG")], CTX, use_llm=False)["transactions"][0]
    assert (t["source"], t["category_id"], t["category"]) == ("actual", "c-misc", "Misc")


def test_seed_resolves_via_legacy_name():
    t = enrich([row("KOPITIAM SINGAPORE SG")], CTX, use_llm=False)["transactions"][0]
    assert (t["source"], t["category_id"]) == ("seed", "c-food")    # Dining & Hawker → "Food & Dining"


def test_seed_unmapped_flagged_and_alias_fixes_it():
    t = enrich([row("NETFLIX.COM")], CTX, use_llm=False)["transactions"][0]
    assert t["unmapped"] and t["category_id"] is None
    t = enrich([row("NETFLIX.COM")], CTX, use_llm=False, aliases={"Subscriptions": "Misc"})["transactions"][0]
    assert t["category_id"] == "c-misc"


def test_llm_only_for_unknown_non_transfer_and_constrained():
    llm = FakeLLM("Groceries")
    res = enrich([row("ZHANG LIANG MALATANG SINGAPORE SG"), row("ZHANG LIANG MALATANG SINGAPORE SG"),
                  row("PAYMT THRU E-BANK (EP52)", credit=True), row("PayNow Transfer To: JOHN")], CTX, llm=llm)
    tx = res["transactions"]
    assert len(llm.calls) == 1 and len(llm.calls[0][0]) == 1          # deduped by payee
    assert set(llm.calls[0][1]) == {"Food & Dining", "Groceries", "Misc"}
    assert tx[0]["source"] == "llm" and tx[0]["category_id"] == "c-groc" and "#llm" in tx[0]["notes"]
    assert tx[2]["source"] is None and tx[2]["kind"] == "transfer" and tx[2]["notes"] == ""
    assert "#review" in tx[3]["notes"]


def test_manual_kept_and_idempotent():
    rows = [row("KOPITIAM SINGAPORE SG")]
    first = enrich(rows, CTX, use_llm=False)["transactions"]
    again = enrich(first, CTX, use_llm=False)["transactions"]
    assert first[0]["category_id"] == again[0]["category_id"]
    manual = [{**first[0], "category": "Misc", "category_id": "c-misc", "source": "manual"}]
    assert enrich(manual, CTX, use_llm=False)["transactions"][0]["category_id"] == "c-misc"


def test_offline_without_actual():
    t = enrich([row("KOPITIAM SINGAPORE SG")], None, use_llm=False)["transactions"][0]
    assert t["category"] == "Dining & Hawker" and t["category_id"] is None


def test_existing_raw_payee_is_reused_until_merged():
    ctx = ActualContext(categories=[{"id": "c-food", "name": "Food & Dining"}],
                        payees=[{"id": "p-raw", "name": "YA KUN KAYA TOAST - T3"}],
                        rules=[{"id": "r", "stage": None,
                                "conditions": [{"field": "payee", "op": "is", "value": "p-raw"}],
                                "actions": [{"field": "category", "op": "set", "value": "c-food"}]}])
    t = enrich([row("YA KUN KAYA TOAST - T3")], ctx, use_llm=False)["transactions"][0]
    assert t["payee"] == "YA KUN KAYA TOAST - T3" and t["source"] == "actual"
    ctx.payees.append({"id": "p-clean", "name": "Ya Kun Kaya Toast"}); ctx.__post_init__()
    t = enrich([row("YA KUN KAYA TOAST - T3")], ctx, use_llm=False)["transactions"][0]
    assert t["payee"] == "Ya Kun Kaya Toast"


def test_unmapped_seed_falls_back_to_llm():
    # Netflix → seed "Subscriptions", which this budget doesn't have: ask the LLM for one of ours
    llm = FakeLLM("Misc")
    t = enrich([row("NETFLIX.COM")], CTX, llm=llm)["transactions"][0]
    assert (t["source"], t["category_id"], t["unmapped"]) == ("llm", "c-misc", False)
    assert t["seed_category"] == "Subscriptions" and len(llm.calls) == 1


def test_unmapped_seed_stays_flagged_when_llm_cannot_resolve():
    t = enrich([row("NETFLIX.COM")], CTX, llm=FakeLLM("Not A Category"))["transactions"][0]
    assert (t["source"], t["category_id"], t["unmapped"], t["category"]) == ("seed", None, True, "Subscriptions")
