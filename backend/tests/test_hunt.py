import json

import httpx

import hunt
import review
from llm import LLMCategorizer


def tx(id, acct, date, amount, payee="X", iid=None, text=None, **kw):
    return {"id": id, "account": acct, "account_name": acct.upper(), "date": date, "amount": amount, "payee": payee,
            "imported_id": iid, "imported_payee": text or payee, "transfer_id": None, **kw}


def openai_script(steps):
    calls = []

    def handler(request):
        calls.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": steps[len(calls) - 1]}]})
    return httpx.MockTransport(handler), calls


def tc(name, args, i):
    return {"id": f"c{i}", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


ROWS = [
    # typed-in copy five days after the import, different wording — the rules (≤2 days) miss it
    tx("g1", "card", "2026-09-02", -2590, "Grab", "ref-1", "GRAB*A-7XYZ SINGAPORE"),
    tx("g2", "card", "2026-09-07", -2590, "Grab ride", None, None),
    # a card payment whose legs differ by 36 cents
    tx("s1", "sav", "2026-09-06", -152373, "Card Payment", "h1", "Bill Payment mBK-UOB Cards 4265884082432583"),
    tx("c1", "card", "2026-09-07", 152337, "Payment", "h2", "PAYMT THRU E-BANK"),
    # an exact own-account transfer without transfer wording
    tx("o1", "sav", "2026-09-10", -50000, "Syamil", "h3", "PAYNOW SYAMIL"),
    tx("o2", "posb", "2026-09-10", 50000, "Syamil", "h4", "INCOMING PAYNOW SYAMIL"),
    # two real bank-referenced coffees
    tx("k1", "card", "2026-09-12", -650, "Starbucks", "ref-8"), tx("k2", "card", "2026-09-12", -650, "Starbucks", "ref-9"),
]


def test_loose_duplicates_widen_the_window_but_respect_bank_refs():
    pairs = hunt.loose_duplicate_pairs(ROWS)
    ids = [{p["a"]["id"], p["b"]["id"]} for p in pairs]
    assert {"g1", "g2"} in ids
    assert {"k1", "k2"} not in ids                       # two bank references are two transactions
    assert not review.find_existing_duplicates(ROWS)     # …and the strict rules indeed miss the Grab pair


def test_orphan_transfers_surface_amount_mismatch():
    orphans = hunt.orphan_transfer_legs(ROWS)
    s1 = next(o for o in orphans if o["row"]["id"] == "s1")
    assert s1["near_matches"][0]["id"] == "c1" and s1["near_matches"][0]["amount_diff"] == -36


def test_hunt_agent_queues_validated_proposals_and_flags(tmp_path):
    transport, calls = openai_script([
        {"content": None, "tool_calls": [tc("overview", {}, 1), tc("loose_duplicates", {}, 2), tc("orphan_transfers", {}, 3)]},
        {"content": None, "tool_calls": [
            tc("search", {"text": "grab"}, 4),
            tc("propose_duplicate", {"keep_id": "k1", "delete_id": "k2", "reason": "same coffee"}, 5),   # refused
            tc("propose_duplicate", {"keep_id": "g1", "delete_id": "nope", "reason": "x"}, 6),           # refused
            tc("propose_duplicate", {"keep_id": "g1", "delete_id": "g2", "reason": "typed-in copy of the Grab ride",
                                     "confidence": 0.9}, 7),
            tc("propose_transfer", {"out_id": "s1", "in_id": "c1", "reason": "card payment"}, 8),          # refused
            tc("propose_transfer", {"out_id": "o2", "in_id": "o1", "reason": "PayNow to self"}, 9),
            tc("flag", {"ids": ["s1", "c1"], "note": "card payment legs differ by S$0.36", "kind": "amount_mismatch"}, 10)]},
        {"content": "Checked 8 rows. Queued one duplicate Grab ride and one own-account transfer; flagged a S$0.36 mismatch."},
    ])
    llm = LLMCategorizer("openai", "m", "http://x/v1", "k", transport, cache_file=tmp_path / "c.json")
    out = hunt.run(ROWS, [{"id": "card", "name": "CARD"}], ("2026-09-01", "2026-09-30"), llm, {"card": "2583"},
                   instructions="check Grab")
    assert "S$0.36" in out["explanation"] and "check Grab" in calls[0]["messages"][1]["content"]
    kinds = sorted(q["kind"] for q in out["queued"])
    assert kinds == ["existing_duplicate", "transfer_pair"]
    dup = next(q for q in out["queued"] if q["kind"] == "existing_duplicate")
    assert dup["status"] == "pending" and dup["payload"]["b"]["id"] == "g2"
    assert review.suggested_decision(dup) == "delete_b"          # the UI highlights deleting the typed-in copy
    xfer = next(q for q in out["queued"] if q["kind"] == "transfer_pair")
    assert xfer["payload"]["a"]["id"] == "o1" and xfer["llm"]["verdict"] == "transfer"   # money-out side first
    assert out["findings"][0]["kind"] == "amount_mismatch" and len(out["findings"][0]["rows"]) == 2
    results = {m["tool_call_id"]: json.loads(m["content"]) for m in calls[2]["messages"] if m["role"] == "tool"}
    assert "error" in results["c5"] and "error" in results["c6"] and "error" in results["c8"]
    assert review.counts()["pending"] == 2                     # nothing applied — only queued


def test_hunt_skips_pairs_you_already_decided(tmp_path):
    rows = ROWS[:2]
    it = review.enqueue("existing_duplicate", {"a": review._slim(rows[0]), "b": review._slim(rows[1]),
                                               "account": "card", "suggest": "delete_b"}, ["g1", "g2"])
    review.decide(it["id"], "keep_both")
    review.mark_done(it["id"], {"ok": True})
    _, handlers, _ = hunt.tools(rows, [], ("2026-09-01", "2026-09-30"))
    assert handlers["loose_duplicates"]()["total"] == 0
    assert "skipped" in handlers["propose_duplicate"]("g1", "g2", "dup")


def test_hunt_route_runs_end_to_end(monkeypatch):
    """POST /review/hunt wires the bridge rows, account last-4s and the LLM into hunt.run."""
    from fastapi.testclient import TestClient
    import main
    import bridge_client

    class FakeLLM:
        enabled, model = True, "fake"

    seen = {}

    def fake_run(rows, accts, window, llm, last4, instructions):
        seen.update(rows=rows, window=window, last4=last4, instructions=instructions)
        return {"explanation": "ok", "queued": [], "findings": [], "tool_calls": [], "scanned": len(rows),
                "window": {"start": window[0], "end": window[1]}}

    monkeypatch.setattr(main, "LLMCategorizer", FakeLLM)
    monkeypatch.setattr(bridge_client, "txns", lambda s, e, a=None: (ROWS, [{"id": "card", "name": "CARD"}]))
    monkeypatch.setattr(main, "_account_last4", lambda: {"card": "2583"})
    monkeypatch.setattr(hunt, "run", fake_run)
    r = TestClient(main.app).post("/review/hunt", json={"start": "2026-09-01", "end": "2026-09-30",
                                                        "instructions": "Grab"})
    assert r.status_code == 200, r.text
    assert r.json()["scanned"] == len(ROWS) and "counts" in r.json()
    assert seen["last4"] == {"card": "2583"} and seen["window"] == ("2026-09-01", "2026-09-30")
    assert seen["instructions"] == "Grab"
