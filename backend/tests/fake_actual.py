"""In-memory Actual: the test adapter for the Actual gateway (actual_gateway.py).

Holds accounts, categories, payees, rules and transactions. Imports go through money.to_actual like the
real gateway, and duplicate classification (`classify`) is a port of actual-bridge/dedup.js. Both run
the same golden cases, actual-bridge/test/fixtures/classify.json (test_classify_golden.py and
dedup.test.js), so the port cannot drift.
"""
from __future__ import annotations

import copy
import itertools

import money
from actual_gateway import GatewayError


def classify(incoming: list[dict], existing: list[dict]) -> tuple[list, list, list]:
    """dedup.js classify, line for line. incoming: Actual rows (money.to_actual); existing: rows in
    Actual. → (clearlyNew, clearlyDup, needsVerify) — needsVerify rows carry `reason`."""
    by_id: dict[str, list] = {}
    for e in existing:
        if e.get("imported_id"):
            by_id.setdefault(e["imported_id"], []).append(e)
    claimed, new, dup, verify, unmatched = set(), [], [], [], []
    for t in incoming:
        legacy = t.get("legacy_ids") if isinstance(t.get("legacy_ids"), list) else []
        hit = None
        for i in [t.get("imported_id"), *legacy]:
            if not i:
                continue
            hit = next((e for e in by_id.get(i, []) if e["id"] not in claimed), None)
            if hit:
                break
        if hit:
            claimed.add(hit["id"])
            dup.append(t)
        else:
            unmatched.append(t)
    dates = sorted(t["date"] for t in incoming if t.get("date"))
    lo, hi = (dates[0], dates[-1]) if dates else (None, None)
    for t in unmatched:
        twin = next((e for e in existing if e.get("imported_id") and e["id"] not in claimed
                     and e["date"] == t.get("date") and lo is not None and lo <= e["date"] <= hi
                     and e["amount"] == t["amount"]), None)
        if twin:
            claimed.add(twin["id"])
            verify.append({**t, "reason": "same date and amount already imported "
                                          f"(id {str(twin['imported_id'])[:24]})"})
        else:
            new.append(t)
    return new, dup, verify


class FakeActual:
    def __init__(self, accounts=None, categories=None, loaded: bool = True):
        self.accounts = accounts or []          # [{id, name, closed?, offbudget?}]
        self.categories = categories or []      # [{id, name, group_name?, is_income?, hidden?}]
        self.payees: list[dict] = []
        self.rules: list[dict] = []             # Actual-format rules (ActualContext evaluates these)
        self.learned: list[dict] = []           # "payee is X → category" rules from create_rules
        self.rows: list[dict] = []              # bridge /txns/range shape
        self.deleted_ids: dict[str, set] = {}   # account → imported_ids deleted (Actual remembers these)
        self.loaded = loaded
        self.calls: list[tuple] = []
        self._ids = itertools.count(1)

    # ── setup helpers ──
    def add(self, account: str, date: str, amount_cents: int, payee: str = "", imported_id: str | None = None,
            **extra) -> dict:
        row = {"id": f"t{next(self._ids)}", "account": account, "account_name": self._name(account),
               "offbudget": False, "date": date, "amount": amount_cents, "payee": payee or None, "payee_id": None,
               "payee_transfer_acct": None, "imported_payee": None, "imported_id": imported_id,
               "category": None, "transfer_id": None, "notes": None, "is_parent": False, "is_child": False,
               "starting_balance_flag": False, **extra}
        self.rows.append(row)
        return row

    def _name(self, account_id: str) -> str:
        return next((a["name"] for a in self.accounts if a["id"] == account_id), account_id)

    def state(self) -> list[tuple]:
        """What's in Actual, comparable across runs (no generated ids)."""
        return sorted((r["account"], r["date"], r["amount"], r["imported_id"] or "", r["category"] or "",
                       r["payee"] or "") for r in self.rows)

    # ── the gateway interface ──
    def ensure_loaded(self) -> bool:
        return self.loaded

    def _need_budget(self):
        if not self.loaded:
            raise GatewayError("No budget loaded")

    def context(self) -> dict:
        self._need_budget()
        return {"accounts": copy.deepcopy(self.accounts), "categories": copy.deepcopy(self.categories),
                "payees": copy.deepcopy(self.payees), "rules": copy.deepcopy(self.rules), "categoryGroups": []}

    def match_accounts(self, request: dict) -> list[dict]:
        self._need_budget()
        ids = set(request.get("ids") or [])
        return [{"id": a["id"], "name": a["name"], "offbudget": bool(a.get("offbudget")), "closed": False,
                 "balance": sum(r["amount"] for r in self.rows if r["account"] == a["id"]),
                 "matchedIds": [r["imported_id"] for r in self.rows
                                if r["account"] == a["id"] and r["imported_id"] in ids]}
                for a in self.accounts if not a.get("closed")]

    def txns(self, start: str, end: str, account_ids=None):
        self._need_budget()
        rows = [dict(r) for r in self.rows if start <= r["date"] <= end
                and (not account_ids or r["account"] in account_ids)]
        return rows, [{"id": a["id"], "name": a["name"], "offbudget": bool(a.get("offbudget")),
                       "closed": bool(a.get("closed"))} for a in self.accounts]

    def import_rows(self, account_id, rows, *, dry_run=False, verified=None, reimport_deleted=None) -> dict:
        self._need_budget()
        self.calls.append(("import", account_id, dry_run, len(rows)))
        sent = [money.to_actual(t) for t in rows]
        new, dup, verify = classify(sent, [r for r in self.rows if r["account"] == account_id])
        if dry_run:
            return money.restate_import(rows, {"ok": True, "dryRun": True, "added": len(new), "skipped": len(dup),
                                               "toVerify": verify, "updated": 0, "errors": []})
        verified = verified or {}
        gone = self.deleted_ids.setdefault(account_id, set())
        added = []
        for t in new + [v for v in verify if verified.get(v.get("imported_id")) == "import"]:
            if t.get("imported_id") in gone and not reimport_deleted:
                continue
            gone.discard(t.get("imported_id"))
            r = self.add(account_id, t["date"], t["amount"], t["payee_name"] or t["imported_payee"],
                         t.get("imported_id"), category=t.get("category"), notes=t["notes"] or None,
                         imported_payee=t["imported_payee"] or None)
            added.append(r["id"])
        unreviewed = [v for v in verify if verified.get(v.get("imported_id")) != "import"]
        return money.restate_import(rows, {
            "ok": True, "dryRun": False, "added": len(added), "updated": 0, "addedIds": added,
            "skipped": len(dup) + len(unreviewed), "errors": [],
            "unreviewed": [{"imported_id": v.get("imported_id"), "date": v["date"],
                            "imported_payee": v["imported_payee"], "amount": v["amount"], "reason": v["reason"]}
                           for v in unreviewed]})

    def delete(self, ids) -> dict:
        self._need_budget()
        ids = set(ids)
        for r in self.rows:
            if r["id"] in ids and r["imported_id"]:
                self.deleted_ids.setdefault(r["account"], set()).add(r["imported_id"])
        before = len(self.rows)
        self.rows = [r for r in self.rows if r["id"] not in ids]
        return {"ok": True, "deleted": before - len(self.rows), "errors": []}

    def apply_actions(self, actions) -> dict:
        out = []
        for a in actions:
            if a["type"] == "delete":
                out.append(self.delete(a["ids"]))
            elif a["type"] == "link":
                for r in self.rows:
                    if r["id"] == a["keep_id"]:
                        r["transfer_id"] = a["other_id"]
                    if r["id"] == a["other_id"]:
                        r["transfer_id"] = a["keep_id"]
                out.append({"ok": True, "errors": []})
            elif a["type"] == "categorize":
                for u in a["updates"]:
                    for r in self.rows:
                        if r["id"] == u["id"]:
                            r["category"] = u["category"]
                out.append({"ok": True, "errors": []})
            else:
                raise ValueError(f"FakeActual can't {a['type']}")
        return {"ok": True, "results": out, "errors": []}

    def create_rules(self, mappings) -> int:
        n = 0
        for m in mappings:
            key = (m["payee"].lower(), m["category_id"])
            if any((r["payee"].lower(), r["category_id"]) == key for r in self.learned):
                continue
            self.learned.append({"payee": m["payee"], "category_id": m["category_id"]})
            n += 1
        return n
