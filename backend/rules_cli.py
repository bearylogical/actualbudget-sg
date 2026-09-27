"""
Audit / sync your Actual rules from the command line.

  docker compose exec backend python rules_cli.py audit              # markdown report
  docker compose exec backend python rules_cli.py audit --json > audit.json
  docker compose exec backend python rules_cli.py sync                # dry run (default)
  docker compose exec backend python rules_cli.py sync --apply --aliases --rules
  docker compose exec backend python rules_cli.py sync --apply --all  # everything incl. merges

Uses the budget already loaded in the bridge (connect once in the web UI), or loads
one from ACTUAL_SERVER_URL / ACTUAL_PASSWORD / ACTUAL_BUDGET_ID
[/ ACTUAL_ENCRYPTION_PASSWORD] if set.
"""
import argparse
import json
import os
import sys

import httpx

from actual_rules import ActualContext
from rules_audit import audit, to_markdown
from taxonomy import load_aliases, save_aliases

BRIDGE = os.getenv("ACTUAL_BRIDGE_URL", "http://actual-bridge:3001")


def context() -> ActualContext:
    r = httpx.get(f"{BRIDGE}/context", timeout=60)
    if r.status_code == 400 and os.getenv("ACTUAL_SERVER_URL"):
        body = {"serverURL": os.environ["ACTUAL_SERVER_URL"],
                "password": os.environ.get("ACTUAL_PASSWORD", ""),
                "budgetId": os.environ.get("ACTUAL_BUDGET_ID", "")}
        if os.getenv("ACTUAL_ENCRYPTION_PASSWORD"):
            body["encryptionPassword"] = os.environ["ACTUAL_ENCRYPTION_PASSWORD"]
        httpx.post(f"{BRIDGE}/budgets/load", json=body, timeout=120).raise_for_status()
        r = httpx.get(f"{BRIDGE}/context", timeout=60)
    if not r.is_success:
        sys.exit(f"Bridge has no budget loaded ({r.status_code}). Connect in the web UI first, "
                 "or set ACTUAL_SERVER_URL / ACTUAL_PASSWORD / ACTUAL_BUDGET_ID.")
    return ActualContext.from_bridge(r.json())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["audit", "sync"])
    ap.add_argument("--json", action="store_true", help="print the raw JSON report")
    ap.add_argument("--all-seed", action="store_true",
                    help="propose rules for every seed merchant, not only ones seen in your data")
    ap.add_argument("--apply", action="store_true", help="actually write (default is dry run)")
    for part in ("aliases", "categories", "merge-payees", "rules", "duplicates", "broken"):
        ap.add_argument(f"--{part}", action="store_true")
    ap.add_argument("--all", action="store_true", help="all parts except --broken")
    a = ap.parse_args()

    ctx = context()
    report = audit(ctx, None, load_aliases(), a.all_seed)
    if a.cmd == "audit":
        print(json.dumps(report, indent=2, default=str) if a.json else to_markdown(report))
        return

    plan = report["plan"]
    on = lambda p: a.all or getattr(a, p.replace("-", "_"))
    selected = {
        "aliases": plan["aliases"] if on("aliases") else {},
        "createCategories": plan["create_categories"] if on("categories") else [],
        "mergePayees": plan["merge_payees"] if on("merge-payees") else [],
        "createRules": plan["create_rules"] if on("rules") else [],
        "deleteRules": (plan["delete_rules"] if on("duplicates") else [])
                       + (plan["delete_broken_rules"] if a.broken else []),
    }
    if not on("categories"):
        selected["createRules"] = [r for r in selected["createRules"]
                                   if not any(isinstance(x.get("value"), dict) and "$category" in x["value"]
                                              for x in r["actions"])]
    for k, v in selected.items():
        print(f"{k:18} {len(v)}")
    if not a.apply:
        print("\nDry run — nothing written. Add --apply to write.")
        for r in selected["createRules"][:40]:
            print("  +", r["why"])
        return
    if selected["aliases"]:
        save_aliases({**load_aliases(), **selected["aliases"]})
    payload = {k: v for k, v in selected.items() if k != "aliases"}
    if any(payload.values()):
        r = httpx.post(f"{BRIDGE}/rules/apply", json=payload, timeout=300)
        print(json.dumps(r.json(), indent=2))
    print("\nAfter:")
    print(json.dumps(audit(context(), None, load_aliases())["summary"], indent=2))


if __name__ == "__main__":
    main()
