# Design & workflow review — Sep 2026

This review was done against real UOB card (`CC_TXN_History…`, 302 rows) and POSB
(`transaction_history…`, 33 rows) exports.

## Target workflow

```
UOB .xls ─┐                       ┌─ Actual rules (learned + synced)   ← source of truth
POSB .csv ├─▶ parse ─▶ clean payee ├─ seed merchant table (SG)
(watch/UI)┘                       └─ LLM fallback (optional, constrained)
                                          │
                     import (dedup by bank ref) ─▶ Actual ─▶ review #review / #llm ─▶ re-categorise
                                                                  │
                                         Actual learns payee rule ◀┘  (next import is right)
```

Monthly loop: drop both exports into the watch folder (or the UI). In Actual, filter
notes for `#review` / `#llm` and fix anything wrong. Actual learns from those fixes.
Run **Rules audit** now and then.

## What was wrong, and what changed

| Area | Problem | Fix |
|---|---|---|
| Matching | Substring regex with no word boundaries: `gap` matched **SinGAPore**, so every "… SINGAPORE SG" line scored 0.95 *Shopping*. Also `gas`→Vegas, `power`→powerbank, `nus`→bonus, `ntu`→adventure | Word-boundary rules in `merchants.py`, first match wins, sign-aware (income only on credits). Tuned on your data: UOB uncategorised went from 112 → 26 |
| Payees | Raw description became the payee, so each Grab ride was a new payee and Actual's learned rules never fired again | `payees.py` cleans names (processor prefixes, branch suffixes, refs, locations). The raw text is kept in `imported_payee`. Actual title-casing is turned off (`payeeNameNormalization: 'original'`) |
| Source of truth | App categories and Actual rules were separate. The scheduler imported with **no category** (only in notes), and `learnCategories` was ignored | One pipeline (`pipeline.py`) for UI and scheduler: Actual rules → seed → LLM. Categories resolve to Actual IDs through aliases, exact names, or legacy names |
| Learning | UI category edits were lost after import. The bridge `/rules` endpoint was dead code | *Learn rules* turns manual edits into `payee is X → category` rules |
| Mapping | Category map lived only in page memory and was rebuilt by a word-overlap heuristic | Aliases saved on the server (`/data/category_aliases.json`) and used by the scheduler too |
| POSB | CSV upload rejected (`.xls/.xlsx` only). Interest rows dropped (blank description). Dedup keyed on `Transaction Ref1`, so two same-day NETS top-ups collided and one was **silently skipped**. Pending rows imported | Content sniffing (xls/xlsx/csv/html). Digibank parser keys on code + Ref1-3, skips non-settled rows, keeps interest, and pulls payees from `To:`/`From:`/`IB:` |
| UOB | File is legacy `.xls` named `.csv`. `Ref No` on line 2 was ignored, so hash dedup was used | Sniffed as xls (xlrd, LibreOffice fallback). `ref-<Ref No>` IDs, with the old hash kept as a legacy ID so earlier imports still dedupe |
| Scheduler | Every file went to one `ACTUAL_ACCOUNT_ID`, so UOB and POSB would land in the same account | `ACCOUNT_ROUTES` by bank or filename (account name or id) |
| Transfers | "PAYMT THRU E-BANK" was categorised as *Payment / Transfer* spending | Transfers are `kind=transfer` and never categorised. PayNow/FAST are `#review` |
| Exposure | Backend `:8000` published on the LAN with `CORS *`, proxying your loaded budget with no auth | Bound to `127.0.0.1`, CORS limited to the UI origin |

## Rules audit — what it checks

- Broken rules (deleted category or payee, bad regex)
- Duplicate rules
- Conflicting rules (same conditions, different category)
- Raw payees to merge
- Places where your rules disagree with the seed table
- Taxonomy gaps, with alias suggestions

It proposes pre-stage rename rules and payee → category rules, but **only for merchants seen
in your data**. A rule is skipped when Actual already has one for that payee.

Checked against `@actual-app/api` 26.9 in an offline budget:

- `imported_payee matches` rules with lookaheads run on import.
- `mergePayees` **rewrites existing rules** onto the target payee, so merging keeps what Actual learned.
- The audit therefore skips creating a category rule when a merged-in raw payee already has one.

## Recommended next steps (not done)

1. **Pin `@actual-app/api`** to your server version in `actual-bridge/package.json`. It is
   `latest` today, so a rebuild can break with `out-of-sync-migrations` after a server update.
2. **Transfer rules.** Make `PAYMT THRU E-BANK…` (UOB) and `UOB:…:I-BANK` (POSB) set the payee
   to the matching Actual transfer payee, so the two sides link automatically. This needs your
   account names, so it is left manual.
3. **Wise.** Add a parser for the Wise statement CSV, or pull from the Wise API with a personal
   token, instead of manual entry.
4. **Split bills.** Incoming PayNow from friends settling a meal is best booked to the same
   category (it reduces Dining). Consider an Actual rule per friend, or tag these on review.
5. **UI.** `+page.svelte` is ~750 lines. Split it into table, import bar and summary components.
6. **Frontend auth.** `:3000` asks for the Actual password in the browser. Keep it behind
   Tailscale or Traefik forward-auth rather than on the open LAN.
