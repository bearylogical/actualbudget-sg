import json

import httpx

import audit


def _setup(tmp_path, monkeypatch):
    monkeypatch.setattr(audit, "DATA_DIR", tmp_path)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    monkeypatch.setenv("TELEGRAM_DIGEST_HOURS", "24")


def test_summary_and_digest_text(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    t = 1_800_000_000
    audit.record({"ts": t, "source": "public", "kind": "tool", "name": "money_summary", "client": "abc", "ok": True})
    audit.record({"ts": t + 1, "source": "public", "kind": "tool", "name": "money_summary", "client": "abc", "ok": True})
    audit.record({"ts": t + 2, "source": "private", "kind": "tool", "name": "ibkr_to_ghostfolio_import", "write": True,
                  "ok": False, "error": "HTTP 502", "args": {"activities_json": "<7 items>"}})
    audit.record({"ts": t + 3, "source": "public", "kind": "auth", "name": "login_failed", "ok": False, "args": {"ip": "1.2.3.4"}})
    audit.record({"ts": t + 4, "source": "public", "kind": "auth", "name": "signed_in"})
    s = audit.summarize(t, t + 10)
    assert s["total_calls"] == 3 and s["alerts"] == 1
    assert s["by_tool"]["money_summary (public)"] == {"calls": 2, "errors": 0}
    assert s["writes"][0]["args"] == {"activities_json": "<7 items>"} and not s["writes"][0]["ok"]
    assert s["auth"] == {"login_failed": 1, "signed_in": 1}
    text = audit.digest_text(s)
    assert text.startswith("<b>⚠️ Budget MCP audit</b>")
    assert "ibkr_to_ghostfolio_import (private) activities_json=&lt;7 items&gt;" in text and "❌" in text
    assert audit.digest_text(audit.summarize(t + 100, t + 200)) is None          # quiet period → no message


def test_digest_schedule_and_send(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    sent = []

    def handler(req):
        sent.append((req.url.path, json.loads(req.content)))
        return httpx.Response(200, json={"ok": True})
    tr = httpx.MockTransport(handler)
    now = 1_800_000_000
    assert audit.digest(now=now, transport=tr)["sent"] is False                  # first run just starts the clock
    audit.record({"ts": now + 60, "source": "public", "kind": "tool", "name": "health"})
    assert audit.digest(now=now + 3600, transport=tr)["reason"] == "not due"
    r = audit.digest(now=now + 86400, transport=tr)
    assert r["sent"] and sent[0][0] == "/bot123:abc/sendMessage"
    assert sent[0][1]["chat_id"] == "42" and "health (public): 1" in sent[0][1]["text"]
    assert audit.digest(now=now + 2 * 86400, transport=tr)["sent"] is False      # nothing new → nothing sent
    assert len(sent) == 1


def test_ingest_endpoint(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    import main
    _setup(tmp_path, monkeypatch)
    c = TestClient(main.app)
    assert c.post("/audit/mcp", json={"source": "private", "kind": "tool", "name": "health"}).json() == {"ok": True}
    assert c.get("/audit/mcp?hours=1").json()["by_tool"] == {"health (private)": {"calls": 1, "errors": 0}}
    r = c.post("/audit/mcp/digest?send=false").json()
    assert r["sent"] is False and "health (private): 1" in r["text"]
