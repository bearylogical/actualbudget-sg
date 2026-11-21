"""
The Actual gateway: the one seam the backend and the scheduler use to reach Actual.

Nothing else talks to actual-bridge (bridge_client is this module's transport). Routes call
default(); modules take a gateway= argument so tests can hand them FakeActual.

One interface, two adapters:
  HttpGateway   talks to actual-bridge (production). Reloads the saved connection when the
                bridge has lost its budget (bridge restart), so callers never see that case.
  FakeActual    in-memory, for tests (backend/tests/fake_actual.py).

Every method raises GatewayError when Actual can't be reached or refuses the request
(.status = the bridge's HTTP status or None, .timeout, .no_budget). main.py maps it to an HTTP
error once, for every route.
Amounts in Actual rows are signed integer cents (money in > 0).
"""
from __future__ import annotations

import httpx

import bridge_client

GatewayError = bridge_client.BridgeError


class HttpGateway:
    def ensure_loaded(self) -> bool:
        """True when a budget is loaded, reloading the saved connection if needed."""
        try:
            loaded = httpx.get(f"{bridge_client.BRIDGE_URL}/health", timeout=5).json().get("budgetLoaded")
        except Exception as e:
            raise GatewayError(f"actual-bridge unreachable: {e}") from e
        return bool(loaded) or bridge_client.reload_saved_budget()

    def context(self) -> dict:
        """{accounts, categoryGroups, categories, payees, rules} — feed to ActualContext.from_bridge."""
        return bridge_client.call("GET", "/context", timeout=60)

    def match_accounts(self, request: dict) -> list[dict]:
        """Open accounts with balance and the imported ids (from request["ids"]) each already holds."""
        return bridge_client.call("POST", "/accounts/match", request, timeout=90).get("accounts", [])

    def txns(self, start: str, end: str, account_ids: list[str] | None = None) -> tuple[list[dict], list[dict]]:
        return bridge_client.txns(start, end, account_ids)

    def import_rows(self, account_id: str, rows: list[dict], *, dry_run: bool = False,
                    verified: dict[str, str] | None = None, reimport_deleted: bool | None = None) -> dict:
        """Import statement rows. → {added, updated, skipped, addedIds, toVerify (dry run) / unreviewed, errors}.
        Rows matching an existing one only by date + amount are held back unless verified[imported_id] == "import"."""
        return bridge_client.import_rows(account_id, rows, dry_run=dry_run, verified=verified,
                                         reimport_deleted=reimport_deleted)

    def delete(self, ids: list[str]) -> dict:
        return bridge_client.call("POST", "/txns/delete", {"ids": ids}, timeout=60)

    def apply_actions(self, actions: list[dict]) -> dict:
        return bridge_client.apply_actions(actions)

    def create_rules(self, mappings: list[dict]) -> int:
        """mappings: [{payee, category_id}] → number of "payee is X → category" rules created."""
        body = {"mappings": [{"description": m["payee"], "categoryId": m["category_id"]} for m in mappings]}
        return int(bridge_client.call("POST", "/rules", body, timeout=60).get("created") or 0)

    # ── connection (the UI's Actual sidebar) ──
    def budgets(self, credentials: dict) -> list[dict]:
        """credentials: {serverURL, password} → the budgets on that Actual server."""
        return bridge_client.call("POST", "/budgets", credentials, timeout=30).get("budgets", [])

    def load_budget(self, credentials: dict) -> dict:
        """credentials: {serverURL, password, budgetId, encryptionPassword?} → {accounts, categoryGroups, payees, rules}."""
        return bridge_client.call("POST", "/budgets/load", credentials, timeout=60)

    def disconnect(self) -> None:
        bridge_client.call("POST", "/reset", {}, timeout=30)

    # ── budget reads for the UI ──
    def list_accounts(self) -> list[dict]:
        """Every account with its balance (signed cents)."""
        return bridge_client.call("GET", "/accounts", timeout=30).get("accounts", [])

    def category_groups(self) -> list[dict]:
        """[{id, name, is_income, categories: [...]}]"""
        return bridge_client.call("GET", "/categories", timeout=30).get("categoryGroups", [])

    def create_category(self, name: str, group_id: str | None = None, group_name: str | None = None) -> dict:
        """Create a category in group_id, or in group_name (created if missing) → {id, name, group_id}."""
        body = {"name": name, "groupId": group_id, "groupName": group_name}
        return bridge_client.call("POST", "/categories", body, timeout=30)

    def list_payees(self) -> list[dict]:
        return bridge_client.call("GET", "/payees", timeout=30).get("payees", [])

    def list_rules(self) -> list[dict]:
        """Actual-format rules."""
        return bridge_client.call("GET", "/rules", timeout=30).get("rules", [])

    def budget_month(self, month: str) -> dict:
        """month "YYYY-MM" → {budget, categoryGroups}."""
        return bridge_client.call("GET", f"/budget-month/{month}", timeout=30)

    def categorize(self, updates: list[dict]) -> dict[str, str]:
        """updates: [{id, category, notes?}], written in one call (one sync).
        → {txn id: error} for the rows that failed; the rest were updated."""
        r = bridge_client.call("POST", "/txns/update", {"updates": updates}, timeout=300)
        return {f["id"]: f["error"] for f in r.get("failed") or []}

    def apply_rules(self, plan: dict) -> dict:
        """Rules sync writes. plan: {createCategories, mergePayees, createRules, deleteRules}
        → {categories, merged, rules, deleted, errors}."""
        return bridge_client.call("POST", "/rules/apply", plan, timeout=300)

    def finance_summary(self, months: int) -> dict:
        """Open accounts with balances, the last `months` budget months (income, spent, budgeted,
        per-category budgeted/spent/balance — Actual's budget engine, only reachable from the bridge),
        this month's uncategorised on-budget rows, and the bridge's today. → {accounts, months, uncategorised, today}"""
        return bridge_client.call("GET", f"/finance/summary?months={int(months)}")


_default = HttpGateway()


def default() -> HttpGateway:
    return _default
