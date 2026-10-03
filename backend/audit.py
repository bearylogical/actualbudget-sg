"""
Audit trail for the MCP servers (public connector + private), with a Telegram digest.

Both MCP containers POST one event per tool call and per sign-in/security event to
/audit/mcp. Events land in DATA_DIR/audit.db (SQLite, kept AUDIT_KEEP_DAYS). Every
TELEGRAM_DIGEST_HOURS the backend sends one aggregated message: calls per tool and per
client, every write call, errors, and security events (sign-ins, wrong passwords,
lockouts, refused registrations, blocked IPs). Nothing is sent when nothing happened.

Env:
  TELEGRAM_BOT_TOKEN      bot token from @BotFather
  TELEGRAM_CHAT_ID        your chat id (message the bot, then getUpdates)
  TELEGRAM_DIGEST_HOURS   default 24
  AUDIT_KEEP_DAYS         default 180

Event fields: ts, source (public|private), kind (tool|auth), name, client, ok, error,
duration_ms, write, args (summarised by the MCP server — no raw payloads).
"""
from __future__ import annotations

import html
import json
import os
import sqlite3
import threading
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

import httpx

DATA_DIR = Path(os.getenv("DATA_DIR", "/data"))
KEEP_DAYS = int(os.getenv("AUDIT_KEEP_DAYS", "180"))

# security events worth calling out (anything kind=auth is counted; these are flagged ⚠️)
ALERT_EVENTS = {"login_failed", "lockout", "refresh_replay", "registration_refused", "ip_blocked", "authorize_refused"}

_lock = threading.Lock()
_conn: sqlite3.Connection | None = None
_conn_path: Path | None = None


def _db() -> sqlite3.Connection:
    global _conn, _conn_path
    path = DATA_DIR / "audit.db"
    if _conn is None or _conn_path != path:
        path.parent.mkdir(parents=True, exist_ok=True)
        _conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        _conn.row_factory = sqlite3.Row
        _conn.executescript("""
        CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, ts REAL NOT NULL, source TEXT, kind TEXT,
          name TEXT, client TEXT, ok INTEGER, error TEXT, duration_ms INTEGER, write INTEGER, args TEXT);
        CREATE INDEX IF NOT EXISTS events_ts ON events(ts);
        CREATE TABLE IF NOT EXISTS state (k TEXT PRIMARY KEY, v TEXT);
        """)
        _conn_path = path
    return _conn


def _q(sql: str, args=()) -> list[sqlite3.Row]:
    with _lock:
        return _db().execute(sql, args).fetchall()


def record(e: dict) -> None:
    _q("INSERT INTO events (ts, source, kind, name, client, ok, error, duration_ms, write, args) VALUES (?,?,?,?,?,?,?,?,?,?)",
       (float(e.get("ts") or time.time()), str(e.get("source") or "?")[:20], str(e.get("kind") or "tool")[:10],
        str(e.get("name") or "?")[:80], (str(e["client"])[:80] if e.get("client") else None),
        1 if e.get("ok", True) else 0, (str(e["error"])[:300] if e.get("error") else None),
        int(e["duration_ms"]) if e.get("duration_ms") is not None else None, 1 if e.get("write") else 0,
        json.dumps(e.get("args") or {}, default=str)[:1000]))


def events(since: float, until: float | None = None) -> list[dict]:
    rows = _q("SELECT * FROM events WHERE ts >= ? AND ts < ? ORDER BY ts", (since, until or time.time() + 1))
    return [{**dict(r), "ok": bool(r["ok"]), "write": bool(r["write"]), "args": json.loads(r["args"] or "{}")} for r in rows]


def summarize(since: float, until: float | None = None) -> dict:
    ev = events(since, until)
    tools = [e for e in ev if e["kind"] == "tool"]
    auth = [e for e in ev if e["kind"] == "auth"]
    by_tool: dict[str, Counter] = defaultdict(Counter)
    for e in tools:
        by_tool[f"{e['name']} ({e['source']})"]["calls"] += 1
        by_tool[f"{e['name']} ({e['source']})"]["errors"] += 0 if e["ok"] else 1
    return {
        "since": since, "until": until or time.time(), "total_calls": len(tools),
        "by_tool": {k: dict(v) for k, v in sorted(by_tool.items(), key=lambda kv: -kv[1]["calls"])},
        "by_client": dict(Counter(f"{e['client'] or 'no client'} ({e['source']})" for e in tools).most_common()),
        "writes": [{"ts": e["ts"], "name": e["name"], "source": e["source"], "client": e["client"], "ok": e["ok"],
                    "args": e["args"], "error": e["error"]} for e in tools if e["write"]],
        "errors": dict(Counter(f"{e['name']}: {(e['error'] or '')[:80]}" for e in tools if not e["ok"]).most_common(10)),
        "auth": dict(Counter(e["name"] for e in auth).most_common()),
        "alerts": sum(1 for e in auth if e["name"] in ALERT_EVENTS),
    }


def _fmt_ts(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).astimezone().strftime("%d %b %H:%M")


def digest_text(s: dict) -> str | None:
    """Telegram HTML for a summary, or None when nothing happened."""
    if not s["total_calls"] and not s["auth"]:
        return None
    e = html.escape
    lines = [f"<b>{'⚠️ ' if s['alerts'] else ''}Budget MCP audit</b> · {_fmt_ts(s['since'])} → {_fmt_ts(s['until'])}",
             f"{s['total_calls']} tool calls, {len(s['writes'])} writes"]
    if s["auth"]:
        lines.append("\n<b>Sign-in & security</b>")
        lines += [f"{'⚠️ ' if k in ALERT_EVENTS else '• '}{e(k)}: {v}" for k, v in s["auth"].items()]
    if s["writes"]:
        lines.append("\n<b>Writes</b>")
        for w in s["writes"][:15]:
            args = ", ".join(f"{k}={v}" for k, v in (w["args"] or {}).items())[:120]
            lines.append(f"{'✅' if w['ok'] else '❌'} {_fmt_ts(w['ts'])} {e(w['name'])} ({e(w['source'])}) {e(args)}")
        if len(s["writes"]) > 15:
            lines.append(f"… and {len(s['writes']) - 15} more")
    if s["by_tool"]:
        lines.append("\n<b>Calls by tool</b>")
        lines += [f"• {e(k)}: {v['calls']}" + (f" ({v['errors']} failed)" if v.get("errors") else "")
                  for k, v in list(s["by_tool"].items())[:12]]
    if s["by_client"]:
        lines.append("\n<b>Clients</b>")
        lines += [f"• {e(k)}: {v}" for k, v in s["by_client"].items()]
    if s["errors"]:
        lines.append("\n<b>Errors</b>")
        lines += [f"• {e(k)} ×{v}" for k, v in s["errors"].items()]
    return "\n".join(lines)[:4000]


def telegram_configured() -> bool:
    return bool(os.getenv("TELEGRAM_BOT_TOKEN") and os.getenv("TELEGRAM_CHAT_ID"))


def send_telegram(text: str, transport: httpx.BaseTransport | None = None) -> None:
    token, chat = os.getenv("TELEGRAM_BOT_TOKEN", ""), os.getenv("TELEGRAM_CHAT_ID", "")
    with httpx.Client(timeout=20, transport=transport) as c:
        r = c.post(f"https://api.telegram.org/bot{token}/sendMessage",
                   json={"chat_id": chat, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True})
    if r.status_code >= 400:
        # never echo the URL: it contains the bot token
        raise RuntimeError(f"Telegram sendMessage failed ({r.status_code}): {r.text[:200]}")


def _state(k: str, v: str | None = None) -> str | None:
    if v is not None:
        _q("INSERT OR REPLACE INTO state VALUES (?,?)", (k, v))
        return v
    r = _q("SELECT v FROM state WHERE k=?", (k,))
    return r[0]["v"] if r else None


def digest(force: bool = False, send: bool = True, now: float | None = None,
           transport: httpx.BaseTransport | None = None) -> dict:
    """Send the digest for everything since the last one, if the interval has passed (or force)."""
    now = now or time.time()
    hours = float(os.getenv("TELEGRAM_DIGEST_HOURS", "24"))
    if _state("last_digest") is None:            # first run: start the clock, report from here on
        _state("last_digest", str(now - (hours * 3600 if force else 0)))
    last = float(_state("last_digest"))
    if not force and now - last < hours * 3600:
        return {"sent": False, "reason": "not due", "next_at": last + hours * 3600}
    s = summarize(last, now)
    text = digest_text(s)
    result = {"sent": False, "summary": s, "text": text}
    if send and text:
        if not telegram_configured():
            return {**result, "reason": "Telegram not configured (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID)"}
        send_telegram(text, transport)
        result["sent"] = True
    if send:
        _state("last_digest", str(now))          # an empty period counts as reported
        _q("DELETE FROM events WHERE ts < ?", (now - KEEP_DAYS * 86400,))
    return result
