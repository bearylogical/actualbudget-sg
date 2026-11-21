"""Card-ness is decided once (accounts.is_card), and the bridge is sent signed cents only."""
import accounts as acct
import bridge_client
import reconcile
from accounts import StatementInfo
from fake_actual import FakeActual


def test_is_card_statement_first_then_account():
    assert acct.is_card(info=StatementInfo(kind="credit_card"))
    assert acct.is_card(info=StatementInfo(balance_is_owed=True))
    # a deposit statement wins over a card-ish account name
    assert not acct.is_card({"id": "a", "name": "UOB One Card"}, info=StatementInfo(kind="deposit"))
    assert acct.is_card({"id": "a", "name": "UOB One Card"}, info=StatementInfo())
    assert acct.is_card({"id": "a", "name": "KrisFlyer"}, card_ids=set())
    assert not acct.is_card({"id": "a", "name": "POSB Savings"}, card_ids=set())
    assert acct.is_card({"id": "a", "name": "Spending"}, card_ids={"a"})
    assert not acct.is_card(None)


def test_card_ids_come_from_remembered_card_fingerprints():
    acct.remember("UOB|credit_card|2583", "acc-card", "Spending")
    acct.remember("UOB|deposit|1111", "acc-dep", "Main")
    assert acct.card_account_ids() == {"acc-card"}
    assert acct.is_card({"id": "acc-card", "name": "Spending"})


def test_owed_balance_is_negative_in_actual():
    assert acct.balance_cents(120.5, True) == -12050
    assert acct.balance_cents(120.5, False) == 12050
    assert reconcile.expected_balance(StatementInfo(kind="credit_card", balance=50))[0] == -5000
    assert reconcile.expected_balance(StatementInfo(kind="deposit", balance=50))[0] == 5000


def test_bank_balance_sign_decided_by_backend():
    """The MCP reconcile tool sends what the bank shows; the backend knows it's a card."""
    fake = FakeActual(accounts=[{"id": "c", "name": "Citi Rewards Visa"}, {"id": "s", "name": "POSB Savings"}])
    fake.add("c", "2026-09-01", -5000, "Shop", "i1")
    fake.add("s", "2026-09-01", 5000, "Salary", "i2")
    for account in ("c", "s"):
        r = reconcile.run(account, StatementInfo(period_end="2026-09-30"), [], bank_balance=50, use_llm=False,
                          gateway=fake)["report"]
        assert r["reconciled"] and r["gap"] == 0
        assert r["kind"] == ("credit_card" if account == "c" else "deposit")


def test_bridge_import_body_is_signed_cents(monkeypatch):
    sent = {}

    def call(method, path, body=None, timeout=None):
        sent.update(body)
        return {"ok": True, "added": 0, "toVerify": [{**body["rows"][0], "reason": "twin"}]}
    monkeypatch.setattr(bridge_client, "call", call)
    stmt = {"date": "2026-09-05", "description": "REFUND", "payee": "Shop", "amount": 30, "is_credit": True,
            "imported_id": "x", "splits": [{"amount": 20}, {"amount": 10}]}
    r = bridge_client.import_rows("acc", [stmt], dry_run=True)
    assert "transactions" not in sent
    assert sent["rows"][0]["amount"] == 3000 and "is_credit" not in sent["rows"][0]
    assert [s["amount"] for s in sent["rows"][0]["subtransactions"]] == [2000, 1000]
    assert r["toVerify"] == [{**stmt, "reason": "twin"}]
