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
