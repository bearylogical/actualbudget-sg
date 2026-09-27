import asyncio
import json
import time

import httpx
import pytest

import health
from llm import LLMCategorizer


@pytest.fixture(autouse=True)
def _paths(tmp_path, monkeypatch):
    monkeypatch.setattr(health, "DATA_DIR", tmp_path)
    monkeypatch.setattr(health, "HEARTBEAT_FILE", tmp_path / "scheduler-heartbeat.json")
    health._llm_cache.update(result=None, ts=0.0)


def llm_with(handler, tmp_path, model="m"):
    return LLMCategorizer("openai", model, "http://x/v1", "k", httpx.MockTransport(handler),
                          cache_file=tmp_path / "c.json")


def ok_reply(text):
    return lambda req: httpx.Response(200, json={"choices": [{"message": {"content": text}}]})


# ── LLM ──────────────────────────────────────────────────────────────────────

def test_llm_disabled_makes_no_call():
    r = health.check_llm(llm=LLMCategorizer("none"))
    assert r["status"] == "disabled"


def test_llm_ok_then_cached_then_refresh(tmp_path):
    calls = []
    def handler(req):
        calls.append(1)
        return ok_reply('{"ok": true}')(req)
    llm = llm_with(handler, tmp_path)
    assert health.check_llm(llm=llm, now=1000)["status"] == "ok"
    r = health.check_llm(llm=llm, now=1100)
    assert r["cached"] and len(calls) == 1
    health.check_llm(refresh=True, llm=llm, now=1100)
    assert len(calls) == 2
    health.check_llm(llm=llm_with(handler, tmp_path, model="other"), now=1100)   # config changed
    assert len(calls) == 3


def test_llm_bad_key_explained(tmp_path):
    llm = llm_with(lambda req: httpx.Response(401, text='{"error":"API key not valid"}'), tmp_path)
    r = health.check_llm(llm=llm)
    assert r["status"] == "error" and "HTTP 401 (bad API key)" in r["detail"]


def test_llm_non_json_is_degraded(tmp_path):
    r = health.check_llm(llm=llm_with(ok_reply("Sure! Here you go."), tmp_path))
    assert r["status"] == "degraded"


def test_llm_unreachable(tmp_path):
    def boom(req):
        raise httpx.ConnectError("connection refused")
    r = health.check_llm(llm=llm_with(boom, tmp_path))
    assert r["status"] == "error" and "ConnectError" in r["detail"]


# ── scheduler ────────────────────────────────────────────────────────────────

def beat(**kw):
    health.HEARTBEAT_FILE.write_text(json.dumps({"ts": time.time(), "poll_secs": 30, "config_ok": True, **kw}))


def test_scheduler_states():
    assert health.check_scheduler()["status"] == "unknown"
    beat()
    assert health.check_scheduler()["status"] == "ok"
    beat(last_result="error", last_error="Account 'X' not found")
    assert health.check_scheduler()["status"] == "degraded"
    beat(config_ok=False)
    assert health.check_scheduler()["status"] == "degraded"
    beat()
    assert health.check_scheduler(now=time.time() + 1000)["status"] == "stale"


# ── bridge / Actual server ───────────────────────────────────────────────────

def run_bridge(payload=None, exc=None):
    def handler(req):
        if exc:
            raise exc
        return httpx.Response(200, json=payload)
    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            return await health.check_bridge("http://bridge", c)
    return asyncio.run(go())


def test_bridge_down():
    bridge, server = run_bridge(exc=httpx.ConnectError("refused"))
    assert bridge["status"] == "error" and server["status"] == "unknown"


def test_bridge_not_connected_yet():
    bridge, server = run_bridge({"ok": True, "initialized": False, "apiVersion": "26.9.0"})
    assert bridge["status"] == "ok" and server["status"] == "unknown"


def test_server_version_mismatch():
    _, server = run_bridge({"ok": True, "initialized": True, "budgetLoaded": True, "apiVersion": "26.9.0",
                            "serverURL": "http://actual:5006",
                            "actualServer": {"ok": True, "status": 200, "version": "25.10.0", "versionMatch": False}})
    assert server["status"] == "degraded" and "pin" in server["detail"]


def test_server_unreachable():
    _, server = run_bridge({"ok": True, "initialized": True, "serverURL": "http://actual:5006",
                            "actualServer": {"ok": False, "error": "fetch failed"}})
    assert server["status"] == "error"


# ── overall ──────────────────────────────────────────────────────────────────

def test_overall():
    ok = {"status": "ok"}
    assert health.overall({"backend": ok, "bridge": ok, "llm": {"status": "disabled"},
                           "scheduler": {"status": "unknown"}}) == "ok"
    assert health.overall({"backend": ok, "bridge": ok, "llm": {"status": "error"}}) == "degraded"
    assert health.overall({"backend": ok, "bridge": {"status": "error"}}) == "error"


def test_backend_check_writable():
    assert health.check_backend()["data_dir"]
