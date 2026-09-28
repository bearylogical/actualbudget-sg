"""
History — how YOU have categorised payees before, read from Actual.

Used in two ways by the categoriser (pipeline.enrich):
  1. lookup(): a payee you've put in the same category at least MIN_TIMES and at
     least MIN_SHARE of the time gets that category directly (source="history"),
     without an LLM call. Catches payees Actual never made a rule for.
  2. examples(): for payees with no usable history, the LLM is shown your own
     past payees that look similar (and a few typical payees per category) so it
     follows your conventions rather than generic ones.

Only rows you can vouch for count: categorised, on-budget, not transfers, not split
parents, not starting balances, and not AI guesses nobody confirmed (#llm in notes).

The history is built from the last HISTORY_DAYS of Actual and cached for
HISTORY_TTL seconds; refresh=True rebuilds it immediately.
"""
from __future__ import annotations

import os
import re
import threading
import time
from collections import Counter, defaultdict
from datetime import date, timedelta

MIN_TIMES = int(os.getenv("HISTORY_MIN_TIMES", "2"))
MIN_SHARE = float(os.getenv("HISTORY_MIN_SHARE", "0.8"))
HISTORY_DAYS = int(os.getenv("HISTORY_DAYS", "365"))
HISTORY_TTL = int(os.getenv("HISTORY_TTL", "300"))
MAX_CONF = 0.95


def norm(s: str | None) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def _grams(s: str) -> set[str]:
    s = re.sub(r"[^a-z0-9 ]", " ", norm(s))
    s = f"  {re.sub(r' +', ' ', s).strip()} "
    return {s[i:i + 3] for i in range(len(s) - 2)}


def _sim(a: set[str], b: set[str]) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


def trusted(t: dict) -> bool:
    return bool(t.get("category") and t.get("payee") and t.get("amount")
                and not t.get("offbudget") and not t.get("transfer_id")
                and not t.get("payee_transfer_acct") and not t.get("is_parent")
                and not t.get("starting_balance_flag")
                and "#llm" not in (t.get("notes") or ""))


class History:
    def __init__(self, rows: list[dict], cat_by_id: dict[str, dict]):
        self.cat_by_id = cat_by_id
        self.by_payee: dict[tuple[str, bool], Counter] = defaultdict(Counter)
        self.display: dict[str, str] = {}
        by_cat: dict[str, Counter] = defaultdict(Counter)
        for t in rows:
            if not trusted(t) or t["category"] not in cat_by_id:
                continue
            k = norm(t["payee"])
            self.by_payee[(k, t["amount"] > 0)][t["category"]] += 1
            self.display.setdefault(k, t["payee"])
            by_cat[t["category"]][t["payee"]] += 1
        self.by_cat = by_cat
        self._grams = {k: _grams(k) for k in self.display}

    def __len__(self):
        return sum(sum(c.values()) for c in self.by_payee.values())

    def lookup(self, payee: str, is_credit: bool) -> dict | None:
        c = self.by_payee.get((norm(payee), bool(is_credit)))
        if not c:
            return None
        total = sum(c.values())
        cat_id, n = c.most_common(1)[0]
        share = n / total
        if total < MIN_TIMES or share < MIN_SHARE:
            return None
        return {"category_id": cat_id, "confidence": round(min(MAX_CONF, share), 2), "times": total}

    def examples(self, payee: str, is_credit: bool, k: int = 6, min_sim: float = 0.3) -> list[dict]:
        """Your most similar past payees and what you called them (most-used category each)."""
        g = _grams(payee)
        scored = []
        for key, kg in self._grams.items():
            c = self.by_payee.get((key, bool(is_credit)))
            if not c:
                continue
            s = _sim(g, kg)
            if s >= min_sim:
                scored.append((s, key, c))
        scored.sort(key=lambda x: -x[0])
        out = []
        for s, key, c in scored[:k]:
            cat = self.cat_by_id.get(c.most_common(1)[0][0], {})
            out.append({"payee": self.display[key], "category": cat.get("name")})
        return out

    def category_examples(self, per: int = 3) -> dict[str, list[str]]:
        """A few typical payees for each of your categories."""
        return {self.cat_by_id[cid]["name"]: [p for p, _ in c.most_common(per)]
                for cid, c in self.by_cat.items() if cid in self.cat_by_id}


# ── cache ─────────────────────────────────────────────────────────────────────
_lock = threading.Lock()
_cached: tuple[float, History] | None = None


def get(ctx, refresh: bool = False, fetch=None) -> History | None:
    """History from Actual (cached). fetch(start, end) → rows; defaults to the bridge."""
    global _cached
    if ctx is None:
        return None
    with _lock:
        if not refresh and _cached and time.time() - _cached[0] < HISTORY_TTL:
            return _cached[1]
    try:
        if fetch is None:
            import bridge_client
            fetch = lambda s, e: bridge_client.txns(s, e)[0]
        end = date.today()
        rows = fetch((end - timedelta(days=HISTORY_DAYS)).isoformat(), end.isoformat())
    except Exception:
        return _cached[1] if _cached else None
    h = History(rows, ctx.cat_by_id)
    with _lock:
        _cached = (time.time(), h)
    return h


def invalidate():
    global _cached
    with _lock:
        _cached = None
