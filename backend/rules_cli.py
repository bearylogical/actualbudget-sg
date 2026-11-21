"""
Rules audit / Rules sync from the command line — a thin adapter over rules_audit.sync.

  docker compose exec backend python rules_cli.py audit              # markdown report
  docker compose exec backend python rules_cli.py audit --json > audit.json
  docker compose exec backend python rules_cli.py sync                # dry run (default)
  docker compose exec backend python rules_cli.py sync --apply --aliases --rules
  docker compose exec backend python rules_cli.py sync --apply --all  # everything incl. merges

Uses the budget loaded in the bridge, or reloads the saved connection (connect once in the
web UI; see connection.py) through the Actual gateway.
"""
import argparse
import json
import sys

import actual_gateway
from actual_rules import ActualContext
from rules_audit import PARTS, audit, sync, to_markdown
from taxonomy import load_aliases

FLAGS = {p: p.replace("_", "-") for p in PARTS}   # merge_payees → --merge-payees


def parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["audit", "sync"])
    ap.add_argument("--json", action="store_true", help="print the raw JSON report")
    ap.add_argument("--all-seed", action="store_true",
                    help="propose rules for every seed merchant, not only ones seen in your data")
    ap.add_argument("--apply", action="store_true", help="actually write (default is dry run)")
    for flag in FLAGS.values():
        ap.add_argument(f"--{flag}", action="store_true")
    ap.add_argument("--all", action="store_true", help="all parts except --broken")
    return ap


def parts_from(a: argparse.Namespace) -> dict:
    """CLI flags → the parts dict rules_audit.sync (and /rules/sync) take."""
    return {p: bool(getattr(a, p)) or (a.all and p != "broken") for p in PARTS}


def gateway():
    gw = actual_gateway.default()
    try:
        loaded = gw.ensure_loaded()
    except actual_gateway.GatewayError as e:
        sys.exit(str(e))
    if not loaded:
        sys.exit("Bridge has no budget loaded and no saved connection. Connect in the web UI first.")
    return gw


def main(argv=None):
    a = parser().parse_args(argv)
    gw = gateway()
    try:
        if a.cmd == "audit":
            report = audit(ActualContext.from_bridge(gw.context()), None, load_aliases(), a.all_seed)
            print(json.dumps(report, indent=2, default=str) if a.json else to_markdown(report))
            return
        out = sync(gw, parts_from(a), dry_run=not a.apply, all_seed=a.all_seed)
    except actual_gateway.GatewayError as e:
        sys.exit(f"Actual: {e}")

    for k, n in out["counts"].items():
        print(f"{k:18} {n}")
    if out["dryRun"]:
        print("\nDry run — nothing written. Add --apply to write.")
        for r in out["plan"]["createRules"][:40]:
            print("  +", r["why"])
        return
    print(json.dumps(out["result"], indent=2))
    print("\nAfter:")
    print(json.dumps(out["summary_after"], indent=2))


if __name__ == "__main__":
    main()
