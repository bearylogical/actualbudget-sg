"""
Canonical category taxonomy for Singapore household budgets.

These are the names the seed rules and the LLM fallback produce. They do NOT have
to exist in Actual under the same name: `resolve_category()` maps each canonical
name onto whatever you actually have in Actual, in this order:

  1. user aliases   (DATA_DIR/category_aliases.json, editable from the UI mapper)
  2. exact name     (case-insensitive)
  3. legacy names   (the 18 labels the old categorizer produced, so budgets that
                     were set up against the old labels keep working)
"""
from __future__ import annotations

import json
import os
from pathlib import Path

# group -> categories.  Order is display order.
TAXONOMY: dict[str, list[str]] = {
    "Food": ["Groceries", "Dining & Hawker", "Food Delivery", "Coffee & Drinks"],
    "Transport": ["Public Transport", "Ride-hailing & Taxi", "Car & Parking"],
    "Housing": ["Mortgage & Rent", "Utilities", "Telco & Internet",
                "Home & Furnishing", "Town Council & Conservancy"],
    "Shopping": ["Shopping", "Clothing & Shoes", "Electronics"],
    "Bills & Subscriptions": ["Subscriptions", "Software & Cloud", "Insurance"],
    "Health & Wellness": ["Medical & Pharmacy", "Fitness", "Personal Care"],
    "Lifestyle": ["Entertainment", "Travel", "Education", "Gifts & Donations"],
    "Financial": ["Bank Fees & Charges", "Taxes", "Claimable Expenses"],
    "Income": ["Salary", "Interest & Dividends", "Cashback & Rebates", "Other Income"],
}

INCOME_GROUPS = {"Income"}

CATEGORIES: list[str] = [c for cats in TAXONOMY.values() for c in cats]
GROUP_OF: dict[str, str] = {c: g for g, cats in TAXONOMY.items() for c in cats}

# canonical -> names the old categorizer used (checked when resolving against Actual)
LEGACY_NAMES: dict[str, list[str]] = {
    "Dining & Hawker": ["Food & Dining", "Dining", "Eating Out", "Restaurants", "Food"],
    "Food Delivery": ["Food & Dining"],
    "Coffee & Drinks": ["Food & Dining"],
    "Groceries": ["Grocery", "Supermarket"],
    "Public Transport": ["Transport", "Transportation"],
    "Ride-hailing & Taxi": ["Transport", "Transportation", "Taxi"],
    "Car & Parking": ["Transport", "Car", "Petrol", "Parking"],
    "Utilities": ["Bills & Utilities", "Bills"],
    "Telco & Internet": ["Bills & Utilities", "Phone", "Mobile", "Internet"],
    "Shopping": ["Shopping", "General Shopping"],
    "Clothing & Shoes": ["Shopping", "Clothing", "Apparel"],
    "Electronics": ["Electronics", "Shopping"],
    "Home & Furnishing": ["Shopping", "Home", "Household"],
    "Subscriptions": ["Subscriptions"],
    "Software & Cloud": ["Software & Cloud", "Subscriptions"],
    "Insurance": ["Insurance"],
    "Medical & Pharmacy": ["Health & Medical", "Medical", "Healthcare", "Health"],
    "Fitness": ["Health & Fitness", "Fitness", "Sports"],
    "Personal Care": ["Personal Care"],
    "Entertainment": ["Entertainment"],
    "Travel": ["Travel", "Holiday"],
    "Education": ["Education"],
    "Gifts & Donations": ["Donations", "Gifts", "Charity"],
    "Claimable Expenses": ["Work / Corporate", "Work", "Reimbursable"],
    "Salary": ["Income", "Salary", "Paycheck"],
    "Interest & Dividends": ["Interest", "Dividends", "Income"],
    "Cashback & Rebates": ["Cashback", "Rebates", "Income"],
    "Other Income": ["Income", "Other Income"],
}

DATA_DIR = Path(os.getenv("DATA_DIR", "/data"))
ALIASES_FILE = DATA_DIR / "category_aliases.json"


def load_aliases() -> dict[str, str]:
    """canonical name -> Actual category name (or id)."""
    try:
        return json.loads(ALIASES_FILE.read_text())
    except Exception:
        return {}


def save_aliases(aliases: dict[str, str]) -> None:
    ALIASES_FILE.parent.mkdir(parents=True, exist_ok=True)
    clean = {k: v for k, v in aliases.items() if k and v}
    ALIASES_FILE.write_text(json.dumps(clean, indent=2, sort_keys=True))


def resolve_category(name: str | None, actual_categories: list[dict],
                     aliases: dict[str, str] | None = None) -> dict | None:
    """Find the Actual category {id,name,...} for a canonical (or any) category name."""
    if not name or not actual_categories:
        return None
    aliases = aliases if aliases is not None else load_aliases()
    by_id = {c["id"]: c for c in actual_categories}
    by_name: dict[str, dict] = {}
    # prefer visible categories when two share a name
    for c in sorted(actual_categories, key=lambda c: bool(c.get("hidden"))):
        by_name.setdefault(c["name"].strip().lower(), c)

    candidates = []
    if name in aliases:
        candidates.append(aliases[name])
    candidates.append(name)
    candidates.extend(LEGACY_NAMES.get(name, []))
    for cand in candidates:
        if cand in by_id:
            return by_id[cand]
        hit = by_name.get(str(cand).strip().lower())
        if hit:
            return hit
    return None
