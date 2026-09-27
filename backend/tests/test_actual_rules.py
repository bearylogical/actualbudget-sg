from actual_rules import ActualContext, apply_rules

CATS = [{"id": "c-groc", "name": "Groceries"}, {"id": "c-food", "name": "Food"},
        {"id": "c-shop", "name": "Shopping"}]
PAYEES = [{"id": "p-ss", "name": "Sheng Siong"}, {"id": "p-grab", "name": "Grab"}]


def txn(**kw):
    base = {"imported_payee": "", "payee": "", "payee_id": None, "notes": "", "amount_cents": -1000, "account_id": "a1"}
    base.update(kw)
    return base


def test_payee_is_rule():
    ctx = ActualContext(CATS, PAYEES, [{"id": "r1", "stage": None, "conditionsOp": "and",
        "conditions": [{"field": "payee", "op": "is", "value": "p-ss"}],
        "actions": [{"field": "category", "op": "set", "value": "c-groc"}]}])
    assert apply_rules(txn(payee="Sheng Siong"), ctx)["category_id"] == "c-groc"
    assert apply_rules(txn(payee="Other"), ctx)["category_id"] is None


def test_pre_rename_then_category():
    ctx = ActualContext(CATS, PAYEES, [
        {"id": "cat", "stage": None, "conditions": [{"field": "payee", "op": "is", "value": "p-grab"}],
         "actions": [{"field": "category", "op": "set", "value": "c-food"}]},
        {"id": "pre", "stage": "pre", "conditions": [{"field": "imported_payee", "op": "matches", "value": "^grab\\*"}],
         "actions": [{"field": "payee", "op": "set", "value": "p-grab"}]},
    ])
    out = apply_rules(txn(imported_payee="GRAB*A-123 SINGAPORE SG", payee="A-123"), ctx)
    assert out == {"payee": "Grab", "category_id": "c-food", "rule_ids": ["pre", "cat"]}


def test_unsupported_rule_is_skipped_not_guessed():
    ctx = ActualContext(CATS, PAYEES, [{"id": "r", "stage": None,
        "conditions": [{"field": "date", "op": "is", "value": "2026-01-01"}],
        "actions": [{"field": "category", "op": "set", "value": "c-shop"}]}])
    assert apply_rules(txn(), ctx)["category_id"] is None


def test_amount_outflow_and_or():
    ctx = ActualContext(CATS, PAYEES, [{"id": "r", "conditionsOp": "or",
        "conditions": [{"field": "amount", "op": "gt", "value": 5000, "options": {"outflow": True}},
                       {"field": "imported_payee", "op": "contains", "value": "ikea"}],
        "actions": [{"field": "category", "op": "set", "value": "c-shop"}]}])
    assert apply_rules(txn(amount_cents=-6000), ctx)["category_id"] == "c-shop"
    assert apply_rules(txn(amount_cents=-100, imported_payee="IKEA TAMPINES"), ctx)["category_id"] == "c-shop"
    assert apply_rules(txn(amount_cents=-100), ctx)["category_id"] is None
