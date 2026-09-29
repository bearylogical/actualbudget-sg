"""
Categorisation pipeline shared by the web backend and the scheduler.

For every parsed transaction, in priority order:

  1. Actual rules   — your own rules in Actual (incl. the ones Actual auto-learns
                      when you re-categorise). Previewed here with actual_rules.py;
                      Actual re-applies them for real on import.       source="actual"
  2. Your history  — a payee you've put in the same category ≥2× and ≥80% of the
                      time (history.py, read from Actual).              source="history"
  3. Seed rules     — merchants.py, mapped onto your Actual categories via
                      taxonomy.resolve_category (aliases → exact → legacy). source="seed"
                      A seed category with no Actual match falls through to the LLM.
  4. LLM fallback   — only for what's left, constrained to your categories and shown
                      your own similar past payees as examples.         source="llm"
  5. Nothing        — left uncategorised and tagged #review.

Transfers between your own accounts (card bill payments, Wise top-ups …) are never
categorised; PayNow/FAST to people are flagged #review instead of guessed, unless
your history says how you categorise that person.
"""
from __future__ import annotations

from actual_rules import ActualContext, apply_rules
from categorizer import UNCATEGORIZED, categorize
from llm import LLMCategorizer
from taxonomy import CATEGORIES, load_aliases, resolve_category

TAG_LLM = "#llm"
TAG_REVIEW = "#review"


def _allowed_categories(ctx: ActualContext | None) -> list[str]:
    if ctx and ctx.categories:
        return sorted({c["name"] for c in ctx.categories if not c.get("hidden")})
    return list(CATEGORIES)


def _examples(history, t: dict) -> dict:
    if history is None:
        return {}
    return {"examples": history.examples(t["payee"], bool(t.get("is_credit")))}


def enrich(transactions: list[dict], ctx: ActualContext | None = None, *,
           account_id: str | None = None, use_llm: bool = True,
           llm: LLMCategorizer | None = None, aliases: dict | None = None,
           history=None) -> dict:
    """history: history.History of your own past categorisations (optional)."""
    aliases = load_aliases() if aliases is None else aliases
    llm = llm or LLMCategorizer()
    out: list[dict] = []
    pending_llm: list[dict] = []

    for i, src in enumerate(transactions):
        t = dict(src)
        # recompute the seed guess from the raw text so enrich() is idempotent
        seed = categorize(t.get("match_text") or t.get("description", ""), bool(t.get("is_credit")),
                          t.get("merchant"))
        seed_cat = seed["category"] if seed["source"] == "seed" else None
        t.update(payee=seed["payee"], kind=seed["kind"], category=seed["category"],
                 confidence=seed["confidence"], source=seed["source"],
                 category_id=None, rule_ids=[], unmapped=False)
        # keep any manual choice made in the UI
        if src.get("source") == "manual":
            t.update(category=src.get("category"), category_id=src.get("category_id"),
                     payee=src.get("payee") or t["payee"], confidence=1.0, source="manual")
            out.append(t)
            continue

        # Backwards compatibility: older versions imported the raw statement text as the
        # payee. If that raw payee exists in Actual (and the clean one doesn't), keep
        # using it so rules Actual already learned for it keep firing. Merging raw
        # payees via the rules audit moves you onto the clean names.
        if ctx is not None and t["payee"].strip().lower() not in ctx.payee_by_name:
            legacy = ctx.payee_by_name.get(str(t.get("description", "")).strip().lower())
            if legacy:
                t["payee"] = legacy["name"]

        # 1. Actual rules
        if ctx is not None:
            amount_cents = round(abs(float(t.get("amount", 0))) * 100)
            p = ctx.payee_by_name.get(str(t["payee"]).strip().lower())
            res = apply_rules({
                "imported_payee": t.get("description", ""),
                "payee": t["payee"],
                "payee_id": p["id"] if p else None,
                "notes": "",
                "amount_cents": amount_cents if t.get("is_credit") else -amount_cents,
                "account_id": account_id,
            }, ctx)
            if res["payee"]:
                t["payee"] = res["payee"]
            if res["category_id"]:
                cat = ctx.cat_by_id[res["category_id"]]
                t.update(category=cat["name"], category_id=cat["id"], confidence=1.0,
                         source="actual", rule_ids=res["rule_ids"])
                out.append(t)
                continue

        # 2. your history: a payee you've consistently put in one category
        if history is not None and ctx is not None and t["kind"] != "transfer":
            h = history.lookup(t["payee"], bool(t.get("is_credit")))
            cat = ctx.cat_by_id.get(h["category_id"]) if h else None
            if cat:
                t.update(category=cat["name"], category_id=cat["id"], confidence=h["confidence"],
                         source="history", history_times=h["times"])
                out.append(t)
                continue

        # 3. seed merchant rules
        if seed_cat and seed_cat != UNCATEGORIZED:
            if ctx is not None:
                hit = resolve_category(seed_cat, ctx.categories, aliases)
                if hit:
                    t.update(category=hit["name"], category_id=hit["id"])
                else:
                    # seed knows the merchant but its category has no Actual match (map it in the
                    # UI to fix for good). Meanwhile let the LLM pick one of YOUR categories so the
                    # row isn't silently left uncategorised; it stays flagged if the LLM can't.
                    t.update(unmapped=True, seed_category=seed_cat)
                    pending_llm.append({"key": str(i), "payee": t["payee"],
                                        "description": t.get("description", ""),
                                        "is_credit": bool(t.get("is_credit")),
                            **_examples(history, t)})
            out.append(t)
            continue

        # transfers / p2p: don't guess
        if t["kind"] in ("transfer", "p2p"):
            t.update(category=UNCATEGORIZED, source=None, confidence=0.0)
            out.append(t)
            continue

        t.update(category=UNCATEGORIZED, source=None, confidence=0.0)
        pending_llm.append({"key": str(i), "payee": t["payee"],
                            "description": t.get("description", ""),
                            "is_credit": bool(t.get("is_credit")),
                            **_examples(history, t)})
        out.append(t)

    # 4. LLM fallback (with your own similar past payees as examples when history is available)
    llm_used = 0
    if use_llm and pending_llm and llm.enabled:
        # one question per unique payee+direction
        uniq: dict[str, dict] = {}
        for it in pending_llm:
            uniq.setdefault(LLMCategorizer.cache_key(it["payee"], it["is_credit"]), it)
        extra = {"category_examples": history.category_examples()} if history is not None else {}
        answers = llm.categorize(
            [{**it, "key": k} for k, it in uniq.items()], _allowed_categories(ctx), **extra)
        for it in pending_llm:
            ans = answers.get(LLMCategorizer.cache_key(it["payee"], it["is_credit"]))
            if not ans:
                continue
            t = out[int(it["key"])]
            hit = resolve_category(ans["category"], ctx.categories, aliases) if ctx is not None else None
            if t.get("unmapped"):
                if not hit:
                    continue       # keep the seed guess + unmapped flag
                t.update(unmapped=False)
            t.update(category=ans["category"], confidence=ans["confidence"], source="llm")
            if hit:
                t.update(category=hit["name"], category_id=hit["id"])
            llm_used += 1

    # tags for Actual notes
    for t in out:
        tags = []
        if t.get("source") == "llm":
            tags.append(TAG_LLM)
        if (t["kind"] == "p2p" and not t.get("category_id")) or \
                (t.get("category") == UNCATEGORIZED and t["kind"] != "transfer"):
            tags.append(TAG_REVIEW)
        t["notes"] = " ".join(tags)

    stats = {"total": len(out)}
    for key in ("actual", "history", "seed", "llm", "manual"):
        stats[key] = sum(1 for t in out if t.get("source") == key)
    stats["transfer"] = sum(1 for t in out if t["kind"] == "transfer")
    stats["review"] = sum(1 for t in out if TAG_REVIEW in t["notes"])
    stats["unmapped"] = sum(1 for t in out if t.get("unmapped"))
    return {"transactions": out, "stats": stats,
            "llm": {**llm.status(), "used": llm_used}}
