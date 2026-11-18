import json

import httpx
import pytest

import review
import reconcile
from accounts import StatementInfo
from llm import LLMCategorizer, run_tools


def tx(id, acct, date, amount, payee="X", iid=None, text=None, **kw):
    return {"id": id, "account": acct, "account_name": acct, "date": date, "amount": amount, "payee": payee,
            "imported_id": iid, "imported_payee": text or payee, "transfer_id": None, **kw}


# ── finders ──────────────────────────────────────────────────────────────────

def test_transfer_pairs_prefer_account_reference():
    rows = [tx("s1", "sav", "2026-09-06", -152373, "Card Payment", "h1", "Bill Payment Card payment mBK-UOB Cards 4265884082432583"),
            tx("c1", "card", "2026-09-06", 152373, "Card Payment", "h2", "PAYMT THRU E-BANK"),
            tx("p1", "sav", "2026-09-07", -5000, "Yani", "ref-PIB1", "PAYNOW-FAST OTHR bibik PIB1 Yani"),
            tx("p2", "posb", "2026-09-07", 5000, "Jane", "ref-2", "Incoming PayNow From: JANE")]
    pairs = review.find_transfer_pairs(rows, {"card": "2583"})
    assert len(pairs) == 1 and pairs[0]["reason_code"] == "account_ref"   # the PayNow look-alike isn't proposed


def test_existing_duplicates_ignore_distinct_bank_refs():
    rows = [tx("a", "c", "2026-08-01", -200, "Ijooz", "ref-1"), tx("b", "c", "2026-08-03", -200, "Ijooz", "ref-2"),
            tx("m1", "c", "2026-09-17", 788973, "Salary", "h9"), tx("m2", "c", "2026-09-17", 788973, "Salary", None)]
    d = review.find_existing_duplicates(rows)
    assert len(d) == 1 and {d[0]["a"]["id"], d[0]["b"]["id"]} == {"m1", "m2"}


def test_existing_duplicates_catch_same_merchant_in_different_export_text():
    # the xls says "SHOPEE SINGAPORE MP", the PDF says "SHOPEE SG MP SINGAPORE"; same charge, two id schemes
    rows = [tx("x", "c", "2026-09-02", -2590, "Shopee", "ref-77", "SHOPEE SINGAPORE MP"),
            tx("p", "c", "2026-09-03", -2590, "SHOPEE SG MP", "h3", "SHOPEE SG MP SINGAPORE")]
    d = review.find_existing_duplicates(rows)
    assert len(d) == 1 and d[0]["confidence"] == "medium" and d[0]["suggest"] == "delete_b"  # keep the bank ref


def test_existing_duplicates_id_scheme_clash_same_day():
    rows = [tx("x", "c", "2026-09-02", -1250, "Grab", "ref-9", "GRAB* A-7XYZ"),
            tx("p", "c", "2026-09-02", -1250, "Grabfood", "h4", "GRABFOOD SINGAPORE")]
    d = review.find_existing_duplicates(rows)
    assert len(d) == 1 and "id schemes" in d[0]["reason"]
    # different merchants, two different hashes → not flagged
    rows = [tx("x", "c", "2026-09-02", -1250, "Kopi", "h1", "TOAST BOX"), tx("p", "c", "2026-09-02", -1250, "Bus", "h2", "BUS/MRT")]
    assert review.find_existing_duplicates(rows) == []


def test_cross_account_duplicates():
    rows = [tx("a", "card", "2026-09-02", -4200, "NTUC", "ref-5", "NTUC FAIRPRICE SINGAPORE"),
            tx("b", "sav", "2026-09-02", -4200, "NTUC", "ref-5", "NTUC FAIRPRICE SINGAPORE"),
            tx("t1", "sav", "2026-09-04", -9900, "Card Payment", "h1", "PAYMT THRU E-BANK"),   # a transfer: opposite sign
            tx("t2", "card", "2026-09-04", 9900, "Card Payment", "h2", "PAYMT THRU E-BANK")]
    d = review.find_cross_account_duplicates(rows)
    assert len(d) == 1 and d[0]["cross_account"] and d[0]["confidence"] == "high"
    it = review.enqueue("existing_duplicate", {**d[0], "account": "card"}, ["a", "b"])
    assert "both" in review.pattern_for("existing_duplicate", it["payload"])[1]
    assert review.suggested_decision({**it, "llm": {"verdict": "duplicate"}}) is None   # you pick the account


# ── queue & learning ─────────────────────────────────────────────────────────

def pair(n, amount=-100):
    return {"a": tx(f"a{n}", "sav", "2026-09-06", amount, "Card Payment"),
            "b": tx(f"b{n}", "card", "2026-09-06", -amount, "Card Payment"), "reason_code": "account_ref"}


def test_decisions_are_remembered_and_learned():
    i1 = review.enqueue("transfer_pair", pair(1), ["a1", "b1"])
    assert i1["status"] == "pending"
    assert review.enqueue("transfer_pair", pair(1), ["a1", "b1"])["id"] == i1["id"]   # no duplicates in the queue
    review.decide(i1["id"], "link")
    i2 = review.enqueue("transfer_pair", pair(2), ["a2", "b2"])
    assert i2["status"] == "pending"                                                   # once isn't a pattern yet
    review.decide(i2["id"], "link")
    i3 = review.enqueue("transfer_pair", pair(3), ["a3", "b3"])
    assert i3["status"] == "auto" and i3["decision"] == "link" and "learned" in i3["decided_by"]
    # forgetting the pattern stops auto-resolution
    key = next(m["key"] for m in review.memory() if m["kind"] == "transfer_pair")
    review.forget(key)
    assert review.enqueue("transfer_pair", pair(4), ["a4", "b4"])["status"] == "pending"


def test_deletes_and_reconcile_fixes_never_become_patterns():
    for n in range(3):
        it = review.enqueue("existing_duplicate", {"a": tx(f"x{n}", "c", "2026-09-01", -1, "Kopi"),
                                                   "b": tx(f"y{n}", "c", "2026-09-01", -1, "Kopi")}, [f"x{n}", f"y{n}"])
        review.decide(it["id"], "delete_b")
    it = review.enqueue("existing_duplicate", {"a": tx("x9", "c", "2026-09-01", -1, "Kopi"),
                                               "b": tx("y9", "c", "2026-09-01", -1, "Kopi")}, ["x9", "y9"])
    assert it["status"] == "pending"


def test_invalid_decision_rejected():
    it = review.enqueue("transfer_pair", pair(7), ["a7", "b7"])
    with pytest.raises(ValueError):
        review.decide(it["id"], "delete_a")


def test_supersede_stale_items():
    it = review.enqueue("existing_duplicate", {"a": tx("q1", "c", "2026-09-01", -1), "b": tx("q2", "c", "2026-09-01", -1)}, ["q1", "q2"])
    assert review.supersede({"q2"}) == 1
    assert review.get(it["id"])["status"] == "superseded"


def test_suggested_decision_keeps_bank_ref_copy():
    it = {"kind": "existing_duplicate", "llm": {"verdict": "duplicate"},
          "payload": {"a": {"imported_id": "ref-1"}, "b": {"imported_id": None}}}
    assert review.suggested_decision(it) == "delete_b"


# ── reconciliation engine ────────────────────────────────────────────────────

def stmt(iid, date, amount, credit=False, rb=None):
    return {"imported_id": iid, "legacy_ids": [], "date": date, "amount": amount, "is_credit": credit,
            "payee": iid, "description": iid, **({"running_balance": rb} if rb is not None else {})}


def test_opening_balance_detected_from_constant_running_offset():
    info = StatementInfo(bank="UOB", kind="deposit", balance=1000.0, period_start="2026-09-01", period_end="2026-09-10")
    s = [stmt("b", "2026-09-10", 50, rb=1000.0), stmt("a", "2026-09-05", 100, credit=True, rb=1050.0)]
    actual = [tx("1", "acc", "2026-09-05", 10000, iid="a"), tx("2", "acc", "2026-09-10", -5000, iid="b")]
    r = reconcile.analyse("acc", info, s, actual)
    assert r["gap"] == 95000 and r["running_balance"]["constant_offset"] == 95000
    plan = r["recommendation"]
    assert plan[0]["fix_type"] == "opening_balance" and plan[0]["actions"][0]["amount_cents"] == 95000
    assert plan[0]["actions"][0]["date"] == "2026-08-31"


def test_missing_row_found_and_divergence_dated():
    info = StatementInfo(bank="UOB", kind="deposit", balance=1000.0, period_start="2026-09-01", period_end="2026-09-10")
    s = [stmt("c", "2026-09-10", 20, rb=1000.0), stmt("b", "2026-09-07", 30, rb=1020.0), stmt("a", "2026-09-01", 1050, credit=True, rb=1050.0)]
    actual = [tx("1", "acc", "2026-09-01", 105000, iid="a"), tx("3", "acc", "2026-09-10", -2000, iid="c")]
    r = reconcile.analyse("acc", info, s, actual)
    assert r["gap"] == -3000 and [m["imported_id"] for m in r["missing"]] == ["b"]
    assert r["running_balance"]["first_divergence"]["date"] == "2026-09-07"
    assert r["exact_combinations"] == [["import:b"]]
    assert r["recommendation"][0]["fix_type"] == "import_missing"


def test_card_statement_balance_is_not_trusted_without_user_balance():
    info = StatementInfo(bank="UOB", kind="credit_card", balance=1523.73, balance_is_owed=True,
                         period_start="2026-09-01", period_end="2026-09-30")
    r = reconcile.analyse("card", info, [stmt("a", "2026-09-05", 10)], [tx("1", "card", "2026-09-05", -1000, iid="a")])
    assert not r["balance_reliable"]
    assert [p["fix_type"] for p in r["recommendation"]] == ["investigate"]
    r2 = reconcile.analyse("card", info, [stmt("a", "2026-09-05", 10)], [tx("1", "card", "2026-09-05", -1000, iid="a")],
                           override_balance=-10.0)
    assert r2["reconciled"]


def test_subset_solver_finds_fewest_changes():
    assert reconcile.subset_to_target([("x", 500), ("y", 200), ("z", 300)], 500) == [["x"]]
    assert reconcile.subset_to_target([("y", 200), ("z", 300)], 500) == [["y", "z"]]


# ── LLM agent (tool calling) ─────────────────────────────────────────────────

def openai_script(steps):
    """Mock an OpenAI-compatible server that replays a scripted sequence of messages."""
    calls = []

    def handler(request):
        body = json.loads(request.content)
        calls.append(body)
        msg = steps[len(calls) - 1]
        return httpx.Response(200, json={"choices": [{"message": msg}]})
    return httpx.MockTransport(handler), calls


def tc(name, args, i):
    return {"id": f"c{i}", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


def test_agent_investigates_and_queues_only_valid_proposals(tmp_path):
    info = StatementInfo(bank="UOB", kind="deposit", balance=1000.0, period_start="2026-09-01", period_end="2026-09-10")
    s = [stmt("c", "2026-09-10", 20, rb=1000.0), stmt("b", "2026-09-07", 30, rb=1020.0), stmt("a", "2026-09-01", 1050, credit=True, rb=1050.0)]
    actual = [tx("1", "acc", "2026-09-01", 105000, iid="a"), tx("3", "acc", "2026-09-10", -2000, iid="c")]
    report = reconcile.analyse("acc", info, s, actual, account_name="UOB One Account")
    transport, calls = openai_script([
        {"content": None, "tool_calls": [tc("get_summary", {}, 1), tc("list_missing", {}, 2)]},
        {"content": None, "tool_calls": [tc("propose_fix", {"title": "bogus", "explanation": "x", "delete_ids": ["nope"]}, 3)]},
        {"content": None, "tool_calls": [tc("propose_fix", {"title": "Import the missing $30 payment",
                                                            "explanation": "bank has it on 7 Sep, Actual doesn't",
                                                            "import_ids": ["b"]}, 4)]},
        {"content": "Gap was -$30.00: one payment on 7 Sep never reached Actual. Proposed importing it."},
    ])
    llm = LLMCategorizer("openai", "gemini-x", "http://x/v1", "k", transport, cache_file=tmp_path / "c.json")
    out = reconcile.agent(report, s, llm)
    assert "7 Sep" in out["explanation"]
    assert [c["tool"] for c in out["tool_calls"]] == ["get_summary", "list_missing", "propose_fix", "propose_fix"]
    assert len(out["queued"]) == 1                              # the bogus id was refused by the tool
    q = out["queued"][0]
    assert q["kind"] == "reconcile_fix" and q["status"] == "pending"
    assert q["payload"]["actions"][0]["transactions"][0]["imported_id"] == "b"
    assert calls[0]["tools"][0]["function"]["name"] == "get_summary"   # tools were offered to the model
    assert calls[1]["messages"][-1]["role"] == "tool"


def test_run_tools_anthropic_shape(tmp_path):
    steps = [{"content": [{"type": "tool_use", "id": "t1", "name": "ping", "input": {"x": 2}}]},
             {"content": [{"type": "text", "text": "done"}]}]
    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json=steps[len(seen) - 1])
    llm = LLMCategorizer("anthropic", "claude-x", None, "k", httpx.MockTransport(handler), cache_file=tmp_path / "c.json")
    out = run_tools(llm, "sys", "go", [{"name": "ping", "description": "d", "parameters": {"type": "object"}}],
                    {"ping": lambda x: {"pong": x * 2}})
    assert out["text"] == "done" and out["calls"][0]["result"] == {"pong": 4}
    assert seen[1]["messages"][-1]["content"][0]["type"] == "tool_result"


def test_advisor_verdicts_saved(tmp_path):
    it = review.enqueue("transfer_pair", pair(11), ["a11", "b11"])
    reply = json.dumps({"results": [{"id": it["id"], "verdict": "transfer", "confidence": 0.93, "reason": "card payment wording"}]})
    transport = httpx.MockTransport(lambda r: httpx.Response(200, json={"choices": [{"message": {"content": reply}}]}))
    llm = LLMCategorizer("openai", "m", "http://x/v1", "k", transport, cache_file=tmp_path / "c.json")
    review.advise([it], llm)
    got = review.get(it["id"])
    assert got["llm"]["verdict"] == "transfer" and review.suggested_decision(got) == "link"


# ── credit card cycle ────────────────────────────────────────────────────────

def card_stmt(iid, date, amount, posted=None, credit=False):
    return {**stmt(iid, date, amount, credit), "posting_date": posted or date}


CARD = dict(bank="UOB", kind="credit_card", balance=150.0, balance_is_owed=True,
            period_start="2026-08-20", period_end="2026-09-25")


def card_history():
    # owed 150.00 on the 12 Sep statement: 100 (25 Aug) + 50 (bought 11 Sep, POSTED 12 Sep)
    s = [card_stmt("a", "2026-08-25", 100), card_stmt("b", "2026-09-11", 50, posted="2026-09-12"),
         card_stmt("c", "2026-09-13", 30, posted="2026-09-14"),                # after the cut-off
         card_stmt("p", "2026-09-20", 150, credit=True),                        # bill paid
         card_stmt("d", "2026-09-21", 20, posted="2026-09-23")]
    actual = [tx("1", "card", "2026-08-25", -10000, iid="a"), tx("2", "card", "2026-09-11", -5000, iid="b"),
              tx("3", "card", "2026-09-13", -3000, iid="c"), tx("4", "card", "2026-09-20", 15000, iid="p"),
              tx("5", "card", "2026-09-21", -2000, iid="d")]
    return s, actual


def test_card_statement_date_inferred_and_rolled_forward():
    s, actual = card_history()
    r = reconcile.analyse("card", StatementInfo(**CARD), s, actual)
    cyc = r["card_cycle"]
    assert cyc["statement_date"] == "2026-09-12" and cyc["source"].startswith("inferred")   # posting date, not 11 Sep
    assert cyc["unbilled"] == -3000 + 15000 - 2000 and cyc["expected_today"] == -5000
    assert r["balance_reliable"] and r["reconciled"]


def test_card_cycle_finds_missing_row_after_statement():
    s, actual = card_history()
    actual = [a for a in actual if a["id"] != "5"]            # the 21 Sep charge never reached Actual
    r = reconcile.analyse("card", StatementInfo(**CARD), s, actual)
    assert r["balance_reliable"] and r["gap"] == -2000
    assert [m["imported_id"] for m in r["missing"]] == ["d"]
    assert r["recommendation"][0]["fix_type"] == "import_missing"


def test_card_cycle_entered_date_is_remembered_and_exposes_cycle_gap():
    s, actual = card_history()
    actual = [a for a in actual if a["id"] != "1"]            # a pre-cycle row is missing → no day matches
    r = reconcile.analyse("card", StatementInfo(**CARD), s, actual)
    assert not r["balance_reliable"] and r["card_cycle"]["statement_date"] is None
    assert "statement date" in r["recommendation"][-1]["title"]
    r = reconcile.analyse("card", StatementInfo(**CARD), s, actual, statement_date="2026-09-12")
    assert r["card_cycle"]["cycle_gap"] == -10000 and r["gap"] == -10000
    assert [m["imported_id"] for m in r["missing"]] == ["a"]
    reconcile.remember_cycle_day("card", "2026-09-12")
    assert reconcile.cycle_day("card") == 12
    r = reconcile.analyse("card", StatementInfo(**CARD), s, actual, cycle_day=reconcile.cycle_day("card"))
    assert r["card_cycle"]["statement_date"] == "2026-09-12" and "remembered" in r["card_cycle"]["source"]
    assert reconcile._last_cycle_date(31, "2026-09-25") == "2026-08-31"
    assert reconcile._last_cycle_date(31, "2026-03-05") == "2026-02-28"
