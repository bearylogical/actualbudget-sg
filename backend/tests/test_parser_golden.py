"""
Golden-file tests: every supported statement layout, parsed end to end.

Fixtures (synthetic, see fixtures/statements/README.md) go through
`parse_statement`, the same entry point the web upload and the scheduler use.
The parser-owned fields of each row — dates, amounts, descriptions, refs and
the dedup ids — are compared against fixtures/golden/<fixture>.json.
Categoriser output is left out on purpose: rule tweaks shouldn't fail these.

Regenerate after an intentional change:  UPDATE_GOLDEN=1 python -m pytest tests/test_parser_golden.py
"""

import json
import os
from pathlib import Path

import pandas as pd
import pytest

import parsers

FIXTURES = Path(__file__).parent / "fixtures" / "statements"
GOLDEN = Path(__file__).parent / "fixtures" / "golden"
UPDATE = os.getenv("UPDATE_GOLDEN") == "1"

TXN_FIELDS = (
    "date", "posting_date", "description", "merchant", "match_text",
    "amount", "currency", "is_credit", "foreign_amount", "foreign_currency",
    "ref", "imported_id", "legacy_ids", "running_balance",
)
INFO_FIELDS = (
    "bank", "kind", "account_type", "last4", "period_start", "period_end",
    "balance", "balance_is_owed", "skipped_pending",
)

# fixture file → bank label parse_statement should report
CASES = {
    "uob_one_account.csv": "UOB",
    "dbs_legacy.csv": "DBS/POSB",
    "dbs_digibank.csv": "DBS/POSB",
    "ocbc.csv": "OCBC",
    "ocbc_360.csv": "OCBC",
    "uob_card.txt": "UOB",
    "uob_account.txt": "UOB",
}


def _parse(name: str, monkeypatch):
    path = FIXTURES / name
    if path.suffix == ".txt":
        # stand-in for a PDF: skip pypdf, feed the layout text it would return
        text = path.read_text()
        monkeypatch.setattr(parsers, "pdf_text", lambda _content: text)
        return parsers.parse_statement(b"%PDF-fixture", name.replace(".txt", ".pdf"))
    return parsers.parse_statement(path.read_bytes(), name)


def _snapshot(txns, bank, info) -> dict:
    return {
        "bank": bank,
        "info": {k: getattr(info, k) for k in INFO_FIELDS},
        "transactions": [{k: t[k] for k in TXN_FIELDS if k in t} for t in txns],
    }


@pytest.mark.parametrize("name", sorted(CASES))
def test_statement_matches_golden(name, monkeypatch):
    txns, bank, info = _parse(name, monkeypatch)
    assert bank == CASES[name]
    actual = _snapshot(txns, bank, info)

    golden_file = GOLDEN / f"{Path(name).stem}.json"
    if UPDATE or not golden_file.exists():
        if not UPDATE:
            pytest.fail(f"no golden file for {name}; run with UPDATE_GOLDEN=1 to create it")
        GOLDEN.mkdir(parents=True, exist_ok=True)
        golden_file.write_text(json.dumps(actual, indent=2, ensure_ascii=False) + "\n")
        return

    expected = json.loads(golden_file.read_text())
    assert actual["info"] == expected["info"]
    assert len(actual["transactions"]) == len(expected["transactions"]), (
        [t["description"] for t in actual["transactions"]]
    )
    for i, (got, want) in enumerate(zip(actual["transactions"], expected["transactions"])):
        assert got == want, f"{name} row {i} ({want.get('description')})"


@pytest.mark.parametrize("name", sorted(CASES))
def test_ids_are_unique_and_stable(name, monkeypatch):
    first, _, _ = _parse(name, monkeypatch)
    again, _, _ = _parse(name, monkeypatch)
    ids = [t["imported_id"] for t in first]
    assert len(ids) == len(set(ids))
    assert ids == [t["imported_id"] for t in again]


# ── behaviour the goldens encode, spelled out ────────────────────────────────


def _rows(name, monkeypatch):
    txns, _, _ = _parse(name, monkeypatch)
    return {t["description"]: t for t in txns}


def test_uob_card_pdf(monkeypatch):
    rows = _rows("uob_card.txt", monkeypatch)
    # summary rows and anything after "End of Transaction Details" are ignored
    assert len(rows) == 5
    assert not any("TOTAL" in d or "BALANCE" in d or "SHOULD NOT" in d for d in rows)
    # December lines on a January statement belong to the previous year
    assert rows["EXAMPLE BOOKSHOP SINGAPORE"]["date"] == "2026-12-27"
    assert rows["BUS/MRT 846451010 SINGAPORE"]["date"] == "2027-01-03"
    stream = rows["STREAMCO.COM AMSTERDAM NL"]
    assert stream["imported_id"] == "ref-74000000000000000000002"
    assert (stream["foreign_currency"], stream["foreign_amount"], stream["amount"]) == ("USD", 14.99, 19.38)
    payment = rows["PAYMT THRU E-BANK/HOMEB/CYBERB (EP52)"]
    assert payment["is_credit"] and payment["amount"] == 1234.56 and payment["ref"] is None


def test_uob_card_pdf_statement_balance(monkeypatch):
    _, _, info = _parse("uob_card.txt", monkeypatch)
    assert (info.balance, info.balance_is_owed) == (62.70, True)   # TOTAL BALANCE line
    assert info.period_end == "2027-01-05"                          # statement date, not last row
    assert parsers.pdf_card_balance("  TOTAL BALANCE FOR UOB ONE CARD    12.00 CR") == -12.00
    assert parsers.pdf_card_balance("no totals here") is None


def test_uob_account_pdf_reads_columns_not_balance(monkeypatch):
    txns, _, info = _parse("uob_account.txt", monkeypatch)
    assert [(t["amount"], t["is_credit"]) for t in txns] == [
        (25.00, False), (6.20, False), (32.15, False), (500.00, True), (120.00, False), (0.42, True),
    ]                                                   # B/F, Total and post-"End" rows skipped
    paynow = txns[0]
    assert paynow["ref"] == "PIB2612020000000011"
    assert (paynow["merchant"], paynow["match_text"]) == ("ALEX EXAMPLE", "PAYNOW ALEX EXAMPLE")
    assert info.last4 == "5678"


def test_uob_account_pdf_and_xls_give_the_same_ids(monkeypatch):
    """The same row from the PDF and the .xls export must dedupe against each other."""
    xls = {t["imported_id"]: t for t in _parse("uob_one_account.csv", monkeypatch)[0]}
    for t in _parse("uob_account.txt", monkeypatch)[0]:
        assert t["imported_id"] in xls, t["description"]
        twin = xls[t["imported_id"]]
        assert (t["description"], t["merchant"], t["match_text"]) == (
            twin["description"], twin["merchant"], twin["match_text"])


def test_uob_account_pdf_keeps_pre_rejoin_ids(monkeypatch):
    """PDF rows used to be described as "LINE1 - LINE2"; those ids stay as legacy ids."""
    nets = next(t for t in _parse("uob_account.txt", monkeypatch)[0] if t["date"] == "2026-12-07")
    old = parsers._hash_id("2026-12-07", "NETS Debit-Consumer - EXAMPLE MART123456789", 32.15, "SGD")
    assert old in nets["legacy_ids"]


def test_uob_xls_picks_up_a_bare_bank_ref():
    df = pd.DataFrame([
        ["Transaction Date", "Transaction Description", "Withdrawal", "Deposit"],
        ["02 Dec 2026", "PAYNOW-FAST PIB2612020000000099 SOMEONE", "5.00", ""],
    ])
    (t,) = parsers.parse_uob(df)
    assert t["imported_id"] == "ref-PIB2612020000000099"
    assert parsers._hash_id("2026-12-02", t["description"], 5.0, "SGD") in t["legacy_ids"]


def test_uob_one_account_export(monkeypatch):
    rows = {t["merchant"]: t for t in _parse("uob_one_account.csv", monkeypatch)[0]}
    assert rows["ALEX EXAMPLE"]["ref"] == "PIB2612020000000011"
    assert rows["ALEX EXAMPLE"]["match_text"] == "PAYNOW ALEX EXAMPLE"   # a person → flagged PayNow
    assert rows["EXAMPLE FOODS PTE LTD"]["match_text"] == "EXAMPLE FOODS PTE LTD dinner"
    assert rows["EXAMPLE BAKERY"]["ref"] == "MBK2612050000000013"
    assert {"EXAMPLE MART", "EXAMPLE CAFE", "JANE EXAMPLE", "UCF"} <= set(rows)
    assert rows["JANE EXAMPLE"]["is_credit"]
    assert rows["EXAMPLE MART"]["running_balance"] == 4918.25


def test_dbs_digibank(monkeypatch):
    txns, _, _ = _parse("dbs_digibank.csv", monkeypatch)
    by_code = {t["description"].split()[0]: t for t in txns}
    assert len(txns) == 5                                # pending + amount-less rows skipped
    assert not any("PENDING" in t["description"] for t in txns)
    assert by_code["GIRO"]["merchant"] == "EXAMPLE CO PTE LTD" and by_code["GIRO"]["is_credit"]
    assert by_code["Funds"]["merchant"] == "ALEX EXAMPLE"
    assert by_code["Bill"]["merchant"] == "EXAMPLE TELCO"
    assert by_code["EXAMPLE"]["merchant"] == "EXAMPLE CAFE SINGAPORE SGP"   # "07DEC" stripped
    assert by_code["SERVICE"]["description"] == "SERVICE CHARGE Fall Below Fee"
    assert by_code["SERVICE"]["ref"] is None


def test_dbs_legacy(monkeypatch):
    txns, _, _ = _parse("dbs_legacy.csv", monkeypatch)
    assert [(t["date"], t["amount"], t["is_credit"]) for t in txns] == [
        ("2026-12-03", 12.30, False), ("2026-12-05", 250.00, True),
        ("2026-12-08", 8.00, False), ("2026-12-10", 1250.00, False),
    ]                                                    # "Total" row skipped
    pos, ict, adv, _giro = txns
    # description from Ref1-3 like digibank, not the bare code
    assert pos["description"] == "EXAMPLE MART SINGAPORE 5548-0000 0001"
    assert pos["merchant"] == "EXAMPLE MART SINGAPORE" and ict["merchant"] == "JOHN EXAMPLE"
    # ids from the old parser (Ref1 as ref, or a hash of the code) still match
    assert "ref-EXAMPLE MART SINGAPORE" in pos["legacy_ids"]
    assert adv["description"] == "ADV" and adv["imported_id"] == parsers._hash_id("2026-12-08", "ADV", 8.0, "SGD")


def test_ocbc(monkeypatch):
    txns, _, _ = _parse("ocbc.csv", monkeypatch)
    assert [t["description"] for t in txns] == [
        "NETS QR EXAMPLE HAWKER", "GIRO - EXAMPLE UTILITIES", "FAST PAYMENT FROM SAM EXAMPLE",
    ]
    assert [t["date"] for t in txns] == ["2026-12-01", "2026-12-03", "2026-12-05"]   # dd/mm, not mm/dd
    assert txns[1]["amount"] == 1200.00 and txns[1]["imported_id"] == "ref-REF0002"
    assert txns[2]["is_credit"] and txns[2]["ref"] is None


def test_ocbc_360_transaction_date_header(monkeypatch):
    txns, bank, info = _parse("ocbc_360.csv", monkeypatch)
    assert bank == "OCBC" and len(txns) == 3
    assert txns[0]["date"] == "2026-11-28"               # Transaction date, not Value date
    assert txns[1]["is_credit"] and txns[1]["amount"] == 3800.00
    assert txns[2]["description"] == "PAYNOW TRANSFER"
    assert info.last4 == "0002"


# ── detection ────────────────────────────────────────────────────────────────


def test_pdf_detection_prefers_layout():
    card = (FIXTURES / "uob_card.txt").read_text()
    account = (FIXTURES / "uob_account.txt").read_text()
    assert parsers.detect_and_parse_pdf(card)[1] == "UOB Credit Card"
    assert parsers.detect_and_parse_pdf(account)[1] == "UOB Account"


def test_pdf_from_unknown_bank_is_rejected():
    with pytest.raises(ValueError, match="Could not detect the bank"):
        parsers.detect_and_parse_pdf("SOME OTHER BANK\n01 JAN  01 JAN  THING   1.00")


def test_unlabelled_ocbc_layout_falls_back_by_shape():
    df = parsers.load_table((FIXTURES / "ocbc.csv").read_bytes())
    df.iloc[0, 0] = "360 Account"                       # no bank name anywhere
    txns, bank = parsers.detect_and_parse(df, hint="export.csv")
    assert bank == "OCBC" and len(txns) == 3


def test_unknown_table_lists_every_parser_tried():
    df = pd.DataFrame([["Hello", "World"], ["1", "2"]])
    with pytest.raises(ValueError) as e:
        parsers.detect_and_parse(df)
    assert all(b in str(e.value) for b in ("UOB", "DBS/POSB", "OCBC"))
