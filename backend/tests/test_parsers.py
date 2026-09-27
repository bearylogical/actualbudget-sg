import io
import pandas as pd
from parsers import parse_uob, parse_bytes, sniff_format


def uob_df():
    rows = [
        ["United Overseas Bank Limited. Company Reg No. 193500026Z", None, None, None, None, None, None],
        ["Account Type:", "UOB ONE CARD", None, None, None, None, None],
        ["Transaction Date", "Posting Date", "Description", "Foreign Currency Type",
         "Transaction Amount(Foreign)", "Local Currency Type", "Transaction Amount(Local)"],
        ["27 Sep 2026", None, "SHOPEE SG MP             +65 6206 661SG \nRef No: 74777156268000100866097",
         None, None, "SGD", 70.67],
        ["26 Sep 2026", None, "ENCIK TAN OTH            SINGAPORE    SG\nRef No: 111", None, None, "SGD", 2.4],
        ["26 Sep 2026", None, "ENCIK TAN OTH            SINGAPORE    SG\nRef No: 222", None, None, "SGD", 2.4],
        ["25 Sep 2026", None, "PAYMT THRU E-BANK/HOMEB/CYBERB (EP52)", None, None, "SGD", -1785.04],
    ]
    return pd.DataFrame(rows)


def test_uob_extracts_bank_ref_and_keeps_legacy_hash():
    tx = parse_uob(uob_df())
    assert len(tx) == 4
    assert tx[0]["imported_id"] == "ref-74777156268000100866097"
    assert tx[0]["description"] == "SHOPEE SG MP +65 6206 661SG"
    assert len(tx[0]["legacy_ids"]) == 1 and len(tx[0]["legacy_ids"][0]) == 16
    # same merchant/amount/day → distinct ids thanks to Ref No
    assert tx[1]["imported_id"] != tx[2]["imported_id"]
    assert tx[1]["payee"] == "Encik Tan Oth"
    assert tx[3]["is_credit"] and tx[3]["kind"] == "transfer"


POSB_CSV = b'''"Account Details For:","personal 136-00000-0",,,,,,,
"Statement as at:","27 Sep 2026",,,,,,,
"",,,,,,,,
"Transaction Date","Transaction Code","Description","Transaction Ref1","Transaction Ref2","Transaction Ref3","Status","Debit Amount","Credit Amount"
"07 Sep 2026","MST","NETS AUTO TOP UP Singapor 05SEP 5548-0000 000003040772564","NETS AUTO TOP UP Singapor     05SEP","5548-0000","000003040772564","Settled",40,""
"07 Sep 2026","MST","NETS AUTO TOP UP Singapor 05SEP 5548-0000 000003040772999","NETS AUTO TOP UP Singapor     05SEP","5548-0000","000003040772999","Settled",40,""
"06 Sep 2026","ICT","Incoming PayNow Ref 9332111 From: JANE TAN OTHR PayNow Transfer","Incoming PayNow Ref 9332111","From: JANE TAN","OTHR PayNow Transfer","Settled","",34
"31 Aug 2026","INT","  ","","","","Settled","",0.06
"30 Aug 2026","MST","FREEBIRD SINGAPORE SGP 29AUG 5548-0000 0001","FREEBIRD SINGAPORE SGP        29AUG","5548-0000","0001","Pending",10,""
'''


def test_posb_csv():
    assert sniff_format(POSB_CSV) == "csv"
    tx, bank = parse_bytes(POSB_CSV, "transaction_history_27092026.csv")
    assert bank == "DBS/POSB"
    assert len(tx) == 4                                   # pending row skipped
    a, b, paynow, interest = tx
    assert a["imported_id"] != b["imported_id"]           # Ref1 alone would collide
    assert "ref-NETS AUTO TOP UP Singapor     05SEP" in a["legacy_ids"]
    assert a["payee"] == "NETS FlashPay"
    assert paynow["payee"] == "Jane Tan" and paynow["kind"] == "p2p" and paynow["is_credit"]
    assert interest["category"] == "Interest & Dividends" and interest["amount"] == 0.06


def test_xls_named_csv_is_sniffed():
    assert sniff_format(b"\xd0\xcf\x11\xe0rest", "CC_TXN_History.csv") == "xls"
    assert sniff_format(b"PK\x03\x04rest", "x.csv") == "xlsx"


def test_finalise_skips_pending_and_suffixes_identical_rows():
    import accounts
    from parsers import _finalise
    rows = [
        {"imported_id": "h", "legacy_ids": [], "pending": False},
        {"imported_id": "h", "legacy_ids": [], "pending": False},
        {"imported_id": "ref-9", "legacy_ids": ["x"], "pending": False},
        {"imported_id": "p", "legacy_ids": [], "pending": True},
    ]
    info = accounts.StatementInfo(bank="UOB")
    out = _finalise(rows, info)
    assert [t["imported_id"] for t in out] == ["h", "h-1", "ref-9"]
    assert out[1]["legacy_ids"] == ["h"]
    assert info.skipped_pending == 1


def test_uob_card_pending_rows_are_held_back():
    from parsers import parse_statement
    import io
    df = uob_df()
    df.iloc[3, 1] = None          # Shopee row: no posting date = pending
    for i in (4, 5, 6):
        df.iloc[i, 1] = "28 Sep 2026"
    buf = io.BytesIO(); df.to_excel(buf, header=False, index=False)
    tx, _, info = parse_statement(buf.getvalue(), "CC_TXN_History.xlsx")
    assert info.skipped_pending == 1 and len(tx) == 3
