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


def test_main_audit_is_the_rules_audit_function():
    # the MCP audit-trail module must not shadow rules_audit.audit, which /rules/audit* call
    import main
    import rules_audit
    assert main.audit is rules_audit.audit


# ── Rules sync (rules_audit.sync) against FakeActual ─────────────────────────

import pytest  # noqa: E402

import actual_gateway  # noqa: E402
import rules_audit  # noqa: E402
from fake_actual import FakeActual  # noqa: E402
from taxonomy import load_aliases  # noqa: E402


def _fake():
    gw = FakeActual(categories=[dict(c) for c in CATS])
    gw.payees = [dict(p) for p in PAYEES] + [{"id": "p-nf", "name": "NETFLIX.COM"}]
    gw.rules = [rule("r1", "p-ss", "c-shop"), rule("r2", "p-ss", "c-shop")]   # r2 duplicate
    return gw


def _applied(gw):
    return [c[1] for c in gw.calls if c[0] == "apply_rules"]


ALL = {p: True for p in rules_audit.PARTS}


def test_sync_dry_run_creates_nothing():
    gw = _fake()
    out = rules_audit.sync(gw, ALL, dry_run=True)
    assert out["dryRun"] is True and "result" not in out
    assert out["counts"]["createRules"] > 0 and out["counts"]["deleteRules"] == 1
    assert _applied(gw) == [] and load_aliases() == {}


def test_sync_skips_rule_that_needs_an_unselected_category():
    gw = _fake()
    with_cats = rules_audit.sync(gw, {"rules": True, "categories": True})["plan"]["createRules"]
    needs_cat = [r for r in with_cats if rules_audit._needs_new_category(r)]
    assert any("Netflix" in r["why"] for r in needs_cat)          # Subscriptions isn't in Actual yet
    out = rules_audit.sync(gw, {"rules": True}, dry_run=False)
    sent = _applied(gw)[0]["createRules"]
    assert sent and not any(rules_audit._needs_new_category(r) for r in sent)
    assert len(sent) == len(with_cats) - len(needs_cat)
    assert out["result"]["rules"] == len(sent) and "summary_after" in out


def test_sync_only_aliases_saves_aliases_and_applies_nothing():
    gw = FakeActual(categories=[{"id": "c-x", "name": "Subscription"}])
    plan = rules_audit.audit(ActualContext(gw.categories, [], []))["plan"]
    assert plan["aliases"] == {"Subscriptions": "Subscription"}
    out = rules_audit.sync(gw, {"aliases": True}, dry_run=False)
    assert _applied(gw) == [] and out["result"] == {"ok": True}
    assert load_aliases() == {**plan["aliases"]}
    assert out["counts"]["aliases"] == len(plan["aliases"]) and out["counts"]["createRules"] == 0


@pytest.mark.parametrize("flags,parts", [
    (["--all"], {p: p != "broken" for p in rules_audit.PARTS}),
    (["--rules", "--merge-payees", "--broken"], {"rules": True, "merge_payees": True, "broken": True}),
    (["--aliases"], {"aliases": True}),
])
def test_cli_and_route_produce_the_same_plan(monkeypatch, capsys, flags, parts):
    from fastapi.testclient import TestClient
    import main
    import rules_cli

    gw = _fake()
    monkeypatch.setattr(actual_gateway, "_default", gw)
    seen = []
    real_sync = rules_audit.sync
    monkeypatch.setattr(rules_cli, "sync", lambda *a, **k: seen.append(real_sync(*a, **k)) or seen[-1])

    rules_cli.main(["sync", *flags])
    assert "Dry run" in capsys.readouterr().out
    r = TestClient(main.app).post("/rules/sync", json={"dryRun": True, "parts": parts})
    assert r.status_code == 200
    assert r.json()["plan"] == seen[0]["plan"] and r.json()["counts"] == seen[0]["counts"]
    assert _applied(gw) == []


def test_route_without_budget_is_400(monkeypatch):
    from fastapi.testclient import TestClient
    import main
    monkeypatch.setattr(actual_gateway, "_default", FakeActual(loaded=False))
    assert TestClient(main.app).post("/rules/sync", json={}).status_code == 400
