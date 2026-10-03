"""
Health checks for every service.

  GET /health/live            backend process is up (Docker healthcheck, no dependencies)
  GET /health                 full report (UI status widget)
  GET /health?llm=refresh     re-test the LLM now instead of using the cached result
  GET /health?strict=1        HTTP 503 unless everything is ok (for Uptime Kuma & co.)

Component status values:
  ok        working
  degraded  working with a problem (e.g. version mismatch, LLM replies but not JSON)
  error     broken
  stale     scheduler heartbeat too old
  disabled  turned off on purpose (LLM_PROVIDER=none)
  unknown   can't tell yet (e.g. no Actual budget loaded in the bridge)

Overall: error if the backend or bridge is broken, degraded if anything else is
error/degraded/stale, else ok. disabled/unknown never lower the overall status.
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

from llm import LLMCategorizer

DATA_DIR = Path(os.getenv("DATA_DIR", "/data"))
HEARTBEAT_FILE = DATA_DIR / "scheduler-heartbeat.json"
LLM_HEALTH_TTL = int(os.getenv("LLM_HEALTH_TTL", "600"))   # seconds; each check is a real (tiny) API call

_llm_cache: dict = {"result": None, "ts": 0.0}


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds")


def check_backend() -> dict:
    problems = []
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        probe = DATA_DIR / ".health-probe"
        probe.write_text(str(time.time()))
        probe.unlink()
    except Exception as e:
        problems.append(f"DATA_DIR {DATA_DIR} not writable: {e}")
    xls_reader = importlib.util.find_spec("xlrd") is not None or shutil.which("libreoffice") is not None
    if not xls_reader:
        problems.append("no .xls reader (xlrd or libreoffice) — UOB exports will fail")
    return {"status": "degraded" if problems else "ok", "data_dir": str(DATA_DIR),
            "xlrd": importlib.util.find_spec("xlrd") is not None,
            "libreoffice": shutil.which("libreoffice") is not None,
            **({"detail": "; ".join(problems)} if problems else {})}


async def check_bridge(bridge_url: str, client: httpx.AsyncClient) -> tuple[dict, dict]:
    """Returns (bridge component, actual_server component)."""
    t0 = time.monotonic()
    try:
        r = await client.get(f"{bridge_url}/health", params={"deep": 1}, timeout=10)
        ms = round((time.monotonic() - t0) * 1000)
        r.raise_for_status()
        d = r.json()
    except Exception as e:
        err = {"status": "error", "detail": f"{type(e).__name__}: {e}"[:240]}
        return err, {"status": "unknown", "detail": "bridge unreachable"}

    bridge = {"status": "ok", "latency_ms": ms, "api_version": d.get("apiVersion"),
              "budget_loaded": d.get("budgetLoaded"), "started_at": d.get("startedAt")}

    srv = d.get("actualServer")
    if not d.get("initialized"):
        server = {"status": "unknown", "detail": "not connected yet — connect in the UI or start the scheduler"}
    elif not srv:
        server = {"status": "unknown", "detail": "bridge did not report server status"}
    elif not srv.get("ok"):
        server = {"status": "error", "url": d.get("serverURL"),
                  "detail": srv.get("error") or f"HTTP {srv.get('status')}"}
    else:
        server = {"status": "ok", "url": d.get("serverURL"), "latency_ms": srv.get("latencyMs"),
                  "version": srv.get("version"), "api_version": d.get("apiVersion")}
        if srv.get("versionMatch") is False:
            server["status"] = "degraded"
            server["detail"] = (f"server {srv.get('version')} ≠ @actual-app/api {d.get('apiVersion')} — "
                                "rebuild the bridge: scripts/actual-version.sh --up")
        if not d.get("budgetLoaded"):
            server["detail"] = (server.get("detail", "") + " no budget loaded").strip()
    return bridge, server


def check_scheduler(now: float | None = None) -> dict:
    now = now or time.time()
    try:
        hb = json.loads(HEARTBEAT_FILE.read_text())
    except FileNotFoundError:
        return {"status": "unknown", "detail": "no heartbeat yet (scheduler not running, or older version)"}
    except Exception as e:
        return {"status": "error", "detail": f"unreadable heartbeat: {e}"}
    age = now - float(hb.get("ts", 0))
    limit = max(3 * int(hb.get("poll_secs", 30)), 300)  # allow for long LLM calls mid-import
    out = {"status": "ok", "last_heartbeat": _iso(hb.get("ts", 0)), "age_s": round(age),
           "pending_files": hb.get("pending_files"), "last_file": hb.get("last_file"),
           "last_result": hb.get("last_result"), "last_run": hb.get("last_run")}
    if age > limit:
        out["status"] = "stale"
        out["detail"] = f"no heartbeat for {round(age)}s (limit {limit}s) — scheduler stuck or stopped"
    elif hb.get("last_result") == "error":
        out["status"] = "degraded"
        out["detail"] = f"last file failed: {hb.get('last_error', '')}"[:240]
    elif hb.get("config_ok") is False:
        out["status"] = "degraded"
        out["detail"] = "no Actual connection yet — load a budget once in the web UI"
    return out


def check_llm(refresh: bool = False, llm: LLMCategorizer | None = None, now: float | None = None) -> dict:
    now = now or time.time()
    llm = llm or LLMCategorizer()
    if not llm.enabled:
        return llm.ping()   # returns "disabled" without any network call
    cached = _llm_cache["result"]
    same_target = cached and cached.get("provider") == llm.provider and cached.get("model") == llm.model
    if not refresh and same_target and now - _llm_cache["ts"] < LLM_HEALTH_TTL:
        return {**cached, "cached": True, "checked_at": _iso(_llm_cache["ts"])}
    result = llm.ping()
    _llm_cache.update(result=result, ts=now)
    return {**result, "cached": False, "checked_at": _iso(now)}


def check_ghostfolio() -> dict:
    import ghostfolio
    if not ghostfolio.configured():
        return {"status": "disabled", "detail": "GHOSTFOLIO_URL / GHOSTFOLIO_TOKEN not set"}
    t0 = time.monotonic()
    s = ghostfolio.summary()
    ms = round((time.monotonic() - t0) * 1000)
    if s.get("error"):
        return {"status": "error", "latency_ms": ms, "detail": s["error"]}
    return {"status": "ok", "latency_ms": ms}


def overall(components: dict) -> str:
    critical = ("backend", "bridge")
    if any(components[c]["status"] == "error" for c in critical if c in components):
        return "error"
    if any(c["status"] in ("error", "degraded", "stale") for c in components.values()):
        return "degraded"
    return "ok"


async def full_report(bridge_url: str, refresh_llm: bool = False) -> dict:
    from starlette.concurrency import run_in_threadpool
    async with httpx.AsyncClient() as client:
        bridge, server = await check_bridge(bridge_url, client)
    components = {
        "backend": check_backend(),
        "bridge": bridge,
        "actual_server": server,
        "scheduler": check_scheduler(),
        "llm": await run_in_threadpool(check_llm, refresh_llm),
        "ghostfolio": await run_in_threadpool(check_ghostfolio),
    }
    return {"status": overall(components), "checked_at": _iso(time.time()), "components": components}
