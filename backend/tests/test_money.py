"""money.py: the one place a Statement row's abs amount + is_credit becomes Actual's signed cents."""
import money


def row(amount, is_credit=False, **extra):
    return {"date": "2026-09-05", "description": "RAW TEXT", "payee": "Shop", "amount": amount,
            "is_credit": is_credit, "imported_id": "x", **extra}


def test_signed_cents_money_in_is_positive():
    assert money.signed_cents(row(1.8)) == -180
    assert money.signed_cents(row(100, True)) == 10000
    assert money.signed_cents(row(0.1 + 0.2)) == -30          # rounds, doesn't truncate
    assert money.statement_amount(-180) == (1.8, False)
    assert money.statement_amount(10000) == (100.0, True)


def test_to_actual_row():
    a = money.to_actual(row(12.5, category_id="c1", legacy_ids=["old"], notes="n"))
    assert a == {"date": "2026-09-05", "amount": -1250, "imported_payee": "RAW TEXT", "payee_name": "Shop",
                 "notes": "n", "cleared": True, "imported_id": "x", "legacy_ids": ["old"], "category": "c1"}
    assert money.to_actual(row(1, payee=""))["payee_name"] == "RAW TEXT"


def test_split_lines_follow_the_parent_direction():
    """The bridge used to negate every split line, so a split refund didn't add up to its parent."""
    refund = money.to_actual(row(30, True, splits=[{"amount": 20, "category_id": "c1"}, {"amount": 10}]))
    assert refund["amount"] == 3000
    assert [s["amount"] for s in refund["subtransactions"]] == [2000, 1000]
    assert refund["subtransactions"][0]["category"] == "c1"
    spend = money.to_actual(row(30, splits=[{"amount": 20}, {"amount": 10}]))
    assert [s["amount"] for s in spend["subtransactions"]] == [-2000, -1000]
    assert sum(s["amount"] for s in spend["subtransactions"]) == spend["amount"]
    assert "subtransactions" not in money.to_actual(row(30, splits=[{"amount": 30}]))


def test_restate_import_hands_back_statement_rows():
    rows = [row(8.5, imported_id="a"), row(2, True, imported_id="b")]
    r = money.restate_import(rows, {
        "toVerify": [{**money.to_actual(rows[0]), "reason": "twin"}],
        "unreviewed": [{"imported_id": "b", "date": "2026-09-05", "imported_payee": "RAW TEXT", "amount": 200,
                        "reason": "twin"}],
        "added": 0})
    assert r["toVerify"] == [{**rows[0], "reason": "twin"}]
    assert r["unreviewed"] == [{"imported_id": "b", "date": "2026-09-05", "description": "RAW TEXT",
                                "amount": 2, "reason": "twin"}]
    assert r["added"] == 0
