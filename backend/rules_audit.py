"""
Audit your live Actual rules/payees/categories against this app, and build a
sync plan. Nothing here writes — `plan` is handed to the bridge's /rules/apply.

Findings
  broken_rules        rules pointing at deleted categories / payees, or invalid regex
  duplicate_rules     identical conditions + actions (safe to delete extras)
  conflicting_rules   identical conditions, different category
  raw_payees          payees that are still raw statement text, grouped by the clean
                      name they should be merged into ("GRAB*A-12 SINGAPORE SG" → Grab)
  disagreements       payee rules in Actual that disagree with the seed table
                      (informational — Actual always wins, review by hand)
  missing_categories  taxonomy categories with no match in Actual, with the closest
                      existing name as an alias suggestion
  unsupported_rules   rules the preview can't evaluate (still work in Actual)

Plan (each part is opt-in)
  aliases             canonical → your Actual category name (stored locally)
  create_categories   taxonomy categories the seed rules need that you don't have
  merge_payees        raw payees → clean payee
  create_rules        for merchants that actually appear in your data:
                        pre:     imported_payee matches <regex> → set payee <Clean>
                        default: payee is <Clean>               → set category
                      skipped where Actual already has a rule for that payee
  delete_rules        duplicates (and, separately, broken rules)
"""
from __future__ import annotations

import difflib
import json
import re
from collections import defaultdict

from actual_rules import ActualContext, _eval_condition
from merchants import MERCHANTS
from payees import looks_raw, normalize_payee
from taxonomy import CATEGORIES, GROUP_OF, INCOME_GROUPS, resolve_category


def _sig(items) -> str:
    return json.dumps(sorted(
        [{"field": i.get("field"), "op": i.get("op"),
          "value": sorted(i["value"]) if isinstance(i.get("value"), list) else i.get("value")}
         for i in (items or [])], key=lambda d: json.dumps(d, sort_keys=True, default=str)),
        sort_keys=True, default=str)


def _cond_sig(rule) -> str:
    return f'{rule.get("stage") or "default"}|{rule.get("conditionsOp") or "and"}|{_sig(rule.get("conditions"))}'


def _category_action(rule):
    for a in rule.get("actions") or []:
        if a.get("field") == "category" and a.get("op") == "set":
            return a.get("value")
    return None


def _payee_action(rule):
    for a in rule.get("actions") or []:
        if a.get("field") == "payee" and a.get("op") == "set":
            return a.get("value")
    return None


def _examples(pattern: str, n: int = 3) -> str:
    alts = [re.sub(r"\\b|\\|\(\?[!=][^)]*\)|[\^$?*+()]|\[[^\]]*\]", "", a).replace("\\", "").strip()
            for a in pattern.split("|")]
    alts = [a for a in alts if a]
    more = f" +{len(alts) - n} more" if len(alts) > n else ""
    return "“" + "”, “".join(alts[:n]) + "”" + more


def describe_rule(rule: dict, ctx: ActualContext) -> str:
    def val(field, v):
        if field == "payee":
            ids = v if isinstance(v, list) else [v]
            return ", ".join(ctx.payee_by_id.get(i, {}).get("name", f"<missing {str(i)[:8]}>") for i in ids)
        if field == "category":
            return ctx.cat_by_id.get(v, {}).get("name", f"<missing {str(v)[:8]}>")
        return json.dumps(v) if isinstance(v, (list, dict)) else str(v)
    op = " OR " if rule.get("conditionsOp") == "or" else " AND "
    conds = op.join(f'{c.get("field")} {c.get("op")} {val(c.get("field"), c.get("value"))}'
                    for c in rule.get("conditions") or [])
    acts = ", ".join(f'{a.get("field")} → {val(a.get("field"), a.get("value"))}'
                     for a in rule.get("actions") or [])
    return f'[{rule.get("stage") or "default"}] {conds or "(always)"} ⇒ {acts}'


def audit(ctx: ActualContext, sample_descriptions: list[str] | None = None,
          aliases: dict | None = None, all_seed: bool = False) -> dict:
    aliases = aliases or {}
    rules = ctx.rules
    report: dict = {"broken_rules": [], "duplicate_rules": [], "conflicting_rules": [],
                    "raw_payees": [], "disagreements": [], "missing_categories": [],
                    "unsupported_rules": []}

    # ── broken / unsupported ─────────────────────────────────────────────────
    for r in rules:
        reasons = []
        cat = _category_action(r)
        if cat and cat not in ctx.cat_by_id:
            reasons.append("sets a category that no longer exists")
        pay = _payee_action(r)
        if pay and pay not in ctx.payee_by_id:
            reasons.append("sets a payee that no longer exists")
        for c in r.get("conditions") or []:
            if c.get("field") == "payee":
                ids = c["value"] if isinstance(c.get("value"), list) else [c.get("value")]
                if any(i not in ctx.payee_by_id for i in ids):
                    reasons.append("condition references a deleted payee")
            if c.get("op") == "matches":
                try:
                    re.compile(str(c.get("value")))
                except re.error:
                    reasons.append(f'invalid regex: {c.get("value")}')
        if reasons:
            report["broken_rules"].append({"rule_id": r.get("id"), "rule": describe_rule(r, ctx),
                                           "reasons": reasons})
        probe = {"imported_payee": "", "payee": "", "notes": "", "amount_cents": 0, "account_id": "x"}
        if any(_eval_condition(c, probe, ctx) is None for c in r.get("conditions") or []):
            report["unsupported_rules"].append({"rule_id": r.get("id"), "rule": describe_rule(r, ctx)})

    # ── duplicates & conflicts ───────────────────────────────────────────────
    by_cond = defaultdict(list)
    for r in rules:
        by_cond[_cond_sig(r)].append(r)
    for group in by_cond.values():
        if len(group) < 2:
            continue
        by_full = defaultdict(list)
        for r in group:
            by_full[_sig(r.get("actions"))].append(r)
        for dupes in by_full.values():
            if len(dupes) > 1:
                report["duplicate_rules"].append({
                    "keep": dupes[0].get("id"), "remove": [d.get("id") for d in dupes[1:]],
                    "rule": describe_rule(dupes[0], ctx)})
        cats = {_category_action(r) for r in group if _category_action(r)}
        if len(cats) > 1:
            report["conflicting_rules"].append({
                "rule_ids": [r.get("id") for r in group],
                "rules": [describe_rule(r, ctx) for r in group],
                "categories": [ctx.cat_by_id.get(c, {}).get("name", c) for c in cats]})

    # ── payee → category rules (simple "payee is X" rules) ───────────────────
    payee_rule_cat: dict[str, str] = {}
    payee_has_pre_rename: set[str] = set()
    for r in rules:
        conds = r.get("conditions") or []
        cat = _category_action(r)
        if (cat in ctx.cat_by_id and len(conds) == 1
                and conds[0].get("field") == "payee" and conds[0].get("op") == "is"):
            payee_rule_cat.setdefault(conds[0]["value"], cat)
        pay = _payee_action(r)
        if pay:
            payee_has_pre_rename.add(pay)

    # ── raw payees → merge groups ────────────────────────────────────────────
    groups = defaultdict(list)
    for p in ctx.payees:
        if p.get("transfer_acct") or not p.get("name"):
            continue
        target = normalize_payee(p["name"])
        if target and target.strip().lower() != p["name"].strip().lower():
            if looks_raw(p["name"]) or target.lower() in ctx.payee_by_name:
                groups[target].append(p)
    for target, ps in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        existing = ctx.payee_by_name.get(target.lower())
        report["raw_payees"].append({
            "target": target, "target_id": existing["id"] if existing else None,
            "payees": [{"id": p["id"], "name": p["name"]} for p in ps]})

    # ── disagreements (Actual payee rule vs seed table) ──────────────────────
    for pid, cat_id in payee_rule_cat.items():
        p = ctx.payee_by_id.get(pid)
        if not p or cat_id not in ctx.cat_by_id:
            continue
        m = next((m for m in MERCHANTS if m.category and m.applies(p["name"])), None)
        if not m:
            continue
        hit = resolve_category(m.category, ctx.categories, aliases)
        if hit and hit["id"] != cat_id:
            report["disagreements"].append({
                "payee": p["name"], "actual_category": ctx.cat_by_id[cat_id]["name"],
                "seed_category": hit["name"]})

    # ── missing taxonomy categories ──────────────────────────────────────────
    actual_names = [c["name"] for c in ctx.categories if not c.get("hidden")]
    missing = []
    for canon in CATEGORIES:
        if resolve_category(canon, ctx.categories, aliases):
            continue
        close = difflib.get_close_matches(canon, actual_names, n=1, cutoff=0.6)
        missing.append({"name": canon, "group": GROUP_OF[canon],
                        "suggested_alias": close[0] if close else None})
    report["missing_categories"] = missing

    # ── build plan ───────────────────────────────────────────────────────────
    corpus = [p["name"] for p in ctx.payees if p.get("name") and not p.get("transfer_acct")]
    corpus += list(sample_descriptions or [])
    relevant = [m for m in MERCHANTS
                if m.kind in ("expense", "income") and m.category
                and (all_seed or any(m.regex.search(s) for s in corpus))]

    alias_plan = {m["name"]: m["suggested_alias"] for m in missing if m["suggested_alias"]}
    eff_aliases = {**aliases, **alias_plan}
    needed_missing = {m.category for m in relevant
                      if not resolve_category(m.category, ctx.categories, eff_aliases)}
    create_categories = [{"name": c, "group": GROUP_OF[c], "is_income": GROUP_OF[c] in INCOME_GROUPS}
                         for c in CATEGORIES if c in needed_missing]

    def cat_ref(canon):
        hit = resolve_category(canon, ctx.categories, eff_aliases)
        return hit["id"] if hit else {"$category": canon}

    merge_sources = {g["target"].lower(): g["payees"] for g in report["raw_payees"]}
    create_rules = []
    existing_pre_patterns = {str(c.get("value")) for r in rules for c in (r.get("conditions") or [])
                             if c.get("field") == "imported_payee" and c.get("op") == "matches"}
    seen_payees = set()
    for m in relevant:
        if m.payee:
            existing = ctx.payee_by_name.get(m.payee.lower())
            if m.pattern not in existing_pre_patterns and not (existing and existing["id"] in payee_has_pre_rename):
                create_rules.append({
                    "why": f"rename matching statement text to payee '{m.payee}'",
                    "stage": "pre", "conditionsOp": "and",
                    "conditions": [{"field": "imported_payee", "op": "matches", "value": m.pattern}],
                    "actions": [{"field": "payee", "op": "set", "value": {"$payee": m.payee}}]})
            if m.payee.lower() in seen_payees:
                continue
            seen_payees.add(m.payee.lower())
            if existing and existing["id"] in payee_rule_cat:
                continue  # Actual already has (probably learned) a category for this payee
            if any(p["id"] in payee_rule_cat for p in merge_sources.get(m.payee.lower(), [])):
                continue  # a raw payee being merged in carries its learned rule over (mergePayees remaps rules)
            create_rules.append({
                "why": f"categorise payee '{m.payee}' as {m.category}",
                "stage": None, "conditionsOp": "and",
                "conditions": [{"field": "payee", "op": "is", "value": {"$payee": m.payee}}],
                "actions": [{"field": "category", "op": "set", "value": cat_ref(m.category)}]})
        else:
            if m.pattern in existing_pre_patterns:
                continue
            create_rules.append({
                "why": f"categorise statement text like {_examples(m.pattern)} as {m.category}",
                "stage": None, "conditionsOp": "and",
                "conditions": [{"field": "imported_payee", "op": "matches", "value": m.pattern}],
                "actions": [{"field": "category", "op": "set", "value": cat_ref(m.category)}]})

    plan = {
        "aliases": alias_plan,
        "create_categories": create_categories,
        "merge_payees": [g for g in report["raw_payees"]],
        "create_rules": create_rules,
        "delete_rules": [rid for d in report["duplicate_rules"] for rid in d["remove"]],
        "delete_broken_rules": [b["rule_id"] for b in report["broken_rules"]],
    }
    report["summary"] = {
        "rules": len(rules),
        "category_rules": sum(1 for r in rules if _category_action(r)),
        "payees": len(ctx.payees),
        "categories": len(ctx.categories),
        "broken": len(report["broken_rules"]),
        "duplicates": sum(len(d["remove"]) for d in report["duplicate_rules"]),
        "conflicts": len(report["conflicting_rules"]),
        "raw_payees": sum(len(g["payees"]) for g in report["raw_payees"]),
        "disagreements": len(report["disagreements"]),
        "missing_categories": len(missing),
        "proposed_rules": len(create_rules),
    }
    report["plan"] = plan
    return report


def to_markdown(report: dict) -> str:
    s = report["summary"]
    L = ["# Actual rules audit", "",
         f'{s["rules"]} rules ({s["category_rules"]} set a category) · {s["payees"]} payees · '
         f'{s["categories"]} categories', "",
         "| check | count |", "|---|---|",
         f'| broken rules | {s["broken"]} |', f'| duplicate rules (extra copies) | {s["duplicates"]} |',
         f'| conflicting rules | {s["conflicts"]} |', f'| raw payees to merge | {s["raw_payees"]} |',
         f'| payee rules disagreeing with seed table | {s["disagreements"]} |',
         f'| taxonomy categories not found | {s["missing_categories"]} |',
         f'| new rules proposed | {s["proposed_rules"]} |', ""]

    def section(title, rows):
        if rows:
            L.extend([f"## {title}", "", *rows, ""])

    section("Broken rules", [f'- {b["rule"]} — {"; ".join(b["reasons"])}' for b in report["broken_rules"]])
    section("Duplicate rules", [f'- {d["rule"]} (×{len(d["remove"]) + 1})' for d in report["duplicate_rules"]])
    section("Conflicting rules", [f'- same conditions, categories: {", ".join(c["categories"])}\n  - ' +
                                  "\n  - ".join(c["rules"]) for c in report["conflicting_rules"]])
    section("Raw payees → merge", [f'- **{g["target"]}**{"" if g["target_id"] else " (new)"} ← ' +
                                   ", ".join(f'`{p["name"]}`' for p in g["payees"][:6]) +
                                   (f' … +{len(g["payees"]) - 6}' if len(g["payees"]) > 6 else "")
                                   for g in report["raw_payees"]])
    section("Actual vs seed disagreements (Actual wins — check these)",
            [f'- {d["payee"]}: Actual = {d["actual_category"]}, seed = {d["seed_category"]}'
             for d in report["disagreements"]])
    section("Taxonomy categories not in Actual",
            [f'- {m["group"]} / {m["name"]}' + (f' → alias to **{m["suggested_alias"]}**?' if m["suggested_alias"] else "")
             for m in report["missing_categories"]])
    section("Proposed new rules", [f'- {r["why"]}' for r in report["plan"]["create_rules"]])
    section("Rules the preview can't evaluate (they still run in Actual)",
            [f'- {u["rule"]}' for u in report["unsupported_rules"][:30]])
    return "\n".join(L)
