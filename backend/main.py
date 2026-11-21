from fastapi import FastAPI, Request, UploadFile, File, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, PlainTextResponse, JSONResponse
from starlette.concurrency import run_in_threadpool
import pandas as pd
import io
import os
import httpx
from parsers import SUPPORTED_EXTENSIONS
import accounts as acct
from actual_rules import ActualContext
from llm import LLMCategorizer
from rules_audit import audit, to_markdown, sync as rules_sync_run
import actual_gateway
from taxonomy import TAXONOMY, CATEGORIES, load_aliases, save_aliases
import health
import connection
import import_log
import actual_gateway
import bridge_client
import actual_gateway
import review_scan
import statement_import as si
from reconcile import run as recon_run

BRIDGE_URL = os.getenv("ACTUAL_BRIDGE_URL", "http://actual-bridge:3001")

app = FastAPI(title="Budget Parser API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=os.getenv("CORS_ORIGINS", "http://localhost:3000").split(","),
    allow_methods=["*"],
    allow_headers=["*"],
)


def gateway() -> actual_gateway.HttpGateway:
    """The Actual gateway every route reaches Actual through (tests swap actual_gateway._default)."""
    return actual_gateway.default()


@app.exception_handler(actual_gateway.GatewayError)
async def _gateway_error(_: Request, e: actual_gateway.GatewayError):
    """One mapping for every route: no budget → 400 (connect first), timeout → 504,
    bridge unreachable or failing → 502, the bridge refusing the request (4xx) → that status."""
    if e.no_budget:
        code, detail = 400, "No Actual budget loaded — connect and load a budget first"
    elif e.timeout:
        code, detail = 504, str(e)
    elif e.status is None:
        code, detail = 502, f"{e} — is actual-bridge running? `docker compose logs actual-bridge`"
    elif 400 <= e.status < 500:
        code, detail = e.status, str(e)
    else:
        code, detail = 502, str(e)
    return JSONResponse({"detail": detail}, status_code=code)


async def require_context() -> ActualContext:
    """Live categories/payees/rules from the loaded Actual budget (reloading it after a bridge
    restart). No budget → GatewayError → 400."""
    return ActualContext.from_bridge(await run_in_threadpool(gateway().context))


# ── Health ───────────────────────────────────────────────────────────────────

@app.get("/health/live")
async def health_live():
    """Process is up. Used by the Docker healthcheck — no dependencies checked."""
    return {"ok": True}


@app.get("/health")
async def health_full(llm: str = "", strict: bool = False):
    """All services. ?llm=refresh re-tests the LLM now; ?strict=1 returns 503 unless ok."""
    report = await health.full_report(BRIDGE_URL, refresh_llm=(llm == "refresh"))
    code = 503 if strict and report["status"] != "ok" else 200
    return JSONResponse(report, status_code=code)


# ── Statement import (statement_import.py) ───────────────────────────────────

def _import_error(e: Exception):
    if isinstance(e, si.StatementImportError):
        raise HTTPException(e.status, str(e))
    if isinstance(e, bridge_client.BridgeError):
        raise HTTPException(502, str(e))
    raise e


@app.post("/imports")
async def imports_open(file: UploadFile = File(...), use_llm: bool = True):
    """Upload a statement → an Import session and its first preview."""
    name = (file.filename or "").lower()
    if not name.endswith(SUPPORTED_EXTENSIONS):
        raise HTTPException(400, f"Supported files: {', '.join(SUPPORTED_EXTENSIONS)}")
    content = await file.read()
    try:
        sid = await run_in_threadpool(si.open_session, "ui", file.filename or "statement", content)
        return await run_in_threadpool(lambda: si.preview(sid, use_llm=use_llm))
    except Exception as e:
        _import_error(e)


@app.post("/imports/{sid}/preview")
async def imports_preview(sid: str, body: dict | None = None):
    """{account_id?, use_llm?, refresh?, categories?, exclude?} → what committing would do."""
    b = body or {}
    try:
        return await run_in_threadpool(lambda: si.preview(
            sid, b.get("account_id") or None, use_llm=b.get("use_llm", True), refresh=bool(b.get("refresh")),
            categories=b.get("categories"), exclude=b.get("exclude")))
    except Exception as e:
        _import_error(e)


@app.post("/imports/{sid}/commit")
async def imports_commit(sid: str, body: dict):
    """{account_id, categories?, include_verified?, exclude?, override_account_guard?, learn_rules?,
    use_llm?, reconcile_use_llm?} → the import result and each step after it."""
    try:
        return await run_in_threadpool(lambda: si.commit(
            sid, body.get("account_id") or "", categories=body.get("categories"),
            include_verified=body.get("include_verified"), exclude=body.get("exclude"),
            override_account_guard=bool(body.get("override_account_guard")),
            learn_rules=body.get("learn_rules", True), use_llm=body.get("use_llm", True),
            reconcile_use_llm=body.get("reconcile_use_llm")))
    except Exception as e:
        _import_error(e)


@app.post("/imports/{sid}/reconcile")
async def imports_reconcile(sid: str, body: dict | None = None):
    """{balance?, statement_date?, use_llm?} → reconcile report + queued fixes."""
    b = body or {}
    if b.get("statement_date"):
        try:
            _date.fromisoformat(b["statement_date"])
        except ValueError:
            raise HTTPException(400, "statement_date must be YYYY-MM-DD")
    try:
        return await run_in_threadpool(lambda: si.reconcile(
            sid, b.get("balance"), statement_date=b.get("statement_date") or None, use_llm=b.get("use_llm", True)))
    except Exception as e:
        _import_error(e)


@app.get("/taxonomy")
async def taxonomy():
    return {"groups": TAXONOMY, "categories": CATEGORIES}


@app.get("/aliases")
async def get_aliases():
    return {"aliases": load_aliases()}


@app.post("/aliases")
async def set_aliases(body: dict):
    """body: {"aliases": {canonical: actual category name or id}} — merged into existing."""
    merged = {**load_aliases(), **(body.get("aliases") or {})}
    merged = {k: v for k, v in merged.items() if v}
    save_aliases(merged)
    return {"aliases": merged}


@app.get("/llm/status")
async def llm_status():
    return LLMCategorizer().status()


# ── Rules audit / sync ───────────────────────────────────────────────────────


def _rules_gateway():
    return actual_gateway.default()


async def _rules_call(fn, *args, **kwargs):
    """Run a Rules audit / sync call against the Actual gateway, mapping GatewayError to HTTP."""
    try:
        return await run_in_threadpool(fn, *args, **kwargs)
    except actual_gateway.GatewayError as e:
        msg = str(e)
        if msg == bridge_client.NO_BUDGET:
            raise HTTPException(400, "No Actual budget loaded — connect and load a budget first")
        raise HTTPException(502, msg)


def _rules_report(sample_descriptions=None, all_seed: bool = False) -> dict:
    ctx = ActualContext.from_bridge(_rules_gateway().context())
    return audit(ctx, sample_descriptions, load_aliases(), all_seed)


@app.post("/rules/audit")
async def rules_audit(body: dict | None = None):
    body = body or {}
    report = await _rules_call(
        _rules_report, body.get("sample_descriptions"), bool(body.get("all_seed"))
    )
    return {"report": report, "markdown": to_markdown(report)}


@app.get("/rules/audit.md", response_class=PlainTextResponse)
async def rules_audit_md():
    return to_markdown(await _rules_call(_rules_report))


@app.post("/rules/sync")
async def rules_sync(body: dict):
    """
    body: {
      dryRun: bool (default true),
      parts: {aliases, categories, merge_payees, rules, duplicates, broken}  (bools),
      sample_descriptions: [...], all_seed: bool
    }
    → rules_audit.sync: {dryRun, counts, plan, result?, summary_after?}
    """
    return await _rules_call(
        rules_sync_run,
        _rules_gateway(),
        body.get("parts") or {},
        dry_run=body.get("dryRun", True),
        sample_descriptions=body.get("sample_descriptions"),
        all_seed=bool(body.get("all_seed")),
    )


@app.post("/export/csv")
async def export_csv(data: dict):
    transactions = data.get("transactions", [])
    if not transactions:
        raise HTTPException(400, "No transactions provided")
    df = pd.DataFrame(transactions)
    for col in ("payee", "category", "currency", "is_credit"):
        if col not in df.columns:
            df[col] = ""
    df = df[
        ["date", "payee", "description", "category", "amount", "currency", "is_credit"]
    ]
    df.columns = [
        "Date",
        "Payee",
        "Description",
        "Category",
        "Amount",
        "Currency",
        "IsCredit",
    ]
    buf = io.StringIO()
    df.to_csv(buf, index=False)
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=budget_export.csv"},
    )


# ── Actual Budget bridge proxy ────────────────────────────────────────────────


async def _bridge(method: str, path: str, body: dict = None, timeout: int = 30):
    """Deprecated raw call, kept only for the routes not yet on the gateway (/rules/sync,
    /review/recategorize/apply-many). New code: add a method to actual_gateway.HttpGateway."""
    return await run_in_threadpool(bridge_client.call, method, path, body, timeout)


@app.post("/actual/budgets")
async def actual_list_budgets(body: dict):
    return {"budgets": await run_in_threadpool(gateway().budgets, body)}


@app.post("/actual/budgets/load")
async def actual_load_budget(body: dict):
    data = await run_in_threadpool(gateway().load_budget, body)
    # Share this connection with the scheduler (see connection.py) so it needs no compose config
    try:
        connection.save(body)
    except Exception as e:
        print(f"[connection] could not save scheduler config: {e}")
    return data


@app.get("/scheduler/connection")
async def scheduler_connection():
    """What the scheduler will use (passwords omitted) + the account map it routes with."""
    return {**connection.status(), "account_map": acct.load_map()}


@app.get("/actual/accounts")
async def actual_accounts():
    return {"accounts": await run_in_threadpool(gateway().list_accounts)}


@app.get("/actual/categories")
async def actual_categories():
    return {"categoryGroups": await run_in_threadpool(gateway().category_groups)}


@app.post("/actual/categories")
async def actual_create_category(body: dict):
    """{name, groupId? | groupName?}"""
    if not body.get("name"):
        raise HTTPException(400, "name required")
    return await run_in_threadpool(gateway().create_category, body["name"], body.get("groupId"),
                                   body.get("groupName"))


@app.get("/actual/payees")
async def actual_payees():
    return {"payees": await run_in_threadpool(gateway().list_payees)}


@app.get("/actual/rules")
async def actual_rules():
    return {"rules": await run_in_threadpool(gateway().list_rules)}


@app.post("/actual/rules")
async def actual_create_rules(body: dict):
    """{mappings: [{description, categoryId}]} → {created}: one "payee is X → category" rule each."""
    mappings = [{"payee": m["description"], "category_id": m["categoryId"]}
                for m in body.get("mappings") or [] if m.get("description") and m.get("categoryId")]
    return {"ok": True, "created": await run_in_threadpool(gateway().create_rules, mappings)}


@app.get("/actual/budget-month/{month}")
async def actual_budget_month(month: str):
    return await run_in_threadpool(gateway().budget_month, month)


@app.post("/actual/reset")
async def actual_reset():
    """Disconnect: also forget the saved connection, so the scheduler stops importing."""
    try:
        connection.clear()
    except Exception as e:
        print(f"[connection] could not clear scheduler config: {e}")
    await run_in_threadpool(gateway().disconnect)
    return {"ok": True}


# ── Review queue (duplicates / transfers / reconciliation fixes) ─────────────
import review
from datetime import date as _date, timedelta as _td


_apply = review_scan.apply_item


@app.post("/review/recategorize/scan")
async def review_recategorize_scan(body: dict | None = None):
    """{days: 120, mode: "uncategorised"|"all", use_llm: true} or {start, end, account_ids}"""
    import recategorize
    body = body or {}
    try:
        return await run_in_threadpool(lambda: recategorize.scan(
            actual_gateway.default(), start=body.get("start"), end=body.get("end"),
            days=int(body.get("days", recategorize.SCAN_DAYS)), mode=body.get("mode", "uncategorised"),
            use_llm=body.get("use_llm", True), account_ids=body.get("account_ids")))
    except actual_gateway.GatewayError as e:
        raise HTTPException(502, str(e))


@app.get("/review")
async def review_list(status: str = "pending", kind: str = ""):
    return {"items": review.list_items(status or None, kind or None), "counts": review.counts()}


@app.get("/review/counts")
async def review_counts():
    return review.counts()


@app.post("/review/scan")
async def review_scan_route(body: dict | None = None):
    """{days: 90} or {start, end, account_ids}"""
    body = body or {}
    end = body.get("end") or _date.today().isoformat()
    start = body.get("start") or (_date.fromisoformat(end) - _td(days=int(body.get("days", 90)))).isoformat()
    return await run_in_threadpool(review_scan.scan_and_queue, start, end, body.get("account_ids"), gateway())


@app.post("/review/{item_id}/decide")
async def review_decide(item_id: str, body: dict):
    """Approve a decision; it is applied to Actual immediately and remembered.
    recategorize items also take {category_id} to apply a different category than proposed."""
    it = review.get(item_id)
    if it and it["kind"] == "recategorize":
        return await _recategorize_decide(it, body.get("decision", ""), body.get("category_id"))
    if body.get("category_id"):
        if not it:
            raise HTTPException(404, "no such item")
        raise HTTPException(400, "category_id only applies to recategorize items")
    try:
        item = review.decide(item_id, body.get("decision", ""), by="you")
    except KeyError:
        raise HTTPException(404, "no such item")
    except ValueError as e:
        raise HTTPException(400, str(e))
    return await run_in_threadpool(review_scan.apply_item, item, gateway())


async def _recategorize_decide(it: dict, decision: str, category_id: str | None):
    """A category fix goes through the Recategorize write path (recategorize.apply), like apply-many."""
    import recategorize
    try:
        r = await run_in_threadpool(recategorize.apply, actual_gateway.default(), [it["id"]],
                                    decision=decision, category_id=category_id)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except actual_gateway.GatewayError as e:
        raise HTTPException(502, str(e))
    return r["items"][0] if r["items"] else {**review.get(it["id"]), "applied": None}


@app.post("/review/recategorize/apply-many")
async def review_recategorize_apply_many(body: dict):
    """{ids, category_id?} — approve several category fixes at once (e.g. every waiting row for
    one payee), optionally with a different category. Each counts as your decision, so the
    payee → category pattern is learned. Applied to Actual in one call."""
    import recategorize
    try:
        r = await run_in_threadpool(recategorize.apply, actual_gateway.default(), body.get("ids") or [],
                                    category_id=body.get("category_id"))
    except ValueError as e:
        raise HTTPException(400, str(e))
    except actual_gateway.GatewayError as e:
        raise HTTPException(502, str(e))
    return {"applied": len(r["applied"]), "failed": r["failed"], "superseded": r["superseded"]}


@app.post("/review/recategorize/refresh")
async def review_recategorize_refresh(body: dict | None = None):
    import recategorize
    try:
        return await run_in_threadpool(lambda: recategorize.refresh(
            actual_gateway.default(), use_llm=(body or {}).get("use_llm", True)))
    except actual_gateway.GatewayError as e:
        raise HTTPException(502, str(e))


@app.post("/review/{item_id}/dismiss")
async def review_dismiss(item_id: str):
    review.dismiss(item_id)
    return review.get(item_id)


@app.post("/review/advise")
async def review_advise(body: dict | None = None):
    """Ask the LLM for a verdict on pending items (advisory only)."""
    llm = LLMCategorizer()
    if not llm.enabled:
        raise HTTPException(400, "LLM is not configured (LLM_PROVIDER)")
    ids = set((body or {}).get("ids") or [])
    items = [i for i in review.list_items("pending") if (not ids or i["id"] in ids)
             and i["kind"] not in ("reconcile_fix", "recategorize")][:40]
    verdicts = await run_in_threadpool(review.advise, items, llm)
    return {"advised": len(verdicts), "items": review.list_items("pending")}


@app.post("/review/hunt")
async def review_hunt(body: dict | None = None):
    """AI hunt: an LLM agent searches Actual for duplicates and unreconciled transfers and
    queues what it finds for approval. {days: 90} or {start, end}, account_ids?, instructions?"""
    import hunt
    llm = LLMCategorizer()
    if not llm.enabled:
        raise HTTPException(400, "LLM is not configured (LLM_PROVIDER)")
    body = body or {}
    end = body.get("end") or _date.today().isoformat()
    start = body.get("start") or (_date.fromisoformat(end) - _td(days=int(body.get("days", 90)))).isoformat()
    rows, accts = await run_in_threadpool(gateway().txns, start, end, body.get("account_ids"))
    try:
        res = await run_in_threadpool(hunt.run, rows, accts, (start, end), llm, review_scan.account_last4(),
                                      str(body.get("instructions") or ""))
    except httpx.HTTPError as e:
        raise HTTPException(502, f"LLM request failed: {e}")
    res["counts"] = review.counts()
    return res


@app.post("/review/accept-ai")
async def review_accept_ai(body: dict | None = None):
    """Bulk-approve pending items whose AI verdict is at least min_confidence (default 0.8).
    Deletions are never bulk-approved."""
    min_conf = float((body or {}).get("min_confidence", 0.8))
    import recategorize
    # category fixes: the proposal's own confidence; applied together in one Actual call
    try:
        done = (await run_in_threadpool(recategorize.accept_confident, actual_gateway.default(), min_conf))["items"]
    except actual_gateway.GatewayError as e:
        raise HTTPException(502, str(e))
    for it in review.list_items("pending"):
        if it["kind"] == "recategorize":
            continue
        d = review.suggested_decision(it)
        conf = (it.get("llm") or {}).get("confidence", 0)
        if not d or conf < min_conf or d.startswith("delete"):
            continue
        item = review.decide(it["id"], d, by="you")
        done.append(await run_in_threadpool(_apply, item))
    return {"applied": len(done), "items": done}


@app.get("/review/memory")
async def review_memory():
    return {"memory": review.memory(), "learn_after": review.LEARN_AFTER}


@app.post("/review/memory/forget")
async def review_forget(body: dict):
    review.forget(body.get("key", ""))
    return {"memory": review.memory()}


# ── Reconciliation ───────────────────────────────────────────────────────────

@app.post("/reconcile")
async def reconcile_account(body: dict):
    """{account_id, statement?, transactions?, balance?, bank_balance?, as_of?, use_llm?, statement_date?}
    → report + queued fixes. balance is signed as Actual shows it; bank_balance is as the bank shows it
    (a card: what you owe, positive) and the backend decides whether the account is a card.
    statement_date: a card's statement (cycle) date, printed on the monthly e-statement — remembered per account."""
    if not body.get("account_id"):
        raise HTTPException(400, "account_id required")
    if body.get("statement_date"):
        try:
            _date.fromisoformat(body["statement_date"])
        except ValueError:
            raise HTTPException(400, "statement_date must be YYYY-MM-DD")
    info = acct.StatementInfo.from_dict(body.get("statement"))
    return await run_in_threadpool(recon_run, body["account_id"], info, body.get("transactions") or [],
                                   body.get("balance"), body.get("as_of"), body.get("use_llm", True),
                                   body.get("statement_date") or None, gateway(),
                                   bank_balance=body.get("bank_balance"))


# ── Import history ───────────────────────────────────────────────────────────

@app.get("/imports")
async def imports_list(limit: int = 100, source: str = "", status: str = "", account_id: str = "",
                       abandoned: bool = True):
    """Every statement file uploaded in the UI or dropped in the watch folder, newest first."""
    rows = await run_in_threadpool(import_log.listing, limit, source, status, account_id, abandoned)
    return {"imports": rows, "summary": await run_in_threadpool(import_log.summary)}


@app.get("/imports/{iid}")
async def imports_get(iid: str):
    r = await run_in_threadpool(import_log.get, iid)
    if not r:
        raise HTTPException(404, "No such import")
    for c in import_log.SESSION_COLS:     # session state, not history
        r.pop(c, None)
    if r["review_ids"]:
        statuses = await run_in_threadpool(review.statuses, r["review_ids"])
        r["review"] = statuses
    return r


@app.get("/imports/{iid}/file")
async def imports_file(iid: str):
    from fastapi.responses import FileResponse
    f = await run_in_threadpool(import_log.file_path, iid)
    if not f:
        raise HTTPException(404, "The original file isn't kept for this import")
    return FileResponse(f[0], filename=f[1])


@app.post("/imports/{iid}/undo")
async def imports_undo(iid: str):
    """Delete the rows this import added (see statement_import.undo)."""
    try:
        return await run_in_threadpool(si.undo, iid)
    except Exception as e:
        _import_error(e)


# ── Money dashboard (Actual + Ghostfolio) ────────────────────────────────────
import finance
import ghostfolio


def _card_account_ids() -> set[str]:
    return acct.card_account_ids()


def _snapshot() -> finance.Snapshot:
    import actual_gateway
    return finance.Snapshot(actual_gateway.default(), ghost=ghostfolio.summary,
                            card_ids=_card_account_ids, review_counts=review.counts)


async def _finance(fn, *args):
    """Run a Snapshot view in the threadpool: Actual unreachable → 502, bad input → 400."""
    try:
        return await run_in_threadpool(fn, *args)
    except bridge_client.BridgeError as e:
        raise HTTPException(502, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/finance/summary")
async def finance_summary(months: int = 6):
    return await _finance(_snapshot().summary, months)


@app.post("/finance/brief")
async def finance_brief():
    llm = LLMCategorizer()
    if not llm.enabled:
        raise HTTPException(400, "LLM is not configured (LLM_PROVIDER)")
    summary = await finance_summary(6)
    bullets = await run_in_threadpool(finance.brief, summary, llm)
    return {"bullets": bullets, "model": llm.model}


# ── Investments: IBKR → Ghostfolio (on demand, data read by Claude's IBKR connector) ──
import ibkr_ghostfolio as ibgf


def _ghostfolio() -> ghostfolio.Client:
    """The Ghostfolio adapter the IBKR routes use; tests override it to swap in a fake transport."""
    return ghostfolio.Client()


@app.exception_handler(ghostfolio.GhostfolioError)
async def _ghostfolio_error(_request, e: ghostfolio.GhostfolioError):
    """Refused by Ghostfolio or the sync → 400; Ghostfolio down or erroring → 502."""
    return JSONResponse({"detail": str(e)}, status_code=502 if isinstance(e, ghostfolio.GhostfolioUnreachable) else 400)


def _dry_run(body: dict | None) -> bool:
    return (body or {}).get("dry_run", True) is not False


@app.post("/investments/ibkr/preview")
async def ibkr_preview(body: dict):
    """{trades: [...], positions: [...], account_id?} — raw IBKR connector output. Writes nothing."""
    return await run_in_threadpool(ibgf.preview, _ghostfolio(), body.get("trades") or [],
                                   body.get("positions") or [], body.get("account_id"))


@app.post("/investments/ibkr/import")
async def ibkr_import(body: dict):
    """{activities: [...from preview.new], positions?: [...], account_id?, keep_cash?} — imports into
    Ghostfolio (see ibkr_ghostfolio.import_activities)."""
    return await run_in_threadpool(ibgf.import_activities, _ghostfolio(), body.get("activities") or [],
                                   body.get("positions") or [], body.get("account_id"), bool(body.get("keep_cash")))


@app.post("/investments/ghostfolio/clear-cash")
async def ghostfolio_clear_cash(body: dict | None = None):
    """{account_ids?: [...], dry_run?: true} — zero cash balances so Ghostfolio tracks securities only.
    Default is a dry run over all accounts; activities and holdings are never touched."""
    return await run_in_threadpool(ibgf.clear_cash, _ghostfolio(), (body or {}).get("account_ids"), _dry_run(body))


@app.get("/investments/ghostfolio/activities")
async def ghostfolio_activities(account_id: str = "", symbol: str = ""):
    """Activities in Ghostfolio (id, date, type, symbol, quantity, unitPrice, fee, comment), newest first."""
    return await run_in_threadpool(ibgf.list_activities, _ghostfolio(), account_id, symbol)


@app.post("/investments/ghostfolio/activities/{activity_id}/update")
async def ghostfolio_update_activity(activity_id: str, body: dict):
    """{changes: {fee?, quantity?, unitPrice?, date?, type?, currency?, comment?, accountId?}, dry_run?: true}"""
    return await run_in_threadpool(_ghostfolio().update_activity, activity_id, body.get("changes") or {},
                                   _dry_run(body))


@app.post("/investments/ghostfolio/activities/{activity_id}/delete")
async def ghostfolio_delete_activity(activity_id: str, body: dict | None = None):
    """{dry_run?: true} — delete one activity (dry run by default)."""
    return await run_in_threadpool(_ghostfolio().delete_activity, activity_id, _dry_run(body))


@app.get("/finance/week")
async def finance_week(days: int = 7):
    """Last N days of spending vs your usual week (previous 4 weeks)."""
    return await _finance(_snapshot().week, days)


@app.get("/finance/transactions")
async def finance_transactions(start: str = "", end: str = "", q: str = "", account: str = "",
                               category: str = "", kind: str = "all", limit: int = 200):
    """Historical Actual transactions, filtered, newest first (default: last 30 days, max 500 rows)."""
    return await _finance(_snapshot().transactions, start, end, q, account, category, kind, limit)


@app.get("/finance/trends")
async def finance_trends(months: int = 12):
    """Monthly income / spending / categories and month-end balances for the last N months (max 24)."""
    return await _finance(_snapshot().trends, months)


@app.get("/finance/weekly")
async def finance_weekly(days: int = 7):
    """The weekly report in one call: {week, summary, review, health} (MCP weekly_snapshot)."""
    snap = await _finance(_snapshot().weekly, days)
    return {**snap, "health": await health.full_report(BRIDGE_URL)}


# ── MCP audit trail (events from mcp / mcp-public) + Telegram digest ─────────
# Not reachable through the UI: nginx refuses /api/audit/ (see frontend/nginx.conf).
import asyncio
import logging

import audit as mcp_audit


@app.post("/audit/mcp")
async def audit_ingest(body: dict):
    await run_in_threadpool(mcp_audit.record, body)
    return {"ok": True}


@app.get("/audit/mcp")
async def audit_summary(hours: float = 24):
    import time as _time
    return await run_in_threadpool(mcp_audit.summarize, _time.time() - hours * 3600)


@app.post("/audit/mcp/digest")
async def audit_digest(force: bool = True, send: bool = True):
    """Send the Telegram digest now (force) — or send=false to preview the text."""
    try:
        r = await run_in_threadpool(mcp_audit.digest, force, send)
    except (httpx.HTTPError, RuntimeError) as e:
        raise HTTPException(502, str(e))
    return {k: v for k, v in r.items() if k != "summary"}


async def _audit_digest_loop():
    while True:
        try:
            await run_in_threadpool(mcp_audit.digest)
        except Exception as e:                      # keep going; the next tick retries
            logging.getLogger("audit").warning("audit digest failed: %s", e)
        await asyncio.sleep(300)


@app.on_event("startup")
async def _start_audit_digest():
    if mcp_audit.telegram_configured():
        asyncio.create_task(_audit_digest_loop())
