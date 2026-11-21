import pytest

import actual_gateway
import bridge_client
import recategorize
import review
import review_scan
from actual_rules import ActualContext
from tests.fake_actual import FakeActual

CATS = [
    {"id": "c-food", "name": "Food", "group_name": "Living"},
    {"id": "c-tpt", "name": "Transport", "group_name": "Living"},
    {"id": "c-sal", "name": "Salary", "group_name": "Income", "is_income": True},
]


def ctx(rules=None):
    return ActualContext(categories=CATS, payees=[{"id": "p-grab", "name": "Grab"}],
                         rules=rules or [], accounts=[{"id": "acc", "name": "UOB One Card"}])


def row(i, text, amount=-1250, category=None, **kw):
    return {"id": f"t{i}", "account": "acc", "account_name": "UOB One Card", "date": "2026-09-01",
            "amount": amount, "payee": None, "imported_payee": text, "category": category,
            "notes": kw.pop("notes", None), **kw}


class NoLLM:
    enabled = False
    model = provider = None
    def status(self): return {"enabled": False}


def test_skips_transfers_splits_offbudget_and_zero():
    rows = [row(1, "x", transfer_id="t9"), row(2, "x", is_parent=True), row(3, "x", offbudget=True),
            row(4, "x", amount=0), row(5, "x", payee_transfer_acct="acc2"), row(6, "x", starting_balance_flag=True)]
    assert recategorize.candidates(rows) == []


def test_rule_proposal_for_uncategorised_and_mismatch_only_via_actual_rule():
    rule = {"id": "r1", "stage": None, "conditionsOp": "and",
            "conditions": [{"field": "imported_payee", "op": "contains", "value": "grab*"}],
            "actions": [{"field": "category", "op": "set", "value": "c-tpt"}]}
    rows = [row(1, "GRAB* RIDE SINGAPORE"),                            # uncategorised → Transport
            row(2, "GRAB* RIDE SINGAPORE", category="c-food"),         # your rule disagrees → propose
            row(3, "GRAB* RIDE SINGAPORE", category="c-tpt")]          # already right → nothing
    got = {p["txn"]["id"]: p for p in recategorize.propose(rows, ctx([rule]), use_llm=False, llm=NoLLM())}
    assert set(got) == {"t1", "t2"}
    assert got["t1"]["proposed"]["id"] == "c-tpt" and got["t1"]["proposed"]["source"] == "actual"
    assert "currently Food" in got["t2"]["reason"]


def test_update_strips_review_tag_and_learning_pattern(monkeypatch):
    p = {"account": "acc", "txn": {**row(1, "GRAB* RIDE"), "payee": "Grab", "notes": "#llm #review"},
         "current": {"id": None, "name": None},
         "proposed": {"id": "c-tpt", "name": "Transport", "source": "llm", "confidence": 0.9}, "reason": "x"}
    assert recategorize.update_for(p) == {"id": "t1", "category": "c-tpt", "notes": "#llm"}
    assert review_scan.actions_for({"kind": "recategorize", "decision": "apply", "payload": p}) == \
        [{"type": "categorize", "updates": [{"id": "t1", "category": "c-tpt", "notes": "#llm"}]}]
    assert review_scan.actions_for({"kind": "recategorize", "decision": "keep", "payload": p}) == []

    # two identical approvals for the same payee → the third proposal resolves itself
    for i in (1, 2):
        it = review.enqueue("recategorize", {**p, "txn": {**p["txn"], "id": f"t{i}"}}, [f"t{i}", "c-tpt"])
        assert it["status"] == "pending"
        review.decide(it["id"], "apply")
    it = review.enqueue("recategorize", {**p, "txn": {**p["txn"], "id": "t3"}}, ["t3", "c-tpt"])
    assert it["status"] == "auto" and it["decision"] == "apply"


def rule(text, category):
    return {"id": f"r-{text}", "stage": None, "conditionsOp": "and",
            "conditions": [{"field": "imported_payee", "op": "contains", "value": text}],
            "actions": [{"field": "category", "op": "set", "value": category}]}


def fake(*rows):
    """FakeActual with an on-budget card; rows: (imported_payee, payee) uncategorised on 2026-09-01."""
    fa = FakeActual(accounts=[{"id": "acc", "name": "UOB One Card"}], categories=[dict(c) for c in CATS])
    fa.rules = [rule("grab", "c-tpt"), rule("shopee", "c-food")]
    for text, payee in rows:
        fa.add("acc", "2026-09-01", -1250, payee, imported_payee=text)
    return fa


def scan(fa):
    return recategorize.scan(fa, start="2026-08-01", end="2026-09-30", use_llm=False)


def queued(status="pending"):
    return {i["payload"]["txn"]["id"]: i for i in review.list_items(status, "recategorize")}


def category(fa, txn_id):
    return next(r["category"] for r in fa.rows if r["id"] == txn_id)


def test_window_defaults_to_days_before_end():
    assert recategorize.window(end="2026-09-30", days=30) == ("2026-08-31", "2026-09-30")
    assert recategorize.window("2026-01-01", "2026-02-01") == ("2026-01-01", "2026-02-01")


def test_scan_queues_and_apply_writes_in_one_call_and_learns():
    fa = fake(("GRAB* RIDE", "Grab"), ("GRAB* RIDE 2", "Grab"), ("SHOPEE SG", "Shopee"))
    r = scan(fa)
    assert (r["scanned"], r["proposed"], r["queued"], r["auto_applied"]) == (3, 3, 3, 0)
    assert all(c is None for c in (category(fa, "t1"), category(fa, "t2"), category(fa, "t3")))   # nothing written yet

    items = queued()
    res = recategorize.apply(fa, [items["t1"]["id"], items["t2"]["id"]])
    assert sorted(res["applied"]) == sorted([items["t1"]["id"], items["t2"]["id"]]) and res["failed"] == []
    assert [c for c in fa.calls if c[0] == "categorize"] == [("categorize", ["t1", "t2"])]   # one call
    assert category(fa, "t1") == category(fa, "t2") == "c-tpt"
    assert all(i["status"] == "done" and i["applied"]["ok"] for i in res["items"])

    # Grab → Transport approved twice (LEARN_AFTER) → the next Grab row applies itself on scan
    fa.add("acc", "2026-09-02", -900, "Grab", imported_payee="GRAB* RIDE 3")
    r = scan(fa)
    assert r["auto_applied"] == 1 and category(fa, "t4") == "c-tpt"


def test_apply_supersedes_a_row_recategorised_in_actual_meanwhile():
    fa = fake(("GRAB* RIDE", "Grab"), ("SHOPEE SG", "Shopee"))
    scan(fa)
    items = queued()
    fa.rows[0]["category"] = "c-sal"           # you fixed t1 by hand in Actual after the scan
    res = recategorize.apply(fa, [items["t1"]["id"], items["t2"]["id"]])
    assert res["superseded"] == [items["t1"]["id"]] and res["applied"] == [items["t2"]["id"]]
    assert category(fa, "t1") == "c-sal"       # not overwritten
    assert ("categorize", ["t2"]) in fa.calls
    assert review.get(items["t1"]["id"])["status"] == "superseded"
    assert not any("grab" in m["key"] for m in review.memory())    # nothing learned from it


def test_partial_failure_fails_only_its_own_item():
    fa = fake(("GRAB* RIDE", "Grab"), ("SHOPEE SG", "Shopee"), ("SHOPEE SG 2", "Shopee"))
    scan(fa)
    items = queued()
    fa.fail_updates["t2"] = "transaction is locked"
    res = recategorize.apply(fa, [it["id"] for it in items.values()])
    assert res["failed"] == [items["t2"]["id"]]
    assert sorted(res["applied"]) == sorted([items["t1"]["id"], items["t3"]["id"]])
    by_id = {i["id"]: i for i in res["items"]}
    assert by_id[items["t2"]["id"]]["status"] == "failed"
    assert by_id[items["t2"]["id"]]["result"]["errors"] == ["transaction is locked"]
    assert by_id[items["t1"]["id"]]["result"] == {"ok": True, "errors": []}
    assert category(fa, "t1") == "c-tpt" and category(fa, "t2") is None and category(fa, "t3") == "c-food"

    # a failed item can be approved again once Actual takes it
    del fa.fail_updates["t2"]
    assert recategorize.apply(fa, [items["t2"]["id"]])["applied"] == [items["t2"]["id"]]


def test_gateway_down_on_write_fails_every_item_with_the_reason(monkeypatch):
    fa = fake(("GRAB* RIDE", "Grab"), ("SHOPEE SG", "Shopee"))
    scan(fa)
    items = queued()

    def boom(updates):
        raise actual_gateway.GatewayError("actual-bridge unreachable")
    monkeypatch.setattr(fa, "categorize", boom)
    res = recategorize.apply(fa, [it["id"] for it in items.values()])
    assert len(res["failed"]) == 2
    assert all(i["result"]["errors"] == ["actual-bridge unreachable"] for i in res["items"])


def test_override_category_and_keep():
    fa = fake(("GRAB* RIDE", "Grab"), ("SHOPEE SG", "Shopee"))
    scan(fa)
    items = queued()
    with pytest.raises(ValueError):
        recategorize.apply(fa, [items["t1"]["id"]], category_id="c-nope")
    res = recategorize.apply(fa, [items["t1"]["id"]], category_id="c-sal")
    assert category(fa, "t1") == "c-sal" and res["items"][0]["payload"]["proposed"]["source"] == "manual"
    res = recategorize.apply(fa, [items["t2"]["id"]], decision="keep")
    assert res["items"][0]["status"] == "done" and category(fa, "t2") is None


def test_refresh_supersedes_fixed_reproposes_changed_and_promotes_learned():
    fa = fake(("GRAB* RIDE", "Grab"), ("SHOPEE SG", "Shopee"), ("SHOPEE SG 2", "Shopee"),
              ("SHOPEE SG 3", "Shopee"))
    assert scan(fa)["queued"] == 4

    # meanwhile: t1 categorised by hand in Actual; your rule for Shopee now says Salary
    fa.rows[0]["category"] = "c-tpt"
    fa.rules = [rule("grab", "c-tpt"), rule("shopee", "c-sal")]
    r = recategorize.refresh(fa, use_llm=False)
    assert (r["fixed_elsewhere"], r["changed"], r["auto_applied"], r["pending"]) == (1, 3, 0, 3)
    assert {i["payload"]["proposed"]["id"] for i in queued().values()} == {"c-sal"}
    assert fa.calls == []                      # refresh wrote nothing

    # approve two Shopee rows as Salary → learned; refresh promotes and applies the third
    items = queued()
    recategorize.apply(fa, [items["t2"]["id"], items["t3"]["id"]])
    r = recategorize.refresh(fa, use_llm=False)
    assert r["auto_applied"] == 1 and r["pending"] == 0 and category(fa, "t4") == "c-sal"


def test_accept_confident_only_takes_high_confidence():
    fa = fake(("GRAB* RIDE", "Grab"), ("SHOPEE SG", "Shopee"))
    scan(fa)
    items = queued()
    review.set_payload(items["t2"]["id"], {**items["t2"]["payload"],
                                           "proposed": {**items["t2"]["payload"]["proposed"], "confidence": 0.3}})
    res = recategorize.accept_confident(fa, 0.8)
    assert res["applied"] == [items["t1"]["id"]] and set(queued()) == {"t2"}


def test_routes_go_through_the_module(monkeypatch):
    from fastapi.testclient import TestClient
    import main
    fa = fake(("GRAB* RIDE", "Grab"), ("SHOPEE SG", "Shopee"), ("SHOPEE SG 2", "Shopee"))
    monkeypatch.setattr(actual_gateway, "default", lambda: fa)
    c = TestClient(main.app)

    assert c.post("/review/recategorize/scan", json={"start": "2026-08-01", "end": "2026-09-30",
                                                     "use_llm": False}).json()["queued"] == 3
    items = queued()
    r = c.post(f"/review/{items['t1']['id']}/decide", json={"decision": "apply", "category_id": "c-sal"}).json()
    assert r["status"] == "done" and r["applied"]["ok"] and category(fa, "t1") == "c-sal"
    assert c.post(f"/review/{items['t2']['id']}/decide",
                  json={"decision": "apply", "category_id": "c-nope"}).status_code == 400

    fa.fail_updates["t3"] = "locked"
    r = c.post("/review/recategorize/apply-many", json={"ids": [items["t2"]["id"], items["t3"]["id"]]}).json()
    assert r == {"applied": 1, "failed": [items["t3"]["id"]], "superseded": []}

    r = c.post("/review/recategorize/refresh", json={"use_llm": False}).json()
    assert r["checked"] == 0
    assert c.post("/review/accept-ai", json={"min_confidence": 0.8}).json()["applied"] == 0


def test_http_gateway_categorize_reads_per_row_failures(monkeypatch):
    seen = []

    def fake_call(method, path, body=None, timeout=120):
        seen.append((path, body))
        return {"ok": True, "updated": 1, "errors": ["t2: locked: yes"], "failed": [{"id": "t2", "error": "locked: yes"}]}
    monkeypatch.setattr(bridge_client, "call", fake_call)
    ups = [{"id": "t1", "category": "c"}, {"id": "t2", "category": "c"}]
    assert actual_gateway.HttpGateway().categorize(ups) == {"t2": "locked: yes"}
    assert seen == [("/txns/update", {"updates": ups})]
