"""
The Actual gateway: what the backend and the scheduler need from the loaded Actual budget.

One interface, two adapters:
  HttpGateway   talks to actual-bridge (production). Reloads the saved connection when the
                bridge has lost its budget (bridge restart), so callers never see that case.
  FakeActual    in-memory, for tests (backend/tests/fake_actual.py).

Every method raises GatewayError when Actual can't be reached or refuses the request.
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
        body = {"accountId": account_id, "transactions": rows, "dryRun": dry_run, "verified": verified or {}}
        if reimport_deleted is not None:
            body["reimportDeleted"] = reimport_deleted
        return bridge_client.call("POST", "/import", body, timeout=120)

    def delete(self, ids: list[str]) -> dict:
        return bridge_client.call("POST", "/txns/delete", {"ids": ids}, timeout=60)

    def apply_actions(self, actions: list[dict]) -> dict:
        return bridge_client.apply_actions(actions)

    def create_rules(self, mappings: list[dict]) -> int:
        """mappings: [{payee, category_id}] → number of "payee is X → category" rules created."""
        body = {"mappings": [{"description": m["payee"], "categoryId": m["category_id"]} for m in mappings]}
        return int(bridge_client.call("POST", "/rules", body, timeout=60).get("created") or 0)

    def categorize(self, updates: list[dict]) -> dict[str, str]:
        """updates: [{id, category, notes?}], written in one call (one sync).
        → {txn id: error} for the rows that failed; the rest were updated."""
        r = bridge_client.call("POST", "/txns/update", {"updates": updates}, timeout=300)
        return {f["id"]: f["error"] for f in r.get("failed") or []}


_default = HttpGateway()


def default() -> HttpGateway:
    return _default
