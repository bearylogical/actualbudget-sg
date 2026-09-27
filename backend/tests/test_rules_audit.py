from actual_rules import ActualContext
from rules_audit import audit, to_markdown

CATS = [{"id": "c-food", "name": "Food & Dining"}, {"id": "c-groc", "name": "Groceries"},
        {"id": "c-shop", "name": "Shopping"}]
PAYEES = [{"id": "p-ss", "name": "Sheng Siong"},
          {"id": "p-raw1", "name": "GRAB*A-9RNPVF SINGAPORE SG"},
          {"id": "p-raw2", "name": "Grab* A-9ROWEG Singapore SG"},
          {"id": "p-kopi", "name": "KOPITIAM SINGAPORE SG"},
          {"id": "p-acct", "name": "POSB", "transfer_acct": "a1"}]


def rule(rid, payee, cat, stage=None):
    return {"id": rid, "stage": stage, "conditionsOp": "and",
            "conditions": [{"field": "payee", "op": "is", "value": payee}],
            "actions": [{"field": "category", "op": "set", "value": cat}]}


def test_audit_findings_and_plan():
    rules = [rule("r1", "p-ss", "c-shop"), rule("r2", "p-ss", "c-shop"),          # duplicate
             rule("r3", "p-kopi", "c-food"), rule("r4", "p-kopi", "c-groc"),      # conflict
             rule("r5", "p-ss", "c-deleted")]                                    # broken (+conflict)
    rep = audit(ActualContext(CATS, PAYEES, rules))
    s = rep["summary"]
    assert s["duplicates"] == 1 and rep["plan"]["delete_rules"] == ["r2"]
    assert s["broken"] == 1 and rep["plan"]["delete_broken_rules"] == ["r5"]
    assert s["conflicts"] >= 1
    grab = next(g for g in rep["raw_payees"] if g["target"] == "Grab")
    assert {p["id"] for p in grab["payees"]} == {"p-raw1", "p-raw2"} and grab["target_id"] is None
    assert all(p["id"] != "p-acct" for g in rep["raw_payees"] for p in g["payees"])  # transfer payees untouched
    # Sheng Siong already has a category rule → no new category rule for it
    whys = [r["why"] for r in rep["plan"]["create_rules"]]
    assert not any("categorise payee 'Sheng Siong'" in w for w in whys)
    assert any("payee 'Grab'" in w for w in whys)
    # disagreement: Actual says Shopping, seed says Groceries
    assert {"payee": "Sheng Siong", "actual_category": "Shopping", "seed_category": "Groceries"} in rep["disagreements"]
    assert "Actual rules audit" in to_markdown(rep)


def test_missing_category_uses_ref_or_alias():
    rep = audit(ActualContext(CATS, [{"id": "p", "name": "NETFLIX.COM"}], []))
    netflix = [r for r in rep["plan"]["create_rules"] if "Netflix" in r["why"] and r["stage"] is None][0]
    assert netflix["actions"][0]["value"] == {"$category": "Subscriptions"}
    assert {"name": "Subscriptions", "group": "Bills & Subscriptions", "is_income": False} in rep["plan"]["create_categories"]
    rep2 = audit(ActualContext(CATS, [{"id": "p", "name": "NETFLIX.COM"}], []), aliases={"Subscriptions": "Shopping"})
    netflix2 = [r for r in rep2["plan"]["create_rules"] if "Netflix" in r["why"] and r["stage"] is None][0]
    assert netflix2["actions"][0]["value"] == "c-shop"


def test_no_category_rule_when_merged_raw_payee_has_learned_one():
    payees = [{"id": "p-raw", "name": "GRAB*A-9RNPVF SINGAPORE SG"}]
    rep = audit(ActualContext(CATS, payees, [rule("r", "p-raw", "c-food")]))
    whys = [r["why"] for r in rep["plan"]["create_rules"]]
    assert not any("categorise payee 'Grab'" in w for w in whys)
    assert any("rename matching statement text to payee 'Grab'" in w for w in whys)
