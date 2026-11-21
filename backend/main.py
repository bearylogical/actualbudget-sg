from fastapi import FastAPI, UploadFile, File, HTTPException
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
from rules_audit import audit, to_markdown
from taxonomy import TAXONOMY, CATEGORIES, load_aliases, save_aliases
import health
import connection
import import_log
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


async def get_context() -> ActualContext | None:
    """Live categories/payees/rules from the loaded Actual budget, or None."""
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.get(f"{BRIDGE_URL}/context")
        if r.is_success:
            return ActualContext.from_bridge(r.json())
    except httpx.HTTPError:
        pass
    return None


async def require_context() -> ActualContext:
    ctx = await get_context()
    if ctx is None:
        raise HTTPException(
            400, "No Actual budget loaded — connect and load a budget first"
        )
    return ctx


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


@app.post("/rules/audit")
async def rules_audit(body: dict | None = None):
    body = body or {}
    ctx = await require_context()
    report = audit(
        ctx, body.get("sample_descriptions"), load_aliases(), bool(body.get("all_seed"))
    )
    return {"report": report, "markdown": to_markdown(report)}


@app.get("/rules/audit.md", response_class=PlainTextResponse)
async def rules_audit_md():
    ctx = await require_context()
    return to_markdown(audit(ctx, None, load_aliases()))


@app.post("/rules/sync")
async def rules_sync(body: dict):
    """
    body: {
      dryRun: bool (default true),
      parts: {aliases, categories, merge_payees, rules, duplicates, broken}  (bools),
      sample_descriptions: [...], all_seed: bool
    }
    """
    dry = body.get("dryRun", True)
    parts = body.get("parts") or {}
    ctx = await require_context()
    report = audit(
        ctx, body.get("sample_descriptions"), load_aliases(), bool(body.get("all_seed"))
    )
    plan = report["plan"]
    selected = {
        "aliases": plan["aliases"] if parts.get("aliases") else {},
        "createCategories": plan["create_categories"]
        if parts.get("categories")
        else [],
        "mergePayees": plan["merge_payees"] if parts.get("merge_payees") else [],
        "createRules": plan["create_rules"] if parts.get("rules") else [],
        "deleteRules": (plan["delete_rules"] if parts.get("duplicates") else [])
        + (plan["delete_broken_rules"] if parts.get("broken") else []),
    }
    if not parts.get("categories"):
        # rules that need a not-yet-created category can't be applied
        selected["createRules"] = [
            r
            for r in selected["createRules"]
            if not any(
                isinstance(a.get("value"), dict) and "$category" in a["value"]
                for a in r["actions"]
            )
        ]
    counts = {k: len(v) for k, v in selected.items()}
    if dry:
        return {"dryRun": True, "counts": counts, "plan": selected}

    if selected["aliases"]:
        save_aliases({**load_aliases(), **selected["aliases"]})
    result = {"ok": True}
    if any(
        selected[k]
        for k in ("createCategories", "mergePayees", "createRules", "deleteRules")
    ):
        body_out = {
            k: selected[k]
            for k in ("createCategories", "mergePayees", "createRules", "deleteRules")
        }
        result = await _bridge("POST", "/rules/apply", body_out, timeout=180)
    after = await require_context()
    after_report = audit(
        after,
        body.get("sample_descriptions"),
        load_aliases(),
        bool(body.get("all_seed")),
    )
    return {
        "dryRun": False,
        "counts": counts,
        "result": result,
        "summary_after": after_report["summary"],
    }


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


async def _bridge(method: str, path: str, body: dict = None, timeout: int = 30, _retry: bool = True):
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            if method == "GET":
                r = await client.get(f"{BRIDGE_URL}{path}")
            else:
                r = await client.post(f"{BRIDGE_URL}{path}", json=body or {})
    except httpx.TimeoutException:
        raise HTTPException(504, f"actual-bridge did not answer within {timeout}s — it may be stuck "
                                 "reaching your Actual server (DNS / TLS / firewall from inside Docker?)")
    except httpx.HTTPError as e:
        raise HTTPException(502, f"Can't reach actual-bridge at {BRIDGE_URL} ({type(e).__name__}) — "
                                 "is the container running? `docker compose logs actual-bridge`")
    if not r.is_success:
        try:
            detail = r.json().get("error", r.text)
        except ValueError:
            detail = r.text
        # bridge restarted and lost its in-memory budget → reload the saved connection, retry once
        if _retry and bridge_client.is_no_budget(r.status_code, detail) \
                and await run_in_threadpool(bridge_client.reload_saved_budget):
            return await _bridge(method, path, body, timeout, _retry=False)
        raise HTTPException(r.status_code, detail)
    return r.json()


@app.post("/actual/budgets")
async def actual_list_budgets(body: dict):
    return await _bridge("POST", "/budgets", body)


@app.post("/actual/budgets/load")
async def actual_load_budget(body: dict):
    data = await _bridge("POST", "/budgets/load", body, timeout=60)
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
    return await _bridge("GET", "/accounts")


@app.get("/actual/categories")
async def actual_categories():
    return await _bridge("GET", "/categories")


@app.post("/actual/categories")
async def actual_create_category(body: dict):
    return await _bridge("POST", "/categories", body)


@app.get("/actual/payees")
async def actual_payees():
    return await _bridge("GET", "/payees")


@app.get("/actual/rules")
async def actual_rules():
    return await _bridge("GET", "/rules")


@app.post("/actual/rules")
async def actual_create_rules(body: dict):
    return await _bridge("POST", "/rules", body)


@app.post("/actual/preview")
async def actual_preview(body: dict):
    return await _bridge("POST", "/preview", body)


@app.get("/actual/budget-month/{month}")
async def actual_budget_month(month: str):
    return await _bridge("GET", f"/budget-month/{month}")


@app.post("/actual/reset")
async def actual_reset():
    """Disconnect: also forget the saved connection, so the scheduler stops importing."""
    try:
        connection.clear()
    except Exception as e:
        print(f"[connection] could not clear scheduler config: {e}")
    return await _bridge("POST", "/reset")


# ── Review queue (duplicates / transfers / reconciliation fixes) ─────────────
import review
from datetime import date as _date, timedelta as _td


_apply = review_scan.apply_item
scan_and_queue = review_scan.scan_and_queue
_account_last4 = review_scan.account_last4   # bound here: the review_scan route below shadows the module


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
async def review_scan(body: dict | None = None):
    """{days: 90} or {start, end, account_ids}"""
    body = body or {}
    end = body.get("end") or _date.today().isoformat()
    start = body.get("start") or (_date.fromisoformat(end) - _td(days=int(body.get("days", 90)))).isoformat()
    try:
        res = await run_in_threadpool(scan_and_queue, start, end, body.get("account_ids"))
    except bridge_client.BridgeError as e:
        raise HTTPException(502, str(e))
    return res


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
    return await run_in_threadpool(_apply, item)


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
    try:
        rows, accts = await run_in_threadpool(bridge_client.txns, start, end, body.get("account_ids"))
    except bridge_client.BridgeError as e:
        raise HTTPException(502, str(e))
    try:
        res = await run_in_threadpool(hunt.run, rows, accts, (start, end), llm, _account_last4(),
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
    """{account_id, statement?, transactions?, balance?, as_of?, use_llm?, statement_date?} → report + queued fixes.
    statement_date: a card's statement (cycle) date, printed on the monthly e-statement — remembered per account."""
    if not body.get("account_id"):
        raise HTTPException(400, "account_id required")
    if body.get("statement_date"):
        try:
            _date.fromisoformat(body["statement_date"])
        except ValueError:
            raise HTTPException(400, "statement_date must be YYYY-MM-DD")
    info = acct.StatementInfo.from_dict(body.get("statement"))
    try:
        res = await run_in_threadpool(recon_run, body["account_id"], info, body.get("transactions") or [],
                                      body.get("balance"), body.get("as_of"), body.get("use_llm", True),
                                      body.get("statement_date") or None)
    except bridge_client.BridgeError as e:
        raise HTTPException(502, str(e))
    return res


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
    return {v["account_id"] for fp, v in acct.load_map().items() if "|credit_card|" in fp}


@app.get("/finance/summary")
async def finance_summary(months: int = 6):
    try:
        actual = await run_in_threadpool(bridge_client.call, "GET", f"/finance/summary?months={int(months)}")
    except bridge_client.BridgeError as e:
        raise HTTPException(502, str(e))
    ghost = await run_in_threadpool(ghostfolio.summary)
    return finance.compute(actual, ghost, _card_account_ids(), review.counts().get("pending", 0))


@app.post("/finance/brief")
async def finance_brief():
    llm = LLMCategorizer()
    if not llm.enabled:
        raise HTTPException(400, "LLM is not configured (LLM_PROVIDER)")
    summary = await finance_summary()
    bullets = await run_in_threadpool(finance.brief, summary, llm)
    return {"bullets": bullets, "model": llm.model}


# ── Investments: IBKR → Ghostfolio (on demand, data read by Claude's IBKR connector) ──
import re

import ibkr_ghostfolio as ibgf


def _ibkr_account(account_id: str | None) -> dict:
    if account_id:
        a = next((x for x in ghostfolio.accounts() if x["id"] == account_id), None)
        if not a:
            raise HTTPException(400, f"Ghostfolio has no account {account_id}")
        return a
    a = ghostfolio.find_broker_account()
    if not a:
        raise HTTPException(400, "No IBKR account found in Ghostfolio — create one (platform: Interactive Brokers) "
                                 "or pass account_id")
    return a


@app.post("/investments/ibkr/preview")
async def ibkr_preview(body: dict):
    """{trades: [...], positions: [...], account_id?} — raw IBKR connector output. Writes nothing."""
    def run():
        a = _ibkr_account(body.get("account_id"))
        mapped = ibgf.map_trades(body.get("trades") or [], body.get("positions") or [], a["id"])
        new, dup = ibgf.already_imported(mapped["activities"], ghostfolio.orders(a["id"]))
        check = ghostfolio.import_activities(new, dry_run=True) if new else {"activities": []}
        problems = [x for x in check.get("activities", []) if x.get("error")]
        current = ghostfolio.holdings(a["id"])
        before = ibgf.compare_positions(body.get("positions") or [], current)
        # what would still differ after importing `new` → candidates for opening lots
        after = ibgf.compare_positions(body.get("positions") or [],
                                       current + [{"symbol": x["symbol"], "quantity": x["quantity"] * (1 if x["type"] == "BUY" else -1)}
                                                  for x in new])
        first = min((x["date"][:10] for x in mapped["activities"]), default=_date.today().isoformat())
        opening_date = (_date.fromisoformat(first) - _td(days=1)).isoformat()
        return {"account": {"id": a["id"], "name": a.get("name"), "currency": a.get("currency"),
                            "cash_balance_to_clear": float(a.get("balance") or 0)}, "new": new, "already_imported": len(dup),
                "skipped": mapped["skipped"], "unmapped": mapped["unmapped"], "ghostfolio_check": problems,
                "position_diff_before": before, "position_diff_after_import": after,
                "suggested_opening_lots": ibgf.opening_lots(body.get("positions") or [], after, a["id"], opening_date)}
    try:
        return await run_in_threadpool(run)
    except ghostfolio.GhostfolioError as e:
        raise HTTPException(400, str(e))
    except httpx.HTTPError as e:
        raise HTTPException(502, f"Ghostfolio unreachable: {e}")


@app.post("/investments/ibkr/import")
async def ibkr_import(body: dict):
    """{activities: [...from preview.new], positions?: [...], account_id?, keep_cash?} — imports into Ghostfolio.

    Every activity is linked to the IBKR account (activities without accountId would land in no account),
    anything already in Ghostfolio is dropped again (safe to re-run), and afterwards the account's cash
    balance is set to 0 so Ghostfolio tracks the stocks/ETFs only — the cash is tracked in Actual."""
    acts = body.get("activities") or []
    if not acts:
        raise HTTPException(400, "nothing to import")
    bad = [a for a in acts if not re.search(r"ibkr:\S+", str(a.get("comment") or ""))]
    if bad:
        raise HTTPException(400, f"{len(bad)} activities lack the ibkr:<trade id> comment — pass the preview's "
                                 "`new` / `suggested_opening_lots` unchanged")
    def run():
        a = _ibkr_account(body.get("account_id") or next((x["accountId"] for x in acts if x.get("accountId")), None))
        linked = [{**x, "accountId": a["id"]} for x in acts]
        new, dup = ibgf.already_imported(linked, ghostfolio.orders(a["id"]))
        res = ghostfolio.import_activities(new, dry_run=False) if new else {"activities": []}
        cleared = [] if body.get("keep_cash") else ghostfolio.clear_cash([a["id"]], dry_run=False)
        diff = ibgf.compare_positions(body.get("positions") or [], ghostfolio.holdings(a["id"])) if body.get("positions") else None
        return {"account": {"id": a["id"], "name": a.get("name")}, "imported": len(res.get("activities", new)),
                "already_imported": len(dup), "cash_cleared": cleared, "position_diff_after": diff}
    try:
        return await run_in_threadpool(run)
    except ghostfolio.GhostfolioError as e:
        raise HTTPException(400, str(e))
    except httpx.HTTPError as e:
        raise HTTPException(502, f"Ghostfolio unreachable: {e}")


@app.post("/investments/ghostfolio/clear-cash")
async def ghostfolio_clear_cash(body: dict | None = None):
    """{account_ids?: [...], dry_run?: true} — zero cash balances so Ghostfolio tracks securities only.
    Default is a dry run over all accounts; activities and holdings are never touched."""
    body = body or {}
    try:
        changed = await run_in_threadpool(ghostfolio.clear_cash, body.get("account_ids") or None,
                                          body.get("dry_run", True) is not False)
    except ghostfolio.GhostfolioError as e:
        raise HTTPException(400, str(e))
    except httpx.HTTPError as e:
        raise HTTPException(502, f"Ghostfolio unreachable: {e}")
    return {"dry_run": body.get("dry_run", True) is not False, "accounts": changed}


@app.get("/investments/ghostfolio/activities")
async def ghostfolio_activities(account_id: str = "", symbol: str = ""):
    """Activities in Ghostfolio (id, date, type, symbol, quantity, unitPrice, fee, comment), newest first."""
    try:
        rows = [ghostfolio.activity_view(o) for o in await run_in_threadpool(ghostfolio.orders, account_id or None)]
    except ghostfolio.GhostfolioError as e:
        raise HTTPException(400, str(e))
    except httpx.HTTPError as e:
        raise HTTPException(502, f"Ghostfolio unreachable: {e}")
    if symbol:
        rows = [r for r in rows if symbol.upper() in str(r["symbol"] or "").upper()]
    rows.sort(key=lambda r: r["date"], reverse=True)
    return {"count": len(rows), "activities": rows}


@app.post("/investments/ghostfolio/activities/{activity_id}/update")
async def ghostfolio_update_activity(activity_id: str, body: dict):
    """{changes: {fee?, quantity?, unitPrice?, date?, type?, currency?, comment?, accountId?}, dry_run?: true}"""
    try:
        return await run_in_threadpool(ghostfolio.update_activity, activity_id, body.get("changes") or {},
                                       body.get("dry_run", True) is not False)
    except ghostfolio.GhostfolioError as e:
        raise HTTPException(400, str(e))
    except httpx.HTTPError as e:
        raise HTTPException(502, f"Ghostfolio unreachable: {e}")


@app.post("/investments/ghostfolio/activities/{activity_id}/delete")
async def ghostfolio_delete_activity(activity_id: str, body: dict | None = None):
    """{dry_run?: true} — delete one activity (dry run by default)."""
    try:
        return await run_in_threadpool(ghostfolio.delete_activity, activity_id,
                                       (body or {}).get("dry_run", True) is not False)
    except ghostfolio.GhostfolioError as e:
        raise HTTPException(400, str(e))
    except httpx.HTTPError as e:
        raise HTTPException(502, f"Ghostfolio unreachable: {e}")


@app.get("/finance/week")
async def finance_week(days: int = 7):
    """Last N days of spending vs your usual week (previous 4 weeks)."""
    today = _date.today()
    start = (today - _td(days=days - 1 + 28)).isoformat()
    try:
        rows, accts = await run_in_threadpool(bridge_client.txns, start, today.isoformat())
        ctx = await run_in_threadpool(bridge_client.call, "GET", "/context")
    except bridge_client.BridgeError as e:
        raise HTTPException(502, str(e))
    names = {c["id"]: c["name"] for c in ctx.get("categories", [])}
    return finance.week(rows, names, today, days)


@app.get("/finance/transactions")
async def finance_transactions(start: str = "", end: str = "", q: str = "", account: str = "",
                               category: str = "", kind: str = "all", limit: int = 200):
    """Historical Actual transactions, filtered, newest first (default: last 30 days, max 500 rows)."""
    try:
        end_d = _date.fromisoformat(end) if end else _date.today()
        start_d = _date.fromisoformat(start) if start else end_d - _td(days=29)
    except ValueError:
        raise HTTPException(400, "start/end must be YYYY-MM-DD")
    if start_d > end_d:
        raise HTTPException(400, "start is after end")
    if kind not in finance.TXN_KINDS:
        raise HTTPException(400, f"kind must be one of {', '.join(finance.TXN_KINDS)}")
    try:
        rows, _ = await run_in_threadpool(bridge_client.txns, start_d.isoformat(), end_d.isoformat())
        ctx = await run_in_threadpool(bridge_client.call, "GET", "/context")
    except bridge_client.BridgeError as e:
        raise HTTPException(502, str(e))
    names = {c["id"]: c["name"] for c in ctx.get("categories", [])}
    r = finance.transactions(rows, names, q, account, category, kind, min(max(limit, 1), 500))
    return {"from": start_d.isoformat(), "to": end_d.isoformat(), **r}


@app.get("/finance/trends")
async def finance_trends(months: int = 12):
    """Monthly income / spending / categories and month-end balances for the last N months (max 24)."""
    months = min(max(months, 1), 24)
    today = _date.today()
    start = _date(today.year, today.month, 1)
    for _ in range(months - 1):
        start = (start - _td(days=1)).replace(day=1)
    try:
        actual = await run_in_threadpool(bridge_client.call, "GET", f"/finance/summary?months={months}")
        rows, _ = await run_in_threadpool(bridge_client.txns, start.isoformat(), today.isoformat())
    except bridge_client.BridgeError as e:
        raise HTTPException(502, str(e))
    return finance.trends(actual, rows, _date.fromisoformat(actual.get("today") or today.isoformat()))


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
