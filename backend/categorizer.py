"""
Seed (offline) categorisation: merchant table + payee normalisation.

This is the *first guess* only. The full pipeline (pipeline.py) layers on top:
  1. your Actual rules          (source="actual")  — always win
  2. these seed merchant rules  (source="seed")
  3. optional LLM fallback      (source="llm")
  4. nothing                    (source=None, category "Uncategorized")

To add a merchant, edit merchants.py — not this file.
"""
from __future__ import annotations

from merchants import match_merchant
from payees import clean_description

UNCATEGORIZED = "Uncategorized"


def categorize(description: str, is_credit: bool | None = None,
               payee_hint: str | None = None) -> dict:
    """payee_hint: the bank's merchant-name field when the parser can isolate it
    (e.g. UOB's fixed-width 25-char merchant column) — gives cleaner payees."""
    m = match_merchant(description, is_credit)
    payee = m.payee if (m and m.payee) else clean_description(payee_hint or description)
    if m is None:
        return {"category": UNCATEGORIZED, "confidence": 0.0, "payee": payee,
                "kind": "income" if is_credit else "expense", "source": None}
    return {
        "category": m.category or UNCATEGORIZED,
        "confidence": m.confidence if m.category else 0.0,
        "payee": payee,
        "kind": m.kind,
        "source": "seed" if m.category else None,
    }


def categorize_transaction(description: str) -> tuple[str, float]:
    """Backwards-compatible (category, confidence) API."""
    r = categorize(description)
    return r["category"], r["confidence"]
