"""In-memory Actual: the test adapter for the Actual gateway (actual_gateway.py).

Holds accounts, categories, payees, rules and transactions. Duplicate classification on import is a
plain port of actual-bridge/dedup.js (id match, then same date + amount against imported rows inside the
statement's range) — the real one is covered by actual-bridge/test/dedup.test.js.
"""
from __future__ import annotations

import copy
import itertools

from actual_gateway import GatewayError


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
        self.fail_updates: dict[str, str] = {}  # txn id → error categorize() reports for it
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

    @staticmethod
    def _cents(t: dict) -> int:
        c = round(float(t["amount"]) * 100)
        return c if t.get("is_credit") else -c

    def _classify(self, account_id: str, incoming: list[dict]):
        existing = [r for r in self.rows if r["account"] == account_id]
        by_id: dict[str, list] = {}
        for e in existing:
            if e["imported_id"]:
                by_id.setdefault(e["imported_id"], []).append(e)
        claimed, new, dup, verify, unmatched = set(), [], [], [], []
        for t in incoming:
            hit = None
            for i in [t.get("imported_id"), *(t.get("legacy_ids") or [])]:
                hit = next((e for e in by_id.get(i, []) if e["id"] not in claimed), None) if i else None
                if hit:
                    break
            if hit:
                claimed.add(hit["id"])
                dup.append(t)
            else:
                unmatched.append(t)
        dates = sorted(t["date"] for t in incoming)
        for t in unmatched:
            twin = next((e for e in existing if e["imported_id"] and e["id"] not in claimed
                         and e["date"] == t["date"] and dates[0] <= e["date"] <= dates[-1]
                         and e["amount"] == self._cents(t)), None)
            if twin:
                claimed.add(twin["id"])
                verify.append({**t, "reason": f"same date and amount already imported (id {twin['imported_id']})"})
            else:
                new.append(t)
        return new, dup, verify

    def import_rows(self, account_id, rows, *, dry_run=False, verified=None, reimport_deleted=None) -> dict:
        self._need_budget()
        self.calls.append(("import", account_id, dry_run, len(rows)))
        new, dup, verify = self._classify(account_id, rows)
        if dry_run:
            return {"ok": True, "dryRun": True, "added": len(new), "skipped": len(dup), "toVerify": verify,
                    "updated": 0, "errors": []}
        verified = verified or {}
        gone = self.deleted_ids.setdefault(account_id, set())
        added = []
        for t in new + [v for v in verify if verified.get(v["imported_id"]) == "import"]:
            if t["imported_id"] in gone and not reimport_deleted:
                continue
            gone.discard(t["imported_id"])
            r = self.add(account_id, t["date"], self._cents(t), t.get("payee") or t["description"],
                         t["imported_id"], category=t.get("category_id"), notes=t.get("notes") or None)
            added.append(r["id"])
        unreviewed = [v for v in verify if verified.get(v["imported_id"]) != "import"]
        return {"ok": True, "dryRun": False, "added": len(added), "updated": 0, "addedIds": added,
                "skipped": len(dup) + len(unreviewed), "errors": [],
                "unreviewed": [{"imported_id": v["imported_id"], "date": v["date"], "description": v["description"],
                                "amount": v["amount"], "reason": v["reason"]} for v in unreviewed]}

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
            elif a["type"] == "import":
                rows = a["transactions"]
                out.append(self.import_rows(a["account"], rows, reimport_deleted=True,
                                            verified={r["imported_id"]: "import" for r in rows}))
            elif a["type"] == "adjust":
                self._need_budget()
                self.add(a["account"], a["date"], int(a["amount_cents"]), a.get("payee", "Reconciliation adjustment"),
                         notes=a.get("note", "#reconcile"))
                out.append({"ok": True, "added": 1, "errors": []})
            elif a["type"] == "categorize":
                for u in a["updates"]:
                    for r in self.rows:
                        if r["id"] == u["id"]:
                            r["category"] = u["category"]
                out.append({"ok": True, "errors": []})
            else:
                raise ValueError(f"FakeActual can't {a['type']}")
        errors = [e for r in out for e in (r.get("errors") or [])]
        return {"ok": not errors, "results": out, "errors": errors}

    def create_rules(self, mappings) -> int:
        n = 0
        for m in mappings:
            key = (m["payee"].lower(), m["category_id"])
            if any((r["payee"].lower(), r["category_id"]) == key for r in self.learned):
                continue
            self.learned.append({"payee": m["payee"], "category_id": m["category_id"]})
            n += 1
        return n

    # ── connection ──
    def budgets(self, credentials: dict) -> list[dict]:
        self.calls.append(("budgets", credentials.get("serverURL")))
        return [{"id": "b1", "groupId": "sync-1", "name": "My Budget"}]

    def load_budget(self, credentials: dict) -> dict:
        self.calls.append(("load_budget", credentials.get("budgetId")))
        self.loaded = True
        return {"ok": True, "accounts": self._accounts_with_balance(), "categoryGroups": self.category_groups(),
                "payees": copy.deepcopy(self.payees), "rules": copy.deepcopy(self.rules)}

    def disconnect(self) -> None:
        self.calls.append(("disconnect",))
        self.loaded = False

    # ── budget reads ──
    def _accounts_with_balance(self) -> list[dict]:
        return [{**a, "balance": sum(r["amount"] for r in self.rows if r["account"] == a["id"])}
                for a in copy.deepcopy(self.accounts)]

    def list_accounts(self) -> list[dict]:
        self._need_budget()
        return self._accounts_with_balance()

    def category_groups(self) -> list[dict]:
        self._need_budget()
        groups: dict[str, dict] = {}
        for c in self.categories:
            g = c.get("group_name") or "General"
            groups.setdefault(g, {"id": f"g-{g}", "name": g, "is_income": bool(c.get("is_income")),
                                  "categories": []})["categories"].append(copy.deepcopy(c))
        return list(groups.values())

    def create_category(self, name: str, group_id: str | None = None, group_name: str | None = None) -> dict:
        self._need_budget()
        if not (group_id or group_name):
            raise GatewayError("groupId or groupName required", status=400)
        group = group_name or (group_id or "").removeprefix("g-")
        cat = {"id": f"c{next(self._ids)}", "name": name, "group_name": group}
        self.categories.append(cat)
        return {"id": cat["id"], "name": name, "group_id": f"g-{group}"}

    def list_payees(self) -> list[dict]:
        self._need_budget()
        return copy.deepcopy(self.payees)

    def list_rules(self) -> list[dict]:
        self._need_budget()
        return copy.deepcopy(self.rules)

    def budget_month(self, month: str) -> dict:
        self._need_budget()
        return {"budget": {"month": month, "categoryGroups": []}, "categoryGroups": self.category_groups()}

    def categorize(self, updates) -> dict[str, str]:
        self._need_budget()
        self.calls.append(("categorize", [u["id"] for u in updates]))
        failed = {}
        for u in updates:
            r = next((r for r in self.rows if r["id"] == u["id"]), None)
            if u["id"] in self.fail_updates or r is None:
                failed[u["id"]] = self.fail_updates.get(u["id"], "transaction not found")
                continue
            r["category"] = u["category"]
            if "notes" in u:
                r["notes"] = u["notes"]
        return failed
