"""
Audit events from this MCP server → the backend's audit trail (/audit/mcp).

One event per tool call and per sign-in/security event. Sending never blocks or breaks a
tool call: events go through a small queue to a background thread, and every event is also
printed as a JSON line to the container log, so the trail survives the backend being down.
Arguments are summarised (long strings → length, JSON lists → item count), never stored raw.
"""
from __future__ import annotations

import json
import os
import queue
import sys
import threading
import time

import httpx

API = os.getenv("BUDGET_APP_URL", "http://127.0.0.1:8000").rstrip("/")
SOURCE = "public" if os.getenv("MCP_PUBLIC_URL") else "private"
ENABLED = os.getenv("MCP_AUDIT", "true").lower() != "false"

_q: queue.Queue = queue.Queue(maxsize=1000)
_started = False
_lock = threading.Lock()


def summarise(value):
    if isinstance(value, str):
        s = value.strip()
        if s[:1] in "[{" and len(s) > 80:
            try:
                v = json.loads(s)
                if isinstance(v, dict):
                    v = next((x for x in v.values() if isinstance(x, list)), v)
                return f"<{len(v)} items>" if isinstance(v, list) else f"<json {len(s)} chars>"
            except ValueError:
                pass
        return s if len(s) <= 80 else f"<{len(s)} chars>"
    return value


def _worker():
    with httpx.Client(timeout=5, trust_env=False) as c:
        while True:
            e = _q.get()
            for attempt in range(3):
                try:
                    c.post(f"{API}/audit/mcp", json=e).raise_for_status()
                    break
                except httpx.HTTPError:
                    time.sleep(2 * (attempt + 1))


def emit(kind: str, name: str, **fields) -> None:
    if not ENABLED:
        return
    global _started
    e = {"ts": time.time(), "source": SOURCE, "kind": kind, "name": name, **fields}
    print("AUDIT " + json.dumps(e, default=str), file=sys.stderr, flush=True)
    with _lock:
        if not _started:
            threading.Thread(target=_worker, daemon=True, name="audit").start()
            _started = True
    try:
        _q.put_nowait(e)
    except queue.Full:
        pass                                         # still in the container log


def auth(name: str, ok: bool = True, client: str | None = None, **args) -> None:
    emit("auth", name, ok=ok, client=client, args=args)


def tool(name: str, args: dict, write: bool, ok: bool, error: str | None, duration_ms: int, client: str | None) -> None:
    emit("tool", name, args={k: summarise(v) for k, v in args.items()}, write=write, ok=ok,
         error=error, duration_ms=duration_ms, client=client)
