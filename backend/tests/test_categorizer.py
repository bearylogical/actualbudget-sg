import pytest
from categorizer import categorize
from payees import normalize_payee, looks_raw


@pytest.mark.parametrize("desc,credit,category,kind", [
    # regressions from the old substring matcher
    ("SHENG SIONG SUPERMARKET SINGAPORE SG", False, "Groceries", "expense"),     # 'gap' in SINGAPORE
    ("STARBUCKS SINGAPORE SG", False, "Coffee & Drinks", "expense"),
    ("LAS VEGAS STEAKHOUSE", False, "Dining & Hawker", "expense"),              # 'gas'
    ("POWERBANK RENTAL", False, "Uncategorized", "expense"),                    # 'power'
    ("ADVENTURE COVE SENTOSA", False, "Uncategorized", "expense"),              # 'ntu'
    ("SALARY BONUS", True, "Salary", "income"),                                  # 'nus'
    # ordering: specific before generic
    ("GRABFOOD SINGAPORE", False, "Food Delivery", "expense"),
    ("Grab* A-9RNPVFUWWHHOAV Singapore SG", False, "Ride-hailing & Taxi", "expense"),
    ("APPLE.COM/BILL 8001861087 IE", False, "Subscriptions", "expense"),
    ("AMAZON WEB SERVICES AWS.AMAZON.CO", False, "Software & Cloud", "expense"),
    ("GIRO - SP SERVICES", False, "Utilities", "expense"),
    ("FAIRPRICE GROUP HAWKER CESINGAPORE SG", False, "Dining & Hawker", "expense"),
    ("NTUC FP - TAMPINES HUB SINGAPORE SG", False, "Groceries", "expense"),
    ("GOOGLE*YOUTUBEPREMIUM SINGAPORE SG", False, "Subscriptions", "expense"),
    ("APPLIED MATERIALS - SOC SINGAPORE SG", False, "Dining & Hawker", "expense"),
    # transfers / p2p are never guessed
    ("PAYMT THRU E-BANK/HOMEB/CYBERB (EP52)", True, "Uncategorized", "transfer"),
    ("UOB:7613634421:I-BANK Transfer OTHR 17883002790434409705", False, "Uncategorized", "transfer"),
    ("PayNow Transfer 5763053 To: HITPAY PAYMENTS", False, "Uncategorized", "p2p"),
    ("GRABPAY TOP UP", False, "Uncategorized", "transfer"),
    # sign-restricted rules
    ("INTEREST CREDIT", True, "Interest & Dividends", "income"),
    ("ANNUAL FEE", False, "Bank Fees & Charges", "expense"),
    ("UOB ONE CASH REBATE BILL REDEMPTION", True, "Cashback & Rebates", "income"),
    ("SHOPEE REFUND", True, "Shopping", "expense"),                              # refund offsets spend
])
def test_categorize(desc, credit, category, kind):
    r = categorize(desc, credit)
    assert (r["category"], r["kind"]) == (category, kind)


@pytest.mark.parametrize("raw,clean", [
    ("SHENG SIONG SUPERMARKET SINGAPORE SG", "Sheng Siong"),
    ("TST* MOTHER DOUGH BAKERY SINGAPORE SG", "Mother Dough Bakery"),
    ("PAYPAL *ASIANFILMAR 4029357733 SG", "Asianfilmar"),
    ("EzypaySGD*Body Fit Traini", "Body Fit Traini"),
    ("KULT YARD* OD SINGAPORE SG", "Kult Yard"),
    ("ZHANG LIANG MALATANG  SINGAPORE SG", "Zhang Liang Malatang"),
    ("Grab* A-9ROWEGCWW7CWAV   Singapore    SG", "Grab"),
])
def test_normalize_payee(raw, clean):
    assert normalize_payee(raw) == clean


def test_looks_raw():
    assert looks_raw("GRAB*A-12345 SINGAPORE SG")
    assert looks_raw("SHENG SIONG SUPERMARKET")
    assert not looks_raw("Sheng Siong")
