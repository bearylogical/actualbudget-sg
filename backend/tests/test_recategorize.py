import review
import recategorize
import bridge_client
from actual_rules import ActualContext

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
    assert bridge_client.actions_for({"kind": "recategorize", "decision": "apply", "payload": p}) == \
        [{"type": "categorize", "updates": [{"id": "t1", "category": "c-tpt", "notes": "#llm"}]}]
    assert bridge_client.actions_for({"kind": "recategorize", "decision": "keep", "payload": p}) == []

    # two identical approvals for the same payee → the third proposal resolves itself
    for i in (1, 2):
        it = review.enqueue("recategorize", {**p, "txn": {**p["txn"], "id": f"t{i}"}}, [f"t{i}", "c-tpt"])
        assert it["status"] == "pending"
        review.decide(it["id"], "apply")
    it = review.enqueue("recategorize", {**p, "txn": {**p["txn"], "id": "t3"}}, ["t3", "c-tpt"])
    assert it["status"] == "auto" and it["decision"] == "apply"


def test_scan_decide_override_and_bulk_accept(monkeypatch):
    from fastapi.testclient import TestClient
    import main
    calls = []

    def fake_call(method, path, body=None, timeout=120):
        calls.append((path, body))
        if path == "/context":
            return {"categories": CATS, "payees": [], "rules": [], "accounts": []}
        if path == "/txns/range":
            return {"transactions": [row(1, "GRAB* RIDE"), row(2, "SHOPEE SINGAPORE")], "accounts": []}
        if path == "/txns/update":
            return {"ok": True, "updated": len(body["updates"]), "errors": []}
        raise AssertionError(path)

    async def fake_bridge(method, path, body=None, timeout=30):
        return fake_call(method, path, body)

    monkeypatch.setattr(bridge_client, "call", fake_call)
    monkeypatch.setattr(main, "_bridge", fake_bridge)
    # deterministic proposals, no LLM
    monkeypatch.setattr(recategorize, "propose", lambda rows, c, **kw: [
        {"account": "acc", "txn": r, "current": {"id": None, "name": None},
         "proposed": {"id": cid, "name": n, "source": "seed", "confidence": conf}, "reason": "known merchant"}
        for r, (cid, n, conf) in zip(rows, [("c-tpt", "Transport", 0.9), ("c-food", "Food", 0.5)])])
    c = TestClient(main.app)

    r = c.post("/review/recategorize/scan", json={"days": 30}).json()
    assert r["queued"] == 2
    items = {i["payload"]["txn"]["id"]: i for i in c.get("/review?kind=recategorize").json()["items"]}

    # override: apply Salary instead of Food
    r = c.post(f"/review/{items['t2']['id']}/decide", json={"decision": "apply", "category_id": "c-sal"}).json()
    assert r["status"] == "done" and calls[-1] == ("/txns/update", {"updates": [{"id": "t2", "category": "c-sal"}]})

    # bulk accept ≥ 0.8 picks up the 0.9 Transport proposal in one bridge call
    r = c.post("/review/accept-ai", json={"min_confidence": 0.8}).json()
    assert r["applied"] == 1
    assert calls[-1] == ("/txns/update", {"updates": [{"id": "t1", "category": "c-tpt"}]})
    assert c.get("/review?kind=recategorize").json()["items"] == []


def test_refresh_drops_fixed_updates_changed_and_apply_many_learns(monkeypatch):
    from fastapi.testclient import TestClient
    import main
    calls = []
    actual = {"t1": row(1, "GRAB* RIDE"), "t2": row(2, "SHOPEE SINGAPORE"), "t3": row(3, "SHOPEE SG"),
              "t4": row(4, "SHOPEE SG 2")}
    for k, r in actual.items():
        r["payee"] = "Shopee" if k != "t1" else "Grab"
    proposal = {"t1": ("c-tpt", "Transport"), "t2": ("c-food", "Food"), "t3": ("c-food", "Food"),
                "t4": ("c-food", "Food")}

    def fake_call(method, path, body=None, timeout=120):
        calls.append((path, body))
        if path == "/context":
            return {"categories": CATS, "payees": [], "rules": [], "accounts": []}
        if path == "/txns/range":
            return {"transactions": list(actual.values()), "accounts": []}
        if path == "/txns/update":
            return {"ok": True, "updated": len(body["updates"]), "errors": []}
        raise AssertionError(path)

    async def fake_bridge(method, path, body=None, timeout=30):
        return fake_call(method, path, body)

    monkeypatch.setattr(bridge_client, "call", fake_call)
    monkeypatch.setattr(main, "_bridge", fake_bridge)
    monkeypatch.setattr(recategorize, "propose", lambda rows, c, **kw: [
        {"account": "acc", "txn": r, "current": {"id": r.get("category"), "name": None},
         "proposed": {"id": proposal[r["id"]][0], "name": proposal[r["id"]][1], "source": "seed", "confidence": 0.9},
         "reason": "x"} for r in rows if not r.get("category")])
    c = TestClient(main.app)
    assert c.post("/review/recategorize/scan", json={"days": 30}).json()["queued"] == 4

    # meanwhile: t1 categorised by hand in Actual; the best idea for Shopee became Salary
    actual["t1"] = {**actual["t1"], "category": "c-tpt"}
    for k in ("t2", "t3", "t4"):
        proposal[k] = ("c-sal", "Salary")
    r = c.post("/review/recategorize/refresh", json={}).json()
    assert (r["fixed_elsewhere"], r["changed"], r["auto_applied"], r["pending"]) == (1, 3, 0, 3)
    items = c.get("/review?kind=recategorize").json()["items"]
    assert {i["payload"]["proposed"]["id"] for i in items} == {"c-sal"}

    # approve two Shopee rows at once with a different category → learned (LEARN_AFTER=2) …
    ids = [i["id"] for i in items if i["payload"]["txn"]["id"] in ("t2", "t3")]
    r = c.post("/review/recategorize/apply-many", json={"ids": ids, "category_id": "c-food"}).json()
    assert r["applied"] == 2 and calls[-1][0] == "/txns/update"
    # … but t4 is still proposed as Salary, so the pattern (Shopee → Food) doesn't match it;
    # a refresh that now proposes Food for it applies it on its own.
    proposal["t4"] = ("c-food", "Food")
    r = c.post("/review/recategorize/refresh", json={}).json()
    assert r["auto_applied"] == 1 and r["pending"] == 0
