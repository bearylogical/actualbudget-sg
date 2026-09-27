import pandas as pd
import pytest

import accounts as acct
from categorizer import categorize
from parsers import parse_statement, _uob_account_parts


def uob_card_df():
    return pd.DataFrame([
        ["United Overseas Bank Limited. Company Reg No. 193500026Z", None, None, None, None, None, None],
        ["Account Number:", "4265884082432583", None, None, None, None, None],
        ["Account Type:", "UOB ONE CARD", None, None, None, None, None],
        ["Statement Date:", "04 Jun 2026 To 27 Sep 2026", None, None, None, None, None],
        ["Statement Balance:", "1523.73", "SGD", None, None, None, None],
        ["Transaction Date", "Posting Date", "Description", "Foreign Currency Type",
         "Transaction Amount(Foreign)", "Local Currency Type", "Transaction Amount(Local)"],
        ["27 Sep 2026", None, "SHOPEE SG MP             +65 6206 661SG \nRef No: 1", None, None, "SGD", 70.67],
    ])


def uob_acct_df():
    return pd.DataFrame([
        ["United Overseas Bank Limited. Company Reg No. 193500026Z", None, None, None, None],
        ["Account Number:", "7613634421", None, None, None],
        ["Account Type:", "One Account", None, None, None],
        ["Statement Period:", "29 Jul 2026 To 27 Sep 2026", None, None, None],
        ["Transaction Date", "Transaction Description", "Withdrawal", "Deposit", "Available Balance"],
        ["26 Sep 2026", "Cash Withdrawal-ATM 4265884082432583", 200, 0, 21800.15],
        ["22 Sep 2026", "PAYNOW-FAST OTHR Transfer - UEN PIB2609051719237136 BREAD HAVEN PTE. LT", 7.5, 0, 22000.15],
    ])


def to_xlsx(df):
    import io
    buf = io.BytesIO()
    df.to_excel(buf, header=False, index=False)
    return buf.getvalue()


POSB = b'''"Account Details For:","personal 136-51134-3",,,,,,,
"Statement as at:","27 Sep 2026",,,,,,,
"Available Balance:","SGD 1357.18",,,,,,,
"Ledger Balance:","SGD 1357.18",,,,,,,
"Transaction Date","Transaction Code","Description","Transaction Ref1","Transaction Ref2","Transaction Ref3","Status","Debit Amount","Credit Amount"
"07 Sep 2026","MST","NETS AUTO TOP UP 1","NETS AUTO TOP UP 05SEP","5548","0001","Settled",40,""
'''


def test_statement_info_for_each_export():
    _, bank, card = parse_statement(to_xlsx(uob_card_df()), "CC_TXN_History_1.csv")
    assert (bank, card.kind, card.last4, card.balance, card.balance_is_owed) == ("UOB", "credit_card", "2583", 1523.73, True)
    assert card.fingerprint == "UOB|credit_card|2583" and card.period_start == "2026-06-04"

    _, bank, sav = parse_statement(to_xlsx(uob_acct_df()), "ACC_TXN_History_1.csv")
    assert (bank, sav.kind, sav.last4, sav.account_type, sav.balance) == ("UOB", "deposit", "4421", "One Account", 21800.15)

    _, bank, posb = parse_statement(POSB, "transaction_history.csv")
    assert (bank, posb.kind, posb.last4, posb.account_type, posb.balance) == ("DBS/POSB", "deposit", "1343", "personal", 1357.18)


ACCOUNTS = [
    {"id": "a-card", "name": "UOB One Card", "balance": -100000},
    {"id": "a-uob", "name": "UOB One Account", "balance": 2180015},
    {"id": "a-posb", "name": "POSB Savings", "balance": 50},
    {"id": "a-wise", "name": "Wise SGD", "balance": 0},
    {"id": "a-old", "name": "Old UOB Card", "closed": True},
]


def info(**kw):
    base = dict(bank="UOB", kind="credit_card", account_type="UOB ONE CARD", last4="2583")
    return acct.StatementInfo(**{**base, **kw})


def test_card_goes_to_card_account():
    r = acct.recommend(info(), [], ACCOUNTS, memory={})
    assert r["recommended"] == "a-card" and r["auto_select"]
    assert all(s["account_id"] != "a-old" for s in r["suggestions"])       # closed accounts never suggested


def test_savings_goes_to_one_account_by_name_and_balance():
    r = acct.recommend(info(kind="deposit", account_type="One Account", last4="4421", balance=21800.15),
                       [], ACCOUNTS, memory={})
    assert r["recommended"] == "a-uob" and r["auto_select"]
    assert any("balance matches" in x for x in r["suggestions"][0]["reasons"])


def test_posb_nickname_account():
    accts = ACCOUNTS + [{"id": "a-personal", "name": "Personal (POSB)", "balance": 0}]
    r = acct.recommend(info(bank="DBS/POSB", kind="deposit", account_type="personal", last4="1343"),
                       [], accts, memory={})
    assert r["recommended"] == "a-personal"


def test_memory_beats_names_and_overlap_counts():
    r = acct.recommend(info(), [], ACCOUNTS, memory={"UOB|credit_card|2583": {"account_id": "a-wise"}})
    assert r["recommended"] == "a-wise" and r["auto_select"]
    txns = [{"imported_id": f"ref-{i}"} for i in range(10)]
    r = acct.recommend(info(kind="unknown", account_type="", last4=""), txns,
                       [{"id": "x", "name": "Account A"}, {"id": "y", "name": "Account B"}],
                       matches={"y": {"matched": 8}}, memory={})
    assert r["recommended"] == "y" and "8 of these" in r["suggestions"][0]["reasons"][0]


def test_ambiguous_is_not_auto_selected():
    accts = [{"id": "c1", "name": "UOB Card"}, {"id": "c2", "name": "UOB Card 2"}]
    r = acct.recommend(info(last4=""), [], accts, memory={})
    assert not r["auto_select"]


def test_remember_forget_roundtrip():
    acct.remember("UOB|credit_card|2583", "a-card", "UOB One Card")
    acct.remember("UOB|credit_card|2583", "a-card", "UOB One Card")
    assert acct.load_map()["UOB|credit_card|2583"]["count"] == 2
    acct.forget("UOB|credit_card|2583")
    assert acct.load_map() == {}
    acct.remember("UOB|unknown|?", "x")
    assert acct.load_map() == {}


def test_match_counts_uses_legacy_ids():
    txns = [{"imported_id": "ref-1", "legacy_ids": ["h1"]}, {"imported_id": "ref-2"}]
    assert acct.match_counts(txns, [{"id": "a", "matchedIds": ["h1"]}]) == {"a": {"matched": 1}}


@pytest.mark.parametrize("desc,name,ref,kind", [
    ("PAYNOW-FAST OTHR Transfer - UEN PIB2609051719237136 BREAD HAVEN PTE. LT", "BREAD HAVEN PTE. LT", "PIB2609051719237136", "expense"),
    ("PAYNOW-FAST OTHR bibik PIB2608131428044548 Yani", "Yani", "PIB2608131428044548", "p2p"),
    ("PAYNOW-FAST NORDIC GREEN PTE LT MBK2609191894242420", "NORDIC GREEN PTE LT", "MBK2609191894242420", "expense"),
    ("NETS Debit-Consumer KOUFU PTE L12358500 xxxxxx4421", "KOUFU PTE L", None, "expense"),
    ("Bill Payment Bill payment mBK-UCF 8028300107", "UCF", None, "expense"),
])
def test_uob_account_parts(desc, name, ref, kind):
    n, r, text = _uob_account_parts(desc)
    assert (n, r) == (name, ref)
    assert categorize(text, False)["kind"] == kind
