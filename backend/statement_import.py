"""
Statement import: one Statement on its way into one Actual account (an Import session).

The web UI and the watch folder both go through this module:

    sid = open_session(source, filename, content)     parse + record in import history
    preview(sid, account_id?, ...)                     account choice, categories, duplicate check
    commit(sid, account_id, ...)                       import, then everything that follows it
    reconcile(sid, balance?, ...)                      re-run reconcile with a corrected balance
    undo(sid)                                          delete the rows the commit added

    import_unattended(...)                             a Watch-folder import: all of the above, no decisions

The session's state lives in its import-history row (import_log.py), so it survives a restart.
Decisions a person makes in the UI are sent as data, never as edited rows:

    categories        {imported_id: {"category": name, "category_id": id|None}}   a manual pick
                      {imported_id: {"confirmed": True}}                          an accepted AI guess
    include_verified  imported_ids of possible duplicates to import anyway; None = nobody decided,
                      so possible duplicates are held in the review queue
    exclude           imported_ids to leave out (e.g. credits)

commit(): everything before the import into Actual raises and changes nothing. The import is the
point of no return; each step after it (remember account, hold duplicates, learn rules, scan,
reconcile) is best-effort and reports its own error in result["steps"]. Committing an already
committed session returns the stored result.
"""
from __future__ import annotations

import hashlib
import re

import accounts as acct
import actual_gateway
import history
import import_log
import reconcile as recon
import review
import review_scan
from actual_rules import ActualContext
from actual_gateway import GatewayError
from parsers import parse_statement
from pipeline import enrich


class StatementImportError(Exception):
    status = 400


class NotFound(StatementImportError):
    status = 404


class Conflict(StatementImportError):
    """Refused: the wrong-account guard, or an undo that isn't possible."""
    status = 409


class ParseFailed(StatementImportError):
    status = 422


class NotConnected(StatementImportError):
    status = 400


COMMITTED = {"imported", "nothing_new"}


# ── Legacy IDs ───────────────────────────────────────────────────────────────
# Rows imported by older versions carry ids in one of these formats; every import sends all of
# them so a Statement matches its earlier imports however it was imported then.

def _js_num(x) -> str:
    """A number formatted the way JavaScript's String(number) does (12 → "12", 12.5 → "12.5")."""
    f = float(x)
    return str(int(f)) if f.is_integer() else repr(f)


def legacy_ids(t: dict) -> list[str]:
    date, desc, amount = t["date"], t["description"], t["amount"]
    web_stmt = re.sub(r"\s+", "-", f"stmt-{date}-{desc}-{_js_num(amount)}")          # old web UI
    web_hash = hashlib.sha256(f"{date}|{desc}|{_js_num(abs(float(amount)))}".encode()).hexdigest()[:16]
    watch = f"auto-{date}-{desc}-{amount}".replace(" ", "-")                          # old scheduler
    return [web_stmt, web_hash, watch]


def _with_legacy_ids(t: dict) -> dict:
    have = list(t.get("legacy_ids") or [])
    return {**t, "legacy_ids": have + [i for i in legacy_ids(t) if i not in have and i != t.get("imported_id")]}


# ── Session ──────────────────────────────────────────────────────────────────

def open_session(source: str, filename: str, content: bytes) -> str:
    """Parse a statement file and record it. source: "ui" | "scheduler". → session id."""
    sid = import_log.start(source, filename, content)
    try:
        txns, _bank, info = parse_statement(content, filename)
    except Exception as e:
        import_log.fail(sid, "parse", e)
        raise ParseFailed(f"Could not parse statement: {e}") from e
    import_log.set_statement(sid, info, len(txns))
    import_log.update(sid, rows=[_with_legacy_ids(t) for t in txns])
    return sid


def _load(sid: str) -> dict:
    rec = import_log.get(sid)
    if not rec or rec.get("rows") is None:
        raise NotFound("No such import session")
    return rec


def _strip(notes: str | None, *tags: str) -> str:
    for tag in tags:
        notes = (notes or "").replace(tag, "")
    return re.sub(r"\s+", " ", notes or "").strip()


def _apply_categories(rows: list[dict], categories: dict | None, ctx: ActualContext | None) -> list[dict]:
    if not categories:
        return rows
    out = []
    for t in rows:
        d = categories.get(t.get("imported_id"))
        if not d:
            out.append(t)
            continue
        t = dict(t)
        if "category" in d or "category_id" in d:        # a manual pick
            cid = d.get("category_id") or None
            name = d.get("category") or (ctx.cat_by_id.get(cid, {}).get("name") if ctx and cid else None)
            t.update(category=name or t.get("category"), category_id=cid, source="manual", confidence=1.0,
                     unmapped=False, notes=_strip(t.get("notes"), "#llm", "#review"))
        if d.get("confirmed"):                            # an accepted AI guess is your choice now
            t.update(confirmed=True, notes=_strip(t.get("notes"), "#llm"))
        out.append(t)
    return out


def _connect(gateway, rows: list[dict]):
    """→ (ctx, match_rows) or raises GatewayError / NotConnected."""
    if not gateway.ensure_loaded():
        raise NotConnected("No Actual budget loaded — connect and load a budget first")
    ctx = ActualContext.from_bridge(gateway.context())
    return ctx, gateway.match_accounts(acct.match_request(rows))


def _no_recommendation(info: acct.StatementInfo) -> dict:
    return {"statement": info.to_dict(), "suggestions": [], "recommended": None, "auto_select": False,
            "remembered": acct.load_map().get(info.fingerprint, {}).get("account_id")}


def preview(sid: str, account_id: str | None = None, *, use_llm: bool = True, refresh: bool = False,
            categories: dict | None = None, exclude: list[str] | None = None,
            routes: dict | None = None, fallback: str | None = None, gateway=None) -> dict:
    """What committing would do. Without account_id the account is chosen as a Watch-folder import
    would (remembered → routes → confident recommendation → fallback); the UI passes no routes.
    refresh: rebuild your category history from Actual first."""
    gateway = gateway or actual_gateway.default()
    rec = _load(sid)
    info = acct.StatementInfo.from_dict(rec["statement"])
    rows = rec["rows"]
    ctx, match_rows, error = None, [], None
    try:
        ctx, match_rows = _connect(gateway, rows)
    except (GatewayError, NotConnected) as e:
        error = str(e)
    connected = ctx is not None
    counts = acct.match_counts(rows, match_rows)
    recommendation = acct.recommend(info, rows, match_rows, counts) if connected else _no_recommendation(info)

    chosen, why = (account_id, "chosen in the web UI") if account_id else (None, None)
    if not chosen and connected:
        chosen, why = acct.resolve(info, recommendation, match_rows, rec["filename"], routes, fallback)
    names = {a["id"]: a.get("name", a["id"]) for a in match_rows}
    guard = acct.cross_account_conflict(chosen, rows, counts, names) if chosen and connected else None

    hist = history.get(ctx, refresh)
    enriched = enrich(rows, ctx, account_id=chosen, use_llm=use_llm, history=hist)
    txns = _apply_categories(enriched["transactions"], categories, ctx)

    duplicates = None
    sending = [t for t in txns if t["imported_id"] not in set(exclude or [])]
    if chosen and connected and sending:
        try:
            d = gateway.import_rows(chosen, sending, dry_run=True)
            duplicates = {"new": d.get("added", 0), "already_imported": d.get("skipped", 0),
                          "to_verify": d.get("toVerify") or []}
        except GatewayError as e:
            error = str(e)
    import_log.update(sid, preview={"account_id": chosen, "use_llm": use_llm, "transactions": txns,
                                    "stats": enriched["stats"]})
    return {
        "session": sid,
        "statement": info.to_dict(),
        "bank": info.bank,
        "transactions": txns,
        "stats": enriched["stats"],
        "count": len(txns),
        "account": {**recommendation, "selected": chosen, "why": why, "names": names},
        "guard": guard,
        "duplicates": duplicates,
        "actual_connected": connected,
        "error": error,
        "history_size": len(hist) if hist is not None else 0,
        "status": rec["status"],
        "seen_before": import_log.seen_before(rec.get("sha256"), exclude=sid),
    }


def commit(sid: str, account_id: str, *, categories: dict | None = None,
           include_verified: list[str] | None = None, exclude: list[str] | None = None,
           override_account_guard: bool = False, learn_rules: bool = True, use_llm: bool = True,
           reconcile_use_llm: bool | None = None, why: str = "chosen in the web UI", gateway=None) -> dict:
    gateway = gateway or actual_gateway.default()
    rec = _load(sid)
    if rec["status"] in COMMITTED and rec.get("result"):
        return rec["result"]
    if not account_id:
        raise StatementImportError("account_id required")
    info = acct.StatementInfo.from_dict(rec["statement"])
    rows = rec["rows"]

    # ── before the import: nothing has changed yet ──
    try:
        ctx, match_rows = _connect(gateway, rows)
    except (GatewayError, NotConnected) as e:
        import_log.fail(sid, "connect", e)
        raise
    names = {a["id"]: a.get("name", a["id"]) for a in match_rows}
    if account_id not in names:
        raise StatementImportError(f"Account {account_id} isn't an open account in Actual")
    conflict = acct.cross_account_conflict(account_id, rows, acct.match_counts(rows, match_rows), names)
    if conflict and not override_account_guard:
        msg = f"Refusing to import into '{names[account_id]}': {conflict}"
        import_log.fail(sid, "account", msg)
        raise Conflict(msg)

    p = rec.get("preview") or {}
    if p.get("account_id") == account_id and p.get("use_llm") == use_llm and p.get("transactions"):
        base, stats = p["transactions"], p.get("stats")
    else:
        e = enrich(rows, ctx, account_id=account_id, use_llm=use_llm, history=history.get(ctx))
        base, stats = e["transactions"], e["stats"]
    leave_out = set(exclude or [])
    txns = [t for t in _apply_categories(base, categories, ctx) if t["imported_id"] not in leave_out]
    if not txns:
        raise StatementImportError("Nothing to import")
    reimport_deleted = True if rec["status"] == "undone" and rec.get("account_id") == account_id else None

    # ── the import: the point of no return ──
    try:
        result = gateway.import_rows(account_id, txns, verified={i: "import" for i in include_verified or []},
                                     reimport_deleted=reimport_deleted)
    except GatewayError as e:
        import_log.fail(sid, "import", e)
        raise
    name = names[account_id]
    import_log.record_result(sid, result, account_id, name, why)
    import_log.update(sid, stage=None, error=None, stats=stats)

    # ── after the import: each step on its own ──
    steps: dict[str, dict] = {}
    review_ids: list[str] = []

    def step(label, fn):
        try:
            steps[label] = {"ok": True, **(fn() or {})}
        except Exception as e:
            steps[label] = {"ok": False, "error": str(e)[:300]}

    step("remember_account", lambda: acct.remember(info.fingerprint, account_id, name) and {})

    def hold():
        if include_verified is not None:          # a person decided every possible duplicate
            return {"held": 0}
        by_id = {t["imported_id"]: t for t in txns}
        by_key = {(t["date"], t["description"], t["amount"]): t for t in txns}
        ids = []
        for u in result.get("unreviewed") or []:
            row = by_id.get(u.get("imported_id")) or by_key.get((u["date"], u["description"], u["amount"]))
            if row:
                it = review.enqueue("import_duplicate", {"account": account_id, "account_name": name,
                                                         "incoming": row, "reason": u.get("reason")},
                                    [account_id, row["imported_id"]])
                ids.append(it["id"])
        review_ids.extend(ids)
        return {"held": len(ids)}
    step("hold_duplicates", hold)

    def learn():
        if not learn_rules:
            return {"created": 0}
        seen, mappings = set(), []
        for t in txns:
            if (t.get("source") == "manual" or t.get("confirmed")) and t.get("category_id") and t.get("payee") \
                    and t["payee"] not in seen:
                seen.add(t["payee"])
                mappings.append({"payee": t["payee"], "category_id": t["category_id"]})
        return {"created": gateway.create_rules(mappings) if mappings else 0}
    step("learn_rules", learn)

    def scan():
        dates = sorted(t["date"] for t in txns)
        s = review_scan.scan_and_queue(dates[0], dates[-1], [account_id], gateway)
        review_ids.extend(i["id"] for i in s["items"])
        return {k: s[k] for k in ("queued", "auto_applied", "duplicates", "transfers")}
    step("scan", scan)

    reconciled = {}

    def run_reconcile():
        res = recon.run(account_id, info, txns,
                        use_llm=use_llm if reconcile_use_llm is None else reconcile_use_llm, gateway=gateway)
        queued = res.get("queued") or []
        review_ids.extend(q["id"] for q in queued if q.get("id"))
        import_log.update(sid, reconcile=import_log.reconcile_summary(res["report"], queued))
        reconciled.update(res)
        return {"reconciled": res["report"]["reconciled"], "gap": res["report"]["gap"]}
    step("reconcile", run_reconcile)

    try:
        import_log.add_review_ids(sid, review_ids)
    except Exception as e:
        steps["history"] = {"ok": False, "error": str(e)[:300]}
    out = {
        "session": sid, "account_id": account_id, "account_name": name, "why": why,
        "import": result, "steps": steps, "review_ids": sorted(set(review_ids)),
        "reconcile": reconciled or None, "sent_ids": [t["imported_id"] for t in txns],
    }
    import_log.update(sid, result=out)
    return out


def reconcile(sid: str, balance: float | None = None, *, statement_date: str | None = None,
              use_llm: bool = True, gateway=None) -> dict:
    """Re-run reconcile for a committed session, e.g. with the balance you typed in.
    balance: signed as Actual shows it (a card you owe money on is negative)."""
    rec = _load(sid)
    res = rec.get("result")
    if rec["status"] not in COMMITTED or not res:
        raise Conflict("Import the statement before reconciling it")
    info = acct.StatementInfo.from_dict(rec["statement"])
    sent = set(res.get("sent_ids") or [])
    rows = [t for t in (rec.get("preview") or {}).get("transactions") or rec["rows"] if t["imported_id"] in sent]
    out = recon.run(res["account_id"], info, rows, balance, use_llm=use_llm, statement_date=statement_date,
                    gateway=gateway)
    queued = out.get("queued") or []
    import_log.update(sid, reconcile=import_log.reconcile_summary(out["report"], queued))
    import_log.add_review_ids(sid, [q["id"] for q in queued if q.get("id")])
    return out


def undo(sid: str, gateway=None) -> dict:
    """Delete the rows the commit added. Actual remembers deleted imported_ids per account, so
    re-importing into that account skips them — unless this session is committed again there."""
    gateway = gateway or actual_gateway.default()
    rec = import_log.get(sid)
    if not rec:
        raise NotFound("No such import")
    if rec["status"] != "imported" or not rec["added_ids"]:
        raise Conflict("Only an import that added rows, and hasn't been undone, can be undone")
    data = gateway.delete(rec["added_ids"])
    import_log.update(sid, status="undone", result=None)
    return {**data, "import": import_log.get(sid)}


# ── Watch-folder import ──────────────────────────────────────────────────────

def import_unattended(source: str, filename: str, content: bytes, *, routes: dict | None = None,
                      fallback: str | None = None, use_llm: bool = True, gateway=None) -> dict:
    """Open, preview and commit with no decisions from a person. Raises StatementImportError
    (or GatewayError) with the stage recorded in the import history when it can't import."""
    gateway = gateway or actual_gateway.default()
    sid = open_session(source, filename, content)
    try:
        p = preview(sid, use_llm=use_llm, routes=routes, fallback=fallback, gateway=gateway)
    except ValueError as e:                       # a route naming a missing account
        import_log.fail(sid, "account", e)
        raise StatementImportError(str(e)) from e
    if not p["actual_connected"]:
        import_log.fail(sid, "connect", p["error"] or "Actual not connected")
        raise NotConnected(p["error"] or "Could not load the Actual budget — load it once in the web UI")
    account_id = p["account"]["selected"]
    if not account_id:
        import_log.fail(sid, "account", p["account"]["why"])
        raise StatementImportError(p["account"]["why"])
    return {**commit(sid, account_id, why=p["account"]["why"], use_llm=use_llm, learn_rules=False,
                     gateway=gateway), "statement": p["statement"], "stats": p["stats"]}
